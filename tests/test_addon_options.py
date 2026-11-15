"""Les options de l'add-on ne doivent pas defaire ce que la page a choisi.

Lancement :  python tests/test_addon_options.py

Deux reglages se font a deux endroits : les options de l'add-on dans Home
Assistant, et l'ecran Reglages de Trouveur. Les premieres sont une amorce, pas
une remise a zero a chaque demarrage -- sinon un choix fait dans la page serait
defait au redemarrage suivant, sans rien dire.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ok: list[bool] = []


def check(label: str, condition: object, detail: str = "") -> None:
    ok.append(bool(condition))
    print("%-58s %s%s" % (label, "OK" if condition else "ECHEC",
                          "  " + detail if detail and not condition else ""))


BAC = tempfile.mkdtemp(prefix="trouveur-options-")
CERTS = os.path.join(BAC, "certs")
os.makedirs(CERTS)
os.environ["TROUVEUR_CONFIG_PATH"] = os.path.join(BAC, "config.json")
os.environ["TROUVEUR_CERTS_DIR"] = CERTS

_spec = importlib.util.spec_from_file_location(
    "apply_options", os.path.join(RACINE, "addon", "apply_options.py"))
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

mod.CONFIG_PATH = os.environ["TROUVEUR_CONFIG_PATH"]
mod.CERTS_DIR = CERTS
mod.OPTIONS_PATH = os.path.join(BAC, "options.json")
mod.SEED_DIR = os.path.join(BAC, "share-absent")


def demarrer(options: dict, config: dict | None = None) -> dict:
    """Rejoue un demarrage de l'add-on et renvoie le config.json obtenu."""
    if config is not None:
        with open(mod.CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(config, fh)
    with open(mod.OPTIONS_PATH, "w", encoding="utf-8") as fh:
        json.dump(options, fh)
    mod.main()
    with open(mod.CONFIG_PATH, encoding="utf-8") as fh:
        return json.load(fh)


# -- le scenario qui attendait l'utilisateur --------------------------------
# Options de l'add-on telles qu'elles existaient : une adresse tr4ker en dur.
ANCIENNES = {"tmdb_api_key": "k", "tracker_enabled": True,
             "tracker_base_url": "https://tr4ker.net",
             "tracker_api_key": "CLE"}

# La page vient d'enregistrer v3x : le profil est pose, les valeurs propres a
# tr4ker ont ete effacees.
CHOIX_PAGE = {"tracker": {"enabled": True, "profile": "v3x.club",
                          "api_key": "CLE-V3X"}}

apres = demarrer(ANCIENNES, CHOIX_PAGE)["tracker"]
check("le profil choisi dans la page survit au redemarrage",
      apres["profile"] == "v3x.club", str(apres))
check("l'adresse de l'ancien tracker ne revient pas",
      apres.get("base_url") in (None, ""), str(apres))

# Le tracker effectivement construit doit bien etre v3x.
sys.path.insert(0, RACINE)
from trouveur import config as cfg           # noqa: E402
from trouveur.tracker import Tracker         # noqa: E402

t = Tracker(cfg._deep_merge(cfg.DEFAULTS, {"tracker": apres}))
check("c'est bien v3x qui est interroge",
      t.base_url == "https://api.v3x.club" and t.auth == "bearer",
      "%s / %s" % (t.base_url, t.auth))

# -- l'option garde son role quand elle a un sens ---------------------------
# Indexeur quelconque : l'adresse ne peut venir que des options.
quelconque = demarrer(
    dict(ANCIENNES, tracker_base_url="https://indexeur.example"),
    {"tracker": {"enabled": True, "profile": "torznab", "api_key": "K"}},
)["tracker"]
check("avec un indexeur quelconque, l'adresse des options s'applique",
      quelconque.get("base_url") == "https://indexeur.example", str(quelconque))

# -- poser le tracker depuis les options de l'add-on ------------------------
pose = demarrer(dict(ANCIENNES, tracker_profile="v3x.club"),
                {"tracker": {"enabled": True, "profile": "tr4ker.net",
                             "base_url": "https://tr4ker.net",
                             "auth": "header", "api_key": "K"}})["tracker"]
check("l'option de profil peut changer de tracker",
      pose["profile"] == "v3x.club", str(pose))
check("elle efface au passage les valeurs de l'ancien",
      not {"base_url", "auth", "header_name"} & set(pose), str(pose))
check("la cle d'API n'est pas perdue", pose.get("api_key") == "CLE", str(pose))

# -- une installation neuve -------------------------------------------------
neuve = demarrer(ANCIENNES, {})["tracker"]
check("sans rien de choisi, tr4ker reste le defaut",
      neuve["profile"] == "tr4ker.net", str(neuve))

# -- le reste de la configuration n'est pas touche --------------------------
garde = demarrer(ANCIENNES, {
    "tracker": {"enabled": True, "profile": "v3x.club", "api_key": "K"},
    "plex": {"enabled": True, "token": "JETON", "client_id": "ID-STABLE"},
})
check("l'identifiant d'installation Plex survit",
      garde["plex"]["client_id"] == "ID-STABLE", str(garde.get("plex")))

shutil.rmtree(BAC, ignore_errors=True)
print("\n%d/%d verifications passees" % (sum(ok), len(ok)))
sys.exit(0 if all(ok) else 1)

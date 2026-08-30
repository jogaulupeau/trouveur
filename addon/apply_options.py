#!/usr/bin/env python3
"""Reporte les options de l'add-on Home Assistant dans /data/config.json.

Regle : les options de l'interface font autorite pour les cles qu'elles
couvrent, et **uniquement** pour celles-la. Tout le reste du fichier est
conserve tel quel — en particulier plex.client_id, ecrit par la connexion
Plex et absent de l'interface : l'ecraser obligerait a se reconnecter a chaque
demarrage.

Une option laissee vide n'ecrase rien non plus. On peut ainsi renseigner un
jeton une fois par « python server.py --plex-login », puis laisser le champ
vide dans l'interface sans le perdre.
"""

from __future__ import annotations

import json
import os
import sys

OPTIONS_PATH = "/data/options.json"
# Dossier partage de Home Assistant, monte en lecture seule : c'est par la
# qu'on recupere des listes existantes lors du tout premier demarrage.
SEED_DIR = "/share/trouveur"
FICHIERS_REPRIS = ("seen.json", "watchlist.json", "ignored.json")
CONFIG_PATH = os.environ.get("TROUVEUR_CONFIG_PATH", "/data/config.json")


def _load(path: str, defaut):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return defaut


def _set(config: dict, chemin: str, valeur) -> None:
    """Pose une valeur, sauf si elle est vide (ne rien effacer par omission)."""
    if valeur is None or valeur == "":
        return
    bloc, _, cle = chemin.partition(".")
    config.setdefault(bloc, {})[cle] = valeur


def reprendre_listes() -> None:
    """Importe des listes deposees dans /share/trouveur, une seule fois.

    Strictement additif : un fichier deja present dans /data n'est jamais
    remplace, et rien n'est efface dans /share. On peut donc laisser les
    fichiers en place sans risque, ils seront simplement ignores ensuite.
    """
    if not os.path.isdir(SEED_DIR):
        return
    for nom in FICHIERS_REPRIS:
        source = os.path.join(SEED_DIR, nom)
        cible = os.path.join("/data", nom)
        if not os.path.isfile(source) or os.path.exists(cible):
            continue
        contenu = _load(source, None)
        # On ne copie qu'un fichier lisible et conforme : mieux vaut demarrer
        # avec une liste vide qu'avec un fichier qui mettra l'app en defaut.
        if not isinstance(contenu, dict) or not isinstance(contenu.get("movies"), dict):
            print("Ignore (format inattendu) : %s" % source, file=sys.stderr)
            continue
        with open(cible, "w", encoding="utf-8") as fh:
            json.dump(contenu, fh, ensure_ascii=False, indent=2)
        print("Liste reprise depuis %s : %d films." % (nom, len(contenu["movies"])))


def main() -> int:
    reprendre_listes()
    options = _load(OPTIONS_PATH, {})
    config = _load(CONFIG_PATH, {})
    if not isinstance(config, dict):
        config = {}

    # Le serveur doit ecouter sur toutes les interfaces du conteneur pour que
    # l'ingress puisse l'atteindre, et ne jamais ouvrir de navigateur.
    config.setdefault("server", {})
    config["server"]["host"] = "0.0.0.0"
    config["server"]["port"] = 8777
    config["server"]["open_browser"] = False

    _set(config, "tmdb.api_key", options.get("tmdb_api_key"))
    _set(config, "tmdb.language", options.get("tmdb_language"))
    _set(config, "tmdb.region", options.get("tmdb_region"))

    services = options.get("my_services")
    if isinstance(services, list):
        # Une liste vide est un choix explicite : tous les services.
        config.setdefault("streaming", {})["my_services"] = [
            int(s) for s in services if str(s).isdigit()
        ]

    config.setdefault("tracker", {})["enabled"] = bool(options.get("tracker_enabled"))
    _set(config, "tracker.base_url", options.get("tracker_base_url"))
    _set(config, "tracker.api_key", options.get("tracker_api_key"))

    config.setdefault("plex", {})["enabled"] = bool(options.get("plex_enabled"))
    _set(config, "plex.base_url", options.get("plex_base_url"))
    _set(config, "plex.token", options.get("plex_token"))
    config["plex"]["verify_tls"] = bool(options.get("plex_verify_tls", True))
    config["plex"]["sync_watched"] = bool(options.get("plex_sync_watched", True))

    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(config, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, CONFIG_PATH)

    if not config.get("tmdb", {}).get("api_key"):
        print(
            "Aucune cle TMDB : renseigne « tmdb_api_key » dans la configuration "
            "de l'add-on.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

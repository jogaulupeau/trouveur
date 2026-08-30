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
# Certificats deposes depuis Reglages : ils l'emportent sur les options, sinon
# un chemin saisi autrefois dans l'interface de l'add-on reviendrait ecraser le
# fichier depose a chaque redemarrage.
CERTS_DIR = os.environ.get("TROUVEUR_CERTS_DIR", "/data/certs")


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


# Doit rester aligne sur trouveur/certificates.py : ce script tourne avant
# l'application et n'importe rien d'elle.
EXTENSIONS = {
    "client": (".p12", ".pfx", ".pem", ".crt", ".cer"),
    "client_key": (".key", ".pem"),
    "ca": (".pem", ".crt", ".cer"),
}


def _certificat_depose(role: str) -> str:
    for extension in EXTENSIONS[role]:
        chemin = os.path.join(CERTS_DIR, role + extension)
        if os.path.isfile(chemin):
            return chemin
    return ""


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

    bloc = config.setdefault("tracker", {})
    # Une case decochee dans les options ne desactive pas ce que l'interface a
    # active : les options sont une amorce, pas une remise a zero a chaque
    # demarrage. Meme regle que pour les champs vides.
    if options.get("tracker_enabled"):
        bloc["enabled"] = True
    bloc.setdefault("enabled", False)
    _set(config, "tracker.base_url", options.get("tracker_base_url"))
    _set(config, "tracker.api_key", options.get("tracker_api_key"))

    bloc = config.setdefault("plex", {})
    if options.get("plex_enabled"):
        bloc["enabled"] = True
    bloc.setdefault("enabled", False)
    _set(config, "plex.base_url", options.get("plex_base_url"))
    _set(config, "plex.token", options.get("plex_token"))
    bloc.setdefault("verify_tls", bool(options.get("plex_verify_tls", True)))
    bloc.setdefault("sync_watched", bool(options.get("plex_sync_watched", True)))

    bloc = config.setdefault("deluge", {})
    if options.get("deluge_enabled"):
        bloc["enabled"] = True
    bloc.setdefault("enabled", False)
    _set(config, "deluge.base_url", options.get("deluge_base_url"))
    _set(config, "deluge.password", options.get("deluge_password"))
    for role, cle, option in (("client", "client_cert", "deluge_client_cert"),
                              ("client_key", "client_key", "deluge_client_key"),
                              ("ca", "ca_cert", "deluge_ca_cert")):
        depose = _certificat_depose(role)
        if depose:
            bloc[cle] = depose
        else:
            _set(config, "deluge." + cle, options.get(option))
    _set(config, "deluge.client_key_password", options.get("deluge_client_key_password"))

    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(config, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, CONFIG_PATH)

    if not config.get("tmdb", {}).get("api_key"):
        # Pas de cle : on demarre quand meme. L'interface s'ouvrira sur son
        # ecran de configuration, ou la cle peut etre saisie. Refuser de
        # demarrer rendrait cet ecran inatteignable.
        print("Aucune cle TMDB. L'interface s'ouvrira sur l'ecran de "
              "configuration : renseigne-la la, ou dans les options de l'add-on.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

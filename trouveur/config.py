"""Chargement et validation de config.json."""

from __future__ import annotations

import json
import os
from typing import Any

from .paths import EXAMPLE_PATH, ROOT, config_path  # noqa: F401 - reexportes

DEFAULTS: dict[str, Any] = {
    "server": {"host": "127.0.0.1", "port": 8777, "open_browser": True},
    "tmdb": {
        "api_key": "",
        "language": "fr-FR",
        "region": "FR",
        "include_adult": False,
    },
    "tracker": {
        # Indexeur Torznab. tr4ker et v3x exposent tous deux ce standard, et
        # leur recherche de films accepte un tmdbid : pas de rapprochement par
        # titre. Un seul tracker est actif a la fois.
        "enabled": False,
        # Profil : "tr4ker.net", "v3x.club", ou "torznab" pour un indexeur
        # quelconque, a decrire alors entierement ici.
        #
        # Nom, adresse, chemin d'API, authentification et chemin de
        # telechargement viennent du profil et ne figurent donc pas ci-dessous.
        # Les y ecrire les remplace -- mais ils survivraient alors a un
        # changement de tracker, ce qui est rarement voulu.
        "profile": "tr4ker.net",
        "api_key": "",
        # Categorie Torznab des films. 2000 couvre aussi ses sous-categories
        # (2040 « Movies/HD », etc.).
        "movie_category": "2000",
        "limit": 100,
        "timeout": 15,
        "max_results": 40,
    },
    "streaming": {
        # Identifiants TMDB des services auxquels tu es abonne. Vide = tous les
        # services de la region sont proposes et affiches.
        "my_services": [],
    },
    "deluge": {
        # Serveur Deluge : Trouveur lui transmet le .torrent deja telecharge,
        # il n'a donc jamais besoin de la cle du tracker.
        "enabled": False,
        "base_url": "",
        "password": "",
        # Acces protege par certificat client : chemins vers les fichiers PEM.
        "client_cert": "",
        "client_key": "",
        "client_key_password": "",
        # Autorite privee ayant signe le certificat du serveur, le cas echeant.
        "ca_cert": "",
        "verify_tls": True,
        "timeout": 20,
        "add_paused": False,
        "download_location": "",
        "label": "",
    },
    "plex": {
        # Serveur Plex (souvent distant : NAS, autre machine du reseau).
        "enabled": False,
        "base_url": "",
        "token": "",
        # Identifiant stable de cette installation, exige par l'API Plex.
        # Cree automatiquement par « python server.py --plex-login ».
        "client_id": "",
        # Vide = toutes les bibliotheques de type film.
        "sections": [],
        # Passe a false si le serveur presente un certificat auto-signe.
        "verify_tls": True,
        "timeout": 20,
        # La bibliotheque est indexee en memoire et rafraichie a cet intervalle.
        "refresh_seconds": 600,
    },
    "cache": {"enabled": True, "ttl_seconds": 86400, "tracker_ttl_seconds": 900},
}


class ConfigError(RuntimeError):
    pass


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load() -> dict[str, Any]:
    chemin = config_path()
    if not os.path.exists(chemin):
        # Premier lancement : on part des valeurs par defaut plutot que de
        # refuser de demarrer. Sans cela l'ecran de configuration serait
        # inatteignable — on ne peut pas configurer une application qui exige
        # d'etre configuree pour demarrer.
        return _deep_merge(DEFAULTS, {})
    try:
        with open(chemin, "r", encoding="utf-8") as fh:
            user_config = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config.json est un JSON invalide : {exc}") from exc

    return _deep_merge(DEFAULTS, user_config)


def is_configured(config: dict[str, Any]) -> bool:
    """Une cle TMDB suffit a rendre l'application utilisable."""
    return bool(config.get("tmdb", {}).get("api_key"))


def load_raw() -> dict[str, Any]:
    """Le fichier tel qu'il est, sans les valeurs par defaut."""
    try:
        with open(config_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save(config: dict[str, Any]) -> None:
    """Ecriture atomique, comme pour les listes."""
    chemin = config_path()
    os.makedirs(os.path.dirname(chemin) or ".", exist_ok=True)
    tmp = chemin + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(config, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, chemin)

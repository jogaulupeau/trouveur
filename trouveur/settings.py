"""Reglages modifiables depuis le navigateur, et connexion Plex assistee.

Deux principes :

  - **Aucun secret ne repart vers le navigateur.** L'interface recoit un simple
    « renseigne / vide » par cle. Afficher un jeton dans une page pour le
    reafficher a l'identique n'apporte rien et l'expose.

  - **Un champ vide ne signifie pas « efface ».** Il signifie « inchange ». On
    peut donc enregistrer un formulaire sans avoir a ressaisir les cles.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from . import config as config_module
from . import certificates
from . import plex_auth

# Les demandes de connexion Plex en cours, le temps que l'utilisateur approuve.
_pins: dict[int, dict[str, Any]] = {}
_pins_lock = threading.Lock()
PIN_TTL = 900


def _prune() -> None:
    limite = time.time() - PIN_TTL
    for pin_id in [k for k, v in _pins.items() if v["cree_a"] < limite]:
        _pins.pop(pin_id, None)


# -- lecture ----------------------------------------------------------------

def current() -> dict[str, Any]:
    """Etat des reglages, secrets masques."""
    config = config_module.load()
    tmdb = config.get("tmdb", {})
    tracker = config.get("tracker", {})
    plex = config.get("plex", {})
    deluge = config.get("deluge", {})

    return {
        "configured": config_module.is_configured(config),
        "tmdb": {
            "has_key": bool(tmdb.get("api_key")),
            "language": tmdb.get("language", "fr-FR"),
            "region": tmdb.get("region", "FR"),
        },
        "streaming": {
            "my_services": config.get("streaming", {}).get("my_services", []),
        },
        "tracker": {
            "enabled": bool(tracker.get("enabled")),
            "name": tracker.get("name", ""),
            "base_url": tracker.get("base_url", ""),
            "has_key": bool(tracker.get("api_key")),
        },
        "deluge": {
            "enabled": bool(deluge.get("enabled")),
            "base_url": deluge.get("base_url", ""),
            "has_password": bool(deluge.get("password")),
            "client_cert": deluge.get("client_cert", ""),
            "client_key": deluge.get("client_key", ""),
            # Nom seulement : le contenu ne repart jamais vers le navigateur.
            "files": {
                role: certificates.current(role)
                for role in ("client", "client_key", "ca")
            },
            "has_key_password": bool(deluge.get("client_key_password")),
            "ca_cert": deluge.get("ca_cert", ""),
            "verify_tls": bool(deluge.get("verify_tls", True)),
            "add_paused": bool(deluge.get("add_paused", False)),
            "download_location": deluge.get("download_location", ""),
            "label": deluge.get("label", ""),
        },
        "plex": {
            "enabled": bool(plex.get("enabled")),
            "base_url": plex.get("base_url", ""),
            "has_token": bool(plex.get("token")),
            "verify_tls": bool(plex.get("verify_tls", True)),
            "sync_watched": bool(plex.get("sync_watched", True)),
        },
    }


# -- ecriture ---------------------------------------------------------------

def _pose(bloc: dict[str, Any], cle: str, valeur: Any, *, secret: bool = False) -> None:
    if valeur is None:
        return
    if secret and valeur == "":
        return           # champ laisse vide : on garde la valeur en place
    bloc[cle] = valeur


def update(payload: dict[str, Any]) -> dict[str, Any]:
    """Applique un formulaire partiel. Ne touche qu'aux cles fournies."""
    config = config_module.load_raw() or {}

    tmdb = payload.get("tmdb") or {}
    bloc = config.setdefault("tmdb", {})
    _pose(bloc, "api_key", tmdb.get("api_key"), secret=True)
    _pose(bloc, "language", tmdb.get("language") or None)
    _pose(bloc, "region", tmdb.get("region") or None)

    services = (payload.get("streaming") or {}).get("my_services")
    if isinstance(services, list):
        config.setdefault("streaming", {})["my_services"] = [
            int(s) for s in services if str(s).lstrip("-").isdigit()
        ]

    tracker = payload.get("tracker") or {}
    bloc = config.setdefault("tracker", {})
    _pose(bloc, "enabled", tracker.get("enabled"))
    _pose(bloc, "base_url", tracker.get("base_url") or None)
    _pose(bloc, "api_key", tracker.get("api_key"), secret=True)

    plex = payload.get("plex") or {}
    bloc = config.setdefault("plex", {})
    _pose(bloc, "enabled", plex.get("enabled"))
    _pose(bloc, "base_url", plex.get("base_url") or None)
    _pose(bloc, "token", plex.get("token"), secret=True)
    _pose(bloc, "verify_tls", plex.get("verify_tls"))
    _pose(bloc, "sync_watched", plex.get("sync_watched"))

    deluge = payload.get("deluge") or {}
    bloc = config.setdefault("deluge", {})
    _pose(bloc, "enabled", deluge.get("enabled"))
    _pose(bloc, "base_url", deluge.get("base_url") or None)
    _pose(bloc, "password", deluge.get("password"), secret=True)
    # Un chemin de certificat vide signifie « je n'en utilise pas » : c'est un
    # choix, pas un oubli. Il doit donc pouvoir etre efface, contrairement aux
    # secrets.
    for cle in ("client_cert", "client_key", "ca_cert", "download_location", "label"):
        if cle in deluge:
            bloc[cle] = deluge[cle]
    _pose(bloc, "client_key_password", deluge.get("client_key_password"), secret=True)
    _pose(bloc, "verify_tls", deluge.get("verify_tls"))
    _pose(bloc, "add_paused", deluge.get("add_paused"))

    config_module.save(config)
    return current()


# -- connexion Plex ---------------------------------------------------------

def plex_login_start() -> dict[str, Any]:
    """Cree une demande d'approbation et renvoie l'adresse a ouvrir."""
    config = config_module.load_raw() or {}
    client_id = (config.get("plex") or {}).get("client_id") or plex_auth.new_client_id()

    pin = plex_auth.request_pin(client_id)
    with _pins_lock:
        _prune()
        _pins[int(pin["id"])] = {"client_id": client_id, "cree_a": time.time()}

    # L'identifiant d'installation doit survivre : Plex l'associe a l'appareil.
    config.setdefault("plex", {})["client_id"] = client_id
    config_module.save(config)

    return {"id": pin["id"], "url": pin["url"]}


def plex_login_poll(pin_id: int) -> dict[str, Any]:
    """Regarde si l'utilisateur a approuve ; si oui, liste ses serveurs."""
    with _pins_lock:
        demande = _pins.get(int(pin_id))
    if not demande:
        raise plex_auth.PlexAuthError("Demande inconnue ou expiree. Relance la connexion.")

    token = plex_auth.check_pin(demande["client_id"], int(pin_id))
    if not token:
        return {"status": "attente"}

    demande["token"] = token
    serveurs = plex_auth.list_servers(demande["client_id"], token)
    return {
        "status": "connecte",
        "servers": [
            {"index": i, "name": s["name"], "owned": s["owned"],
             "connections": len(s["connections"])}
            for i, s in enumerate(serveurs)
        ],
        # Conserve cote serveur : les jetons ne transitent pas par la page.
        "_stored": bool(demande.setdefault("servers", serveurs)),
    }


def plex_login_finish(pin_id: int, index: int) -> dict[str, Any]:
    """Teste les adresses du serveur choisi et enregistre celle qui repond."""
    with _pins_lock:
        demande = _pins.get(int(pin_id))
    if not demande or "servers" not in demande:
        raise plex_auth.PlexAuthError("Demande expiree. Relance la connexion.")

    serveurs = demande["servers"]
    if not 0 <= index < len(serveurs):
        raise plex_auth.PlexAuthError("Serveur inconnu.")

    essais: list[dict[str, Any]] = []
    joignable = plex_auth.probe_connections(
        serveurs[index],
        on_try=lambda uri, ok: essais.append({"uri": uri, "ok": ok}),
    )
    if not joignable:
        return {
            "status": "injoignable",
            "attempts": essais,
            "message": "Aucune adresse de ce serveur ne repond depuis Trouveur.",
        }

    config = config_module.load_raw() or {}
    bloc = config.setdefault("plex", {})
    bloc["enabled"] = True
    bloc["base_url"] = joignable["base_url"]
    bloc["token"] = joignable["token"]
    bloc["verify_tls"] = joignable["verify_tls"]
    bloc["client_id"] = demande["client_id"]
    bloc.setdefault("sync_watched", True)
    config_module.save(config)

    with _pins_lock:
        _pins.pop(int(pin_id), None)

    voie = "relais Plex" if joignable["relay"] else (
        "reseau local" if joignable["local"] else "acces distant")
    return {
        "status": "connecte",
        "server": serveurs[index]["name"],
        "base_url": joignable["base_url"],
        "route": voie,
        "attempts": essais,
    }

"""Connexion a Plex par code PIN (le « Se connecter avec Plex » officiel).

Le mot de passe est saisi sur la page de Plex, dans le navigateur : ni Trouveur
ni son serveur local ne le voient jamais. Plex renvoie ensuite un jeton, puis la
liste des serveurs du compte avec leurs adresses de connexion — de quoi remplir
config.json sans rien recopier a la main.

Deroulement :
  1. creation d'un PIN            POST  https://plex.tv/api/v2/pins
  2. l'utilisateur approuve       https://app.plex.tv/auth#?code=...
  3. recuperation du jeton        GET   https://plex.tv/api/v2/pins/<id>
  4. decouverte des serveurs      GET   https://plex.tv/api/v2/resources
"""

from __future__ import annotations

import ssl
import time
import urllib.parse
import uuid
from typing import Any, Callable

from .http_client import HttpError, get_json, post_json, register_secret

PINS_URL = "https://plex.tv/api/v2/pins"
RESOURCES_URL = "https://plex.tv/api/v2/resources"
AUTH_PAGE = "https://app.plex.tv/auth#?"

PRODUCT = "Trouveur"
DEVICE = "Trouveur (local)"
PLATFORM = "Python"

# Un PIN Plex expire au bout de quelques minutes.
POLL_INTERVAL = 2
POLL_TIMEOUT = 300


class PlexAuthError(RuntimeError):
    pass


def new_client_id() -> str:
    """Identifiant stable de cette installation, attendu par l'API Plex."""
    return str(uuid.uuid4())


def _headers(client_id: str, token: str | None = None) -> dict[str, str]:
    headers = {
        "X-Plex-Product": PRODUCT,
        "X-Plex-Version": "1.0",
        "X-Plex-Client-Identifier": client_id,
        "X-Plex-Device": DEVICE,
        "X-Plex-Platform": PLATFORM,
        "Accept": "application/json",
    }
    if token:
        headers["X-Plex-Token"] = token
    return headers


def request_pin(client_id: str) -> dict[str, Any]:
    """Cree un PIN et renvoie son identifiant, son code et l'URL a ouvrir."""
    url = PINS_URL + "?strong=true"
    try:
        payload = post_json(url, headers=_headers(client_id), timeout=20)
    except HttpError as exc:
        raise PlexAuthError("Plex n'a pas accepte la demande de connexion : %s" % exc) from exc

    if not isinstance(payload, dict) or not payload.get("id") or not payload.get("code"):
        raise PlexAuthError("Reponse inattendue de Plex a la creation du PIN.")

    code = payload["code"]
    query = urllib.parse.urlencode({
        "clientID": client_id,
        "code": code,
        "context[device][product]": PRODUCT,
        "context[device][deviceName]": DEVICE,
    })
    return {"id": payload["id"], "code": code, "url": AUTH_PAGE + query}


def check_pin(client_id: str, pin_id: int) -> str | None:
    """Un seul coup d'oeil, sans attendre : c'est le navigateur qui rythme.

    Renvoie le jeton si l'utilisateur a approuve, None sinon.
    """
    try:
        payload = get_json(
            "%s/%s" % (PINS_URL, pin_id), headers=_headers(client_id), timeout=15
        )
    except HttpError as exc:
        if exc.status == 404:
            raise PlexAuthError("Demande expiree. Relance la connexion.") from exc
        raise PlexAuthError(str(exc)) from exc

    token = (payload or {}).get("authToken")
    if token:
        register_secret(token)
    return token


def poll_for_token(
    client_id: str,
    pin_id: int,
    timeout: int = POLL_TIMEOUT,
    on_tick: Callable[[int], None] | None = None,
) -> str | None:
    """Attend l'approbation. Renvoie le jeton, ou None si le delai expire."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            payload = get_json(
                "%s/%s" % (PINS_URL, pin_id), headers=_headers(client_id), timeout=15
            )
        except HttpError as exc:
            # Un PIN consomme ou expire renvoie 404 : inutile d'insister.
            if exc.status == 404:
                return None
            raise PlexAuthError(str(exc)) from exc

        token = (payload or {}).get("authToken")
        if token:
            register_secret(token)
            return token

        if on_tick:
            on_tick(int(deadline - time.time()))
        time.sleep(POLL_INTERVAL)
    return None


def list_servers(client_id: str, token: str) -> list[dict[str, Any]]:
    """Serveurs Plex accessibles au compte, avec leurs adresses de connexion."""
    url = RESOURCES_URL + "?includeHttps=1&includeRelay=1"
    try:
        payload = get_json(url, headers=_headers(client_id, token), timeout=25)
    except HttpError as exc:
        raise PlexAuthError("Impossible de lister tes serveurs Plex : %s" % exc) from exc

    servers = []
    for resource in payload or []:
        if "server" not in (resource.get("provides") or ""):
            continue
        register_secret(resource.get("accessToken"))
        servers.append({
            "name": resource.get("name") or "Serveur sans nom",
            "client_identifier": resource.get("clientIdentifier"),
            "access_token": resource.get("accessToken") or token,
            "owned": bool(resource.get("owned")),
            "connections": [
                {
                    "uri": c.get("uri"),
                    "local": bool(c.get("local")),
                    "relay": bool(c.get("relay")),
                    "protocol": c.get("protocol"),
                }
                for c in (resource.get("connections") or [])
                if c.get("uri")
            ],
        })
    return servers


def probe_connections(
    server: dict[str, Any],
    timeout: int = 6,
    on_try: Callable[[str, bool], None] | None = None,
) -> dict[str, Any] | None:
    """Essaie les adresses du serveur et retient la premiere qui repond.

    Ordre volontaire : local d'abord (rapide), puis distant, le relais en
    dernier car il est bride par Plex.
    """
    def rank(connection: dict[str, Any]) -> tuple:
        return (connection["relay"], not connection["local"])

    for connection in sorted(server["connections"], key=rank):
        uri = connection["uri"]
        # Les certificats plex.direct sont valides ; une IP nue ne l'est pas.
        context = ssl._create_unverified_context() if uri.startswith("https") else None
        ok = False
        try:
            payload = get_json(
                uri.rstrip("/") + "/identity",
                headers={"X-Plex-Token": server["access_token"], "Accept": "application/json"},
                timeout=timeout,
                ssl_context=context,
            )
            # La presence de la cle suffit : un MediaContainer vide reste une
            # reponse Plex valide, et bool({}) vaut faux.
            ok = isinstance(payload, dict) and "MediaContainer" in payload
        except HttpError:
            ok = False

        if on_try:
            on_try(uri, ok)
        if ok:
            return {
                "base_url": uri.rstrip("/"),
                "token": server["access_token"],
                # Une IP nue en https impose de ne pas verifier le certificat.
                "verify_tls": ".plex.direct" in uri or uri.startswith("http://"),
                "relay": connection["relay"],
                "local": connection["local"],
            }
    return None

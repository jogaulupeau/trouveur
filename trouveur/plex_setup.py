"""Assistant en ligne de commande : « python server.py --plex-login ».

Enchaine la connexion Plex, la decouverte des serveurs du compte et l'ecriture
de config.json. Rien n'est ecrit avant qu'une adresse ait effectivement repondu.
"""

from __future__ import annotations

import collections
import json
import sys
import webbrowser
from typing import Any

from . import config as config_module
from . import plex_auth


def _read_config_raw() -> collections.OrderedDict:
    with open(config_module.CONFIG_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh, object_pairs_hook=collections.OrderedDict)


def _write_config_raw(data: collections.OrderedDict) -> None:
    tmp = config_module.CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    import os
    os.replace(tmp, config_module.CONFIG_PATH)


def _choose(servers: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not servers:
        return None
    if len(servers) == 1:
        print("  Un seul serveur trouve : %s" % servers[0]["name"])
        return servers[0]

    print("\n  Plusieurs serveurs sont accessibles :")
    for position, server in enumerate(servers, 1):
        proprio = "le tien" if server["owned"] else "partage avec toi"
        print("    %d. %s  (%s)" % (position, server["name"], proprio))

    while True:
        try:
            reponse = input("\n  Lequel utiliser ? [1-%d] " % len(servers)).strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if reponse.isdigit() and 1 <= int(reponse) <= len(servers):
            return servers[int(reponse) - 1]
        print("  Reponse invalide.")


def run() -> int:
    try:
        raw = _read_config_raw()
    except FileNotFoundError:
        print("config.json introuvable. Copie config.example.json d'abord.", file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print("config.json est un JSON invalide : %s" % exc, file=sys.stderr)
        return 1

    plex_config = raw.setdefault("plex", collections.OrderedDict())
    client_id = plex_config.get("client_id") or plex_auth.new_client_id()

    print("Connexion a Plex")
    print("----------------")
    try:
        pin = plex_auth.request_pin(client_id)
    except plex_auth.PlexAuthError as exc:
        print("  %s" % exc, file=sys.stderr)
        return 1

    print("\n  Ouvre cette page et approuve la connexion :\n")
    print("    %s\n" % pin["url"])
    print("  (ton mot de passe est saisi chez Plex ; Trouveur ne le voit jamais)")
    try:
        webbrowser.open(pin["url"])
    except Exception:  # noqa: BLE001 - l'URL reste affichee ci-dessus
        pass

    print("\n  En attente de ton approbation…")
    try:
        token = plex_auth.poll_for_token(client_id, pin["id"])
    except plex_auth.PlexAuthError as exc:
        print("  %s" % exc, file=sys.stderr)
        return 1

    if not token:
        print("  Delai depasse ou demande refusee. Relance la commande.", file=sys.stderr)
        return 1
    print("  Connecte.")

    print("\nRecherche de tes serveurs")
    print("-------------------------")
    try:
        servers = plex_auth.list_servers(client_id, token)
    except plex_auth.PlexAuthError as exc:
        print("  %s" % exc, file=sys.stderr)
        return 1

    server = _choose(servers)
    if not server:
        print("  Aucun serveur Plex utilisable sur ce compte.", file=sys.stderr)
        return 1

    print("\n  Test des adresses de %s :" % server["name"])

    def montrer(uri: str, ok: bool) -> None:
        print("    %-52s %s" % (uri[:52], "repond" if ok else "muette"))

    reachable = plex_auth.probe_connections(server, on_try=montrer)
    if not reachable:
        print(
            "\n  Aucune adresse ne repond depuis cette machine. Le serveur est"
            "\n  peut-etre eteint, ou injoignable depuis ce reseau.",
            file=sys.stderr,
        )
        return 1

    plex_config["enabled"] = True
    plex_config["base_url"] = reachable["base_url"]
    plex_config["token"] = reachable["token"]
    plex_config["client_id"] = client_id
    plex_config["verify_tls"] = reachable["verify_tls"]
    plex_config.setdefault("sections", [])
    plex_config.setdefault("timeout", 20)
    plex_config.setdefault("refresh_seconds", 600)
    raw["plex"] = plex_config
    _write_config_raw(raw)

    voie = "relais Plex" if reachable["relay"] else ("reseau local" if reachable["local"] else "acces distant")
    print("\n  Adresse retenue : %s  (%s)" % (reachable["base_url"], voie))
    if reachable["relay"]:
        print("  Le relais Plex est bride : une adresse directe serait plus rapide.")
    print("\nconfig.json est a jour. Redemarre le serveur : python server.py")
    return 0

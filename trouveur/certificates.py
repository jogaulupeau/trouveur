"""Depot des certificats envoyes depuis l'interface.

Un certificat client contient une cle privee. Trois regles en decoulent :

  - **il est ecrit avec des droits restreints** (0600) dans le volume de
    donnees, a cote de la configuration ;
  - **le nom du fichier n'est jamais celui fourni par le navigateur.** Il est
    impose par le role, ce qui elimine toute traversee de chemin ;
  - **aucune route ne le renvoie.** L'interface n'en connait que le nom et la
    taille.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from typing import Any

from .paths import certs_dir

# Un certificat depasse rarement quelques kilo-octets ; au-dela, ce n'en est
# pas un.
TAILLE_MAX = 256 * 1024

# Le nom d'origine, garde a cote pour l'affichage seulement. « client.p12 » ne
# dit pas quel fichier a ete depose ; « certificat-nas.p12 », si.
NOMS = ".noms.json"

ROLES = {
    "client": {
        "extensions": (".p12", ".pfx", ".pem", ".crt", ".cer"),
        "libelle": "certificat client",
    },
    "client_key": {
        "extensions": (".key", ".pem"),
        "libelle": "clé privée",
    },
    "ca": {
        "extensions": (".pem", ".crt", ".cer"),
        "libelle": "autorité de certification",
    },
}


class CertificateError(RuntimeError):
    pass


def _extension(nom: str, role: str) -> str:
    extension = os.path.splitext(nom or "")[1].lower()
    permises = ROLES[role]["extensions"]
    if extension not in permises:
        raise CertificateError(
            "Extension inattendue pour un %s : %s. Attendu : %s."
            % (ROLES[role]["libelle"], extension or "(aucune)", ", ".join(permises))
        )
    return extension


def store(role: str, filename: str, data_b64: str) -> dict[str, Any]:
    """Ecrit un certificat et renvoie son chemin."""
    if role not in ROLES:
        raise CertificateError("Type de certificat inconnu.")
    extension = _extension(filename, role)

    try:
        contenu = base64.b64decode(data_b64 or "", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CertificateError("Fichier illisible.") from exc
    if not contenu:
        raise CertificateError("Fichier vide.")
    if len(contenu) > TAILLE_MAX:
        raise CertificateError(
            "Fichier trop volumineux (%d Ko) : ce n'est pas un certificat."
            % (len(contenu) // 1024)
        )

    dossier = certs_dir()
    os.makedirs(dossier, exist_ok=True)
    try:
        os.chmod(dossier, 0o700)
    except OSError:
        pass

    # Le nom vient du role, jamais du navigateur : pas de traversee possible.
    cible = os.path.join(dossier, role + extension)
    _effacer_role(role, sauf=cible)

    tmp = cible + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(contenu)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, cible)
    try:
        os.chmod(cible, 0o600)
    except OSError:
        pass

    _noter_nom(role, os.path.basename(filename))
    return {"path": cible, "name": os.path.basename(cible),
            "label": os.path.basename(filename), "size": len(contenu)}


def _noms() -> dict[str, str]:
    try:
        with open(os.path.join(certs_dir(), NOMS), encoding="utf-8") as fh:
            donnees = json.load(fh)
        return donnees if isinstance(donnees, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _noter_nom(role: str, nom: str) -> None:
    """Purement decoratif : son absence ne doit jamais gener le chargement."""
    donnees = _noms()
    if nom:
        donnees[role] = nom
    else:
        donnees.pop(role, None)
    try:
        with open(os.path.join(certs_dir(), NOMS), "w", encoding="utf-8") as fh:
            json.dump(donnees, fh, ensure_ascii=False)
    except OSError:
        pass


def _effacer_role(role: str, sauf: str = "") -> None:
    """Un role ne garde qu'un fichier : deposer un .p12 doit retirer le .pem."""
    dossier = certs_dir()
    if not os.path.isdir(dossier):
        return
    for extension in ROLES[role]["extensions"]:
        chemin = os.path.join(dossier, role + extension)
        if chemin != sauf and os.path.isfile(chemin):
            try:
                os.unlink(chemin)
            except OSError:
                pass


def remove(role: str) -> bool:
    if role not in ROLES:
        raise CertificateError("Type de certificat inconnu.")
    avant = bool(current(role))
    _effacer_role(role)
    _noter_nom(role, "")
    return avant


def current(role: str) -> dict[str, Any] | None:
    """Le fichier depose pour ce role, s'il existe."""
    dossier = certs_dir()
    if role not in ROLES or not os.path.isdir(dossier):
        return None
    for extension in ROLES[role]["extensions"]:
        chemin = os.path.join(dossier, role + extension)
        if os.path.isfile(chemin):
            return {"path": chemin, "name": os.path.basename(chemin),
                    "label": _noms().get(role) or os.path.basename(chemin),
                    "size": os.path.getsize(chemin)}
    return None

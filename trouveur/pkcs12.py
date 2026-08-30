"""Lecture d'un certificat client au format PKCS#12 (.p12 / .pfx).

La bibliotheque standard ne sait pas ouvrir ce format : ssl.load_cert_chain
n'accepte que du PEM, et par des chemins de fichiers — pas de la memoire. On
delegue donc la conversion a openssl, presque toujours present.

Deux precautions, parce qu'il s'agit d'une cle privee :

  - **elle ne touche jamais le disque en clair.** openssl reecrit la cle en PEM
    en la laissant chiffree, avec la meme phrase de passe, qui est ensuite
    donnee a load_cert_chain ;
  - **les fichiers temporaires sont supprimes des le chargement.** Le contexte
    TLS garde la cle en memoire, les fichiers ne servent qu'a la lui passer.

La phrase de passe transite par l'environnement du processus openssl, jamais
par la ligne de commande, qui est lisible dans la liste des processus.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from typing import Iterator


class Pkcs12Error(RuntimeError):
    pass


def is_pkcs12(path: str) -> bool:
    return path.lower().endswith((".p12", ".pfx"))


def openssl_disponible() -> bool:
    return shutil.which("openssl") is not None


def _run(args: list[str], password: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, TROUVEUR_P12_PASS=password or "")
    return subprocess.run(
        ["openssl", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _extraire(source: str, password: str, sortie: str, quoi: str) -> None:
    """Extrait le certificat ou la cle. La cle reste chiffree en sortie."""
    commun = [
        "pkcs12", "-in", source, "-out", sortie,
        "-passin", "env:TROUVEUR_P12_PASS",
    ]
    if quoi == "cert":
        args = commun + ["-clcerts", "-nokeys"]
    else:
        # Pas de -nodes : la cle sort chiffree, avec la meme phrase de passe.
        args = commun + ["-nocerts", "-passout", "env:TROUVEUR_P12_PASS"]

    resultat = _run(args, password)
    if resultat.returncode != 0:
        # OpenSSL 3 refuse par defaut les chiffrements anciens (RC2, 40 bits)
        # que produisent encore beaucoup d'outils : -legacy les reactive.
        resultat = _run(args + ["-legacy"], password)

    if resultat.returncode != 0:
        erreur = (resultat.stderr or "").strip().splitlines()
        detail = erreur[-1] if erreur else "cause inconnue"
        if "mac verify" in detail.lower() or "invalid password" in detail.lower():
            raise Pkcs12Error(
                "Phrase de passe refusée par le fichier PKCS#12."
            )
        raise Pkcs12Error("Lecture du PKCS#12 impossible : %s" % detail)

    if not os.path.isfile(sortie) or os.path.getsize(sortie) == 0:
        raise Pkcs12Error(
            "Le fichier PKCS#12 ne contient pas de %s exploitable."
            % ("certificat client" if quoi == "cert" else "clé privée")
        )


@contextmanager
def pem_temporaire(source: str, password: str) -> Iterator[tuple[str, str]]:
    """Fournit (certificat, cle) en PEM, le temps de les charger.

    Les fichiers sont crees avec des droits restreints et supprimes en sortant,
    que le chargement ait reussi ou non.
    """
    if not os.path.isfile(source):
        raise Pkcs12Error("Fichier PKCS#12 introuvable : %s" % source)
    if not openssl_disponible():
        raise Pkcs12Error(
            "openssl est introuvable : impossible de lire un fichier PKCS#12. "
            "Convertis-le en PEM, ou installe openssl."
        )

    dossier = tempfile.mkdtemp(prefix="trouveur-p12-")
    try:
        os.chmod(dossier, 0o700)
    except OSError:
        pass

    cert = os.path.join(dossier, "client.crt")
    cle = os.path.join(dossier, "client.key")
    try:
        _extraire(source, password, cert, "cert")
        _extraire(source, password, cle, "key")
        for chemin in (cert, cle):
            try:
                os.chmod(chemin, 0o600)
            except OSError:
                pass
        yield cert, cle
    finally:
        shutil.rmtree(dossier, ignore_errors=True)

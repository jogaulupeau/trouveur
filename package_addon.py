#!/usr/bin/env python3
"""Assemble le dossier de l'add-on Home Assistant.

Copie le code dans addon/app/, d'ou le Dockerfile le prend. Rien d'autre : ni
config.json, ni les listes, ni le cache. Une image de conteneur se partage et
se reconstruit — y enfermer une cle d'API ou un historique de visionnage serait
une fuite, et une donnee perdue a la prochaine mise a jour.

    python package_addon.py
"""

from __future__ import annotations

import os
import shutil
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
CIBLE = os.path.join(ROOT, "addon", "app")

# Ce qui constitue l'application.
A_COPIER = [
    ("server.py", "fichier"),
    ("trouveur", "dossier"),
    ("web", "dossier"),
    ("config.example.json", "fichier"),
]

# Ce qui ne doit jamais entrer dans l'image.
INTERDITS = (
    "config.json", "seen.json", "watchlist.json", "ignored.json",
    ".cache", "__pycache__", ".backup", ".corrompu", ".tmp", ".avant-",
)


def _interdit(nom: str) -> bool:
    return any(marqueur in nom for marqueur in INTERDITS)


def _copier_dossier(source: str, destination: str) -> list[str]:
    copies = []
    for racine, dossiers, fichiers in os.walk(source):
        dossiers[:] = [d for d in dossiers if not _interdit(d)]
        for fichier in fichiers:
            if _interdit(fichier):
                continue
            chemin = os.path.join(racine, fichier)
            relatif = os.path.relpath(chemin, source)
            arrivee = os.path.join(destination, relatif)
            os.makedirs(os.path.dirname(arrivee), exist_ok=True)
            shutil.copy2(chemin, arrivee)
            copies.append(relatif)
    return copies


def main() -> int:
    # On ne vide que le dossier d'assemblage, jamais les sources ni les donnees.
    if os.path.isdir(CIBLE):
        shutil.rmtree(CIBLE)
    os.makedirs(CIBLE)

    total = 0
    for nom, genre in A_COPIER:
        source = os.path.join(ROOT, nom)
        if not os.path.exists(source):
            print("Absent, ignore : %s" % nom, file=sys.stderr)
            continue
        if genre == "dossier":
            copies = _copier_dossier(source, os.path.join(CIBLE, nom))
            print("  %-22s %d fichiers" % (nom + "/", len(copies)))
            total += len(copies)
        else:
            shutil.copy2(source, os.path.join(CIBLE, nom))
            print("  %-22s 1 fichier" % nom)
            total += 1

    # Verification : aucune donnee personnelle n'a suivi.
    fuites = []
    for racine, _, fichiers in os.walk(CIBLE):
        for fichier in fichiers:
            if _interdit(fichier):
                fuites.append(os.path.relpath(os.path.join(racine, fichier), CIBLE))
    if fuites:
        print("\nARRET : des fichiers personnels ont ete copies : %s" % fuites,
              file=sys.stderr)
        return 1

    print("\n%d fichiers assembles dans addon/app/" % total)
    print("Aucune cle ni liste embarquee : elles vivront dans /data.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

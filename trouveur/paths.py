"""Ou vivent les donnees : a cote du code, ou dans un volume dedie.

En usage local, tout reste dans le dossier du projet — c'est le plus simple a
inspecter et a sauvegarder. En conteneur (add-on Home Assistant), le code est
remplace a chaque mise a jour : les donnees doivent alors vivre ailleurs, dans
un volume persistant designe par TROUVEUR_DATA_DIR.

Chaque fichier garde en plus sa propre variable d'environnement, pour les tests
et pour les installations qui rangent leurs donnees a leur facon.
"""

from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def data_dir() -> str:
    """Vide = les donnees restent a cote du code."""
    return os.environ.get("TROUVEUR_DATA_DIR") or ROOT


def data_file(name: str, env_var: str) -> str:
    """Chemin d'un fichier de donnees, surchargeable individuellement.

    Resolu a l'appel, jamais fige a l'import : un module importe tot (config)
    figerait sinon les chemins reels, et un test qui pose sa variable ensuite
    ecrirait dans les vraies donnees. C'est arrive une fois, cela suffit.
    """
    return os.environ.get(env_var) or os.path.join(data_dir(), name)


def config_path() -> str:
    return data_file("config.json", "TROUVEUR_CONFIG_PATH")


def cache_dir() -> str:
    return data_file(".cache", "TROUVEUR_CACHE_DIR")


def seen_path() -> str:
    return data_file("seen.json", "TROUVEUR_SEEN_PATH")


def watchlist_path() -> str:
    return data_file("watchlist.json", "TROUVEUR_WATCHLIST_PATH")


def ignored_path() -> str:
    return data_file("ignored.json", "TROUVEUR_IGNORED_PATH")


def certs_dir() -> str:
    """Certificats deposes depuis l'interface.

    Dans le volume de donnees : ils doivent survivre aux mises a jour, au meme
    titre que la configuration.
    """
    return data_file("certs", "TROUVEUR_CERTS_DIR")

# L'exemple accompagne le code, pas les donnees.
EXAMPLE_PATH = os.path.join(ROOT, "config.example.json")


def ensure_data_dir() -> None:
    if data_dir() != ROOT:
        os.makedirs(data_dir(), exist_ok=True)

# Tests

Sans dépendance : `python tests/<fichier>.py`. Chaque fichier affiche une ligne
par vérification et sort en code 1 à la première qui échoue.

| Fichier | Couvre |
|---|---|
| `test_tracker.py` | Adaptateur Torznab : profils tr4ker.net / v3x.club, authentification, provenance du lien de téléchargement, non-divulgation de la clé d'API, bascule d'un tracker à l'autre, diagnostic. |

Aucun test ne sort sur le réseau : les serveurs distants sont simulés en local.
Aucun n'écrit dans les vraies listes — celles-ci vivent dans le dossier de
données, que les tests ne touchent pas.

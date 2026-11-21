# Tests

Sans dépendance : `python tests/<fichier>.py`. Chaque fichier affiche une ligne
par vérification et sort en code 1 à la première qui échoue.

| Fichier | Couvre |
|---|---|
| `test_tracker.py` | Adaptateur Torznab : profils tr4ker.net / v3x.club, authentification, provenance du lien de téléchargement, non-divulgation de la clé d'API, bascule d'un tracker à l'autre, diagnostic. |
| `test_homeassistant.py` | Le lancement sur un lecteur de Home Assistant, contre un faux Home Assistant local : lecteurs listés, commande envoyée selon la méthode (plex, lien, app), confirmation quand le lecteur est occupé, lecteur indisponible, jeton absent des erreurs. |
| `test_certification.py` | Le filtre de public : le plafond part vers TMDB avec le pays de la région, la liste des classements vient de TMDB, une valeur louche n'est jamais transmise. |
| `test_addon_options.py` | Les options de l'add-on Home Assistant ne défont pas ce que la page a choisi au redémarrage suivant. |

Aucun test ne sort sur le réseau : les serveurs distants sont simulés en local.
Aucun n'écrit dans les vraies listes — celles-ci vivent dans le dossier de
données, que les tests ne touchent pas.

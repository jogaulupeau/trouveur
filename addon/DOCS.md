# Trouveur — add-on Home Assistant

Trouveur propose des films selon vos critères et indique où les regarder :
votre serveur Plex, vos plateformes de streaming, votre tracker.

L'interface apparaît dans la barre latérale de Home Assistant (ingress) : ni
port à ouvrir, ni mot de passe supplémentaire.

## Installation

1. Copier le dossier `addon/` dans `/addons/trouveur/` sur la machine Home
   Assistant — par le partage Samba (`\\homeassistant\addons\`) ou l'add-on
   « Advanced SSH & Web Terminal ».
2. **Paramètres → Modules complémentaires → Boutique**, menu `⋮` →
   **Vérifier les mises à jour**. « Trouveur » apparaît dans « Add-ons locaux ».
3. L'installer, remplir la configuration, démarrer.

Le dossier doit contenir `app/`, produit par `python package_addon.py` depuis
le dépôt. Ce script n'y copie que le code : ni clé, ni liste, ni cache.

## Configuration

| Option | Rôle |
| --- | --- |
| `tmdb_api_key` | **Obligatoire.** Clé v3 ou jeton v4 de [TMDB](https://www.themoviedb.org/settings/api). Sans elle l'add-on refuse de démarrer. |
| `tmdb_language`, `tmdb_region` | Langue des fiches et région pour les dates et les plateformes. |
| `my_services` | Identifiants TMDB de vos abonnements. Vide = tous les services. Netflix 8 · Prime Video 119 · Disney+ 337 · Apple TV+ 350 · Canal+ 381 · Paramount+ 531 |
| `tracker_*` | Indexeur Torznab : activation, adresse, clé d'API. |
| `plex_*` | Serveur Plex : activation, adresse, jeton, vérification TLS, reprise des films déjà vus. |

Les champs `tracker_api_key` et `plex_token` sont de type `password` : Home
Assistant les masque dans l'interface et les exclut des journaux.

**Une option laissée vide n'efface pas la valeur existante.** On peut donc
renseigner un jeton une fois, puis vider le champ sans le perdre.

## Vos données

Tout ce que l'add-on écrit va dans son volume `/data` : les trois listes, la
configuration et le cache. **Ce volume survit aux redémarrages et aux mises à
jour** de l'add-on — seul le code est remplacé.

Il est inclus dans les sauvegardes Home Assistant, partielles comme complètes.

### Reprendre des listes existantes

Si Trouveur tournait déjà ailleurs, ses listes se récupèrent au **premier
démarrage** :

1. Créer un dossier `trouveur` dans le partage `share` de Home Assistant
   (`\\homeassistant\share\trouveur\`).
2. Y déposer `seen.json`, `watchlist.json`, `ignored.json` — les trois ou
   seulement certains.
3. Démarrer l'add-on. Le journal indique le nombre de films repris.

La reprise est **strictement additive** :

- un fichier déjà présent dans `/data` n'est **jamais** remplacé ;
- rien n'est supprimé ni modifié dans `share`, monté en lecture seule ;
- un fichier illisible est signalé dans le journal et ignoré, sans empêcher le
  démarrage.

Les fichiers peuvent donc rester dans `share` : après la première reprise ils
sont simplement ignorés.

### Connexion Plex sans recopier de jeton

Le flux OAuth de Plex (`--plex-login`) demande un terminal. Deux possibilités :

- lancer Trouveur une fois en local, faire `python server.py --plex-login`,
  puis reporter `plex_base_url` et `plex_token` dans les options ;
- ou coller directement le jeton relevé dans l'interface web de Plex.

L'identifiant d'installation (`plex.client_id`) écrit par cette connexion est
conservé d'un démarrage à l'autre : les options ne l'écrasent pas.

## Journal

`log_level` règle la verbosité. La synchronisation Plex et la reprise des
listes annoncent ce qu'elles font au démarrage.

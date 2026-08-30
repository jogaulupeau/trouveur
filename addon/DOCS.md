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

Deux endroits, au choix : les options ci-dessous, ou l'écran **Réglages** dans
l'interface de l'add-on. Rien n'est obligatoire pour démarrer — sans clé TMDB
l'application s'ouvre directement sur son écran de configuration.

Les options de Home Assistant sont une **amorce, pas une remise à zéro** : une
case décochée ne désactive pas ce que l'interface a activé, et un champ vide
n'efface pas une valeur en place. On peut donc tout laisser vide ici et tout
régler depuis l'interface.

| Option | Rôle |
| --- | --- |
| `tmdb_api_key` | Clé v3 ou jeton v4 de [TMDB](https://www.themoviedb.org/settings/api). Peut aussi être saisie dans l'écran **Réglages** de l'interface. |
| `tmdb_language`, `tmdb_region` | Langue des fiches et région pour les dates et les plateformes. |
| `my_services` | Identifiants TMDB de vos abonnements. Se règle plus simplement dans **Réglages → Mes abonnements**, avec les logos. Vide = tous les services. |
| `tracker_*` | Indexeur Torznab : activation, adresse, clé d'API. |
| `deluge_*` | Serveur Deluge : activation, adresse, mot de passe. Se règle aussi dans **Réglages**, où le certificat client se dépose directement. |
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

Le plus simple : dans l'interface, **Réglages** → **Mes listes**, et choisir un
ou plusieurs fichiers `seen.json`, `watchlist.json`, `ignored.json`. La liste
visée est déduite du nom du fichier.

L'import est **additif** : un film déjà présent garde sa date et sa provenance,
rien n'est remplacé ni supprimé. Réimporter deux fois la même sauvegarde est
donc sans effet.

L'autre voie, utile pour amorcer une installation neuve, passe par le dossier
partagé au **premier démarrage** :

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

### Connexion Plex

Dans l'interface, **Réglages** → **Se connecter avec Plex**. La page
d'approbation de Plex s'ouvre, tes identifiants sont saisis chez eux, et tes
serveurs sont ensuite découverts avec leurs adresses. Aucun jeton à recopier,
aucun terminal.

L'identifiant d'installation (`plex.client_id`) est conservé d'un démarrage à
l'autre : les options ne l'écrasent pas, il n'y a donc pas à se reconnecter.

Le bouton **Synchroniser les « déjà vus » maintenant** reporte immédiatement
les films lus sur Plex, sans attendre le prochain démarrage. Comme la
synchronisation automatique, il n'ajoute que l'inédit et ne retire jamais rien.

## Deluge

Un bouton **Deluge** apparaît sur chaque torrent de la fiche d'un film et lance
le téléchargement sur le serveur.

Trouveur récupère le `.torrent` auprès du tracker avec sa propre clé, puis le
transmet à Deluge en base64 (`core.add_torrent_file`). **Deluge n'a donc besoin
ni de la clé du tracker, ni d'un accès au tracker.**

### Certificat client

Si l'accès à Deluge est protégé par un certificat client, **le déposer depuis
Réglages** : le bouton de sélection de fichier l'envoie directement à l'add-on.
Rien à copier dans `/share`, aucun chemin à saisir.

Les formats **PKCS#12 (`.p12`, `.pfx`) et PEM** sont acceptés. Un `.p12`
protégé par phrase de passe n'a pas à être converti : le déposer et renseigner
sa phrase de passe suffit, le champ « clé privée » reste vide.

Le fichier est rangé dans `/data/certs`, sous un nom imposé par son rôle, avec
des droits restreints — il **survit donc aux mises à jour** de l'add-on, comme
la configuration. Son contenu n'est jamais renvoyé au navigateur : l'écran de
réglages n'en affiche que le nom, avec un bouton **Retirer**.

La clé privée n'est **jamais écrite en clair** : la conversion interne la laisse
chiffrée, et les fichiers temporaires sont supprimés dès que le contexte TLS
les a lus.

### Si le serveur présente un certificat privé

Un échec « unable to get local issuer certificate » ne vient **pas** du
certificat client : il dit que Trouveur ne reconnaît pas l'autorité qui a signé
le certificat du **serveur** Deluge. Le diagnostic nomme alors cette autorité —
déposer son certificat dans « Autorité de certification » suffit.

À défaut, l'interrupteur **Vérifier le certificat du serveur** permet de passer
outre. La liaison reste chiffrée, mais Trouveur ne vérifie plus à qui il parle :
à ne décocher que sur un réseau de confiance.

Le bouton **Tester la connexion** déroule le diagnostic étape par étape :
lecture du certificat, négociation TLS, authentification, liaison au démon.

## Journal

`log_level` règle la verbosité. La synchronisation Plex et la reprise des
listes annoncent ce qu'elles font au démarrage.

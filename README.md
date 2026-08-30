# Trouveur

Application locale qui propose des films selon vos critères (genre, époque, note,
durée, langue, thème), affiche une fiche complète en français — synopsis, notes,
bande-annonce, distribution, liens IMDb / SensCritique / Allociné / Letterboxd —
et indique si des torrents sont disponibles sur votre tracker.

Aucune dépendance : uniquement Python 3.9+ et sa bibliothèque standard.
Les clés d'API restent côté serveur, le navigateur ne les voit jamais.

## Démarrage

1. Copier la configuration et renseigner les clés :

```bash
cp config.example.json config.json
```

2. Ouvrir `config.json` et remplacer `COLLE_TA_CLE_TMDB_ICI` par votre clé
   [TMDB](https://www.themoviedb.org/settings/api) (clé v3 de 32 caractères ou
   jeton v4 commençant par `eyJ` — les deux fonctionnent).

3. Lancer :

```bash
python server.py
```

Le navigateur s'ouvre sur <http://127.0.0.1:8777/>.

Options : `--port 9000`, `--host 0.0.0.0`, `--no-browser`, `--clear-cache`,
`--plex-login`.

## Le tracker

tr4ker expose une **API Torznab** standard sur `/api`. L'adaptateur cible ce
standard : il fonctionne donc tel quel avec n'importe quel indexeur Torznab
(Prowlarr, Jackett, autre tracker) en changeant `base_url`.

```jsonc
"tracker": {
  "enabled": true,
  "base_url": "https://tr4ker.net",
  "api_path": "/api",
  "api_key": "votre-cle",
  "auth": "header",            // ou "query" pour envoyer ?apikey=
  "header_name": "X-Api-Key",
  "movie_category": "2000",
  "limit": 100
}
```

La recherche interroge d'abord `t=movie&tmdbid=<id>` : le rapprochement est
**exact**, sans ambiguïté de titre. Si le tracker ne connaît pas ce tmdbid, elle
retombe sur une recherche par titre — mais ce repli est bruité (le tracker
n'applique pas le filtre `cat`), donc les résultats sont alors filtrés sur le
nom de la release et sur l'année, et l'interface signale explicitement que le
rapprochement s'est fait par titre.

La résolution, la source, le codec, la langue et le statut freeleech sont
déduits du nom de la release et affichés sous forme d'étiquettes.

**La clé ne sort jamais du serveur.** Le fichier `.torrent` n'est pas servi par
un lien direct vers le tracker (qui exigerait la clé dans l'URL de la page) :
le bouton pointe vers `/api/torrent?slug=…`, et le serveur local relaie la
requête en ajoutant l'en-tête d'authentification.

Tant que `tracker.enabled` vaut `false`, l'application fonctionne normalement,
simplement sans les pastilles de disponibilité.

## Fonctionnement

```
server.py              serveur HTTP local + routage
trouveur/config.py     lecture et validation de config.json
trouveur/tmdb.py       client TMDB (découverte, fiches, genres)
trouveur/tracker.py    adaptateur Torznab (recherche + proxy .torrent)
trouveur/http_client.py appels JSON, masquage des clés dans les erreurs
trouveur/cache.py      cache disque (.cache/)
trouveur/seen.py       listes « vus » et « a voir » (seen.json, watchlist.json)
trouveur/reco.py       suggestions baties sur l'historique
trouveur/plex.py       index de la bibliotheque Plex
trouveur/plex_auth.py  connexion Plex par code PIN (OAuth)
trouveur/plex_setup.py assistant --plex-login
web/                   interface : une page, un script, une feuille de style
trouveur/paths.py      ou vivent les donnees (local ou volume dedie)
addon/                 paquet Home Assistant
package_addon.py       assemble addon/app/
```

Routes internes : `/api/bootstrap`, `/api/discover`, `/api/movie/<id>`,
`/api/search`, `/api/similar`, `/api/foryou`, `/api/collection`,
`/api/providers`, `/api/availability`,
`/api/torrents`, `/api/torrent` (relais du fichier .torrent),
`/api/seen`, `/api/watchlist` et `/api/ignored` (GET, POST, DELETE), `/api/plex`,
`/api/plex/sync`.

Les réponses TMDB sont mises en cache 24 h, celles du tracker 15 min. `python
server.py --clear-cache` vide le tout.

## Plateformes de streaming

Le panneau propose un filtre **Disponible sur** : Netflix, Disney+, Prime Video…
et, en tête de liste, **ton serveur Plex**. L'intention est qu'ils soient au même
niveau — la question posée est « où puis-je regarder ce film ce soir ? », et ton
serveur est une réponse comme une autre.

Techniquement ils ne le sont pas, et cela change le coût de la requête :

- **Plateformes seules** — le filtre part dans la requête TMDB
  (`with_watch_providers`, abonnement uniquement). Exact, aucun appel
  supplémentaire, et le total affiché est juste.
- **Plex seul** — l'index de la bibliothèque est déjà en mémoire, le filtre ne
  coûte rien non plus.
- **Les deux** — TMDB ne sait rien de ton serveur, l'union ne peut donc pas
  s'exprimer dans sa requête. Le tri se fait après coup, ce qui demande de
  connaître les plateformes de chaque film : un appel par film, mis en cache
  24 h. Comptez une demi-seconde de plus la première fois.

Quand le filtrage a lieu après la requête, le nombre total de films
correspondants n'est plus connu : l'interface affiche « 20 propositions » au
lieu d'un total emprunté à TMDB qui ne correspondrait plus à ce qui est montré.

### Tes abonnements

`streaming.my_services` dans `config.json` restreint la liste aux services
auxquels tu es réellement abonné : eux seuls apparaissent dans le filtre et en
badge. Un badge « Canal+ » quand on n'y est pas abonné n'apprend rien.

```jsonc
"streaming": { "my_services": [8, 119, 337] }   // Netflix, Prime Video, Disney+
```

Laisser la liste vide propose tous les services de la région. **Ton serveur Plex
est indépendant de ce réglage** : il est toujours proposé et toujours badgé.

### Badges sur les affiches

Chaque affiche porte des badges « où regarder » : le serveur Plex d'abord (en
orange, disponible tout de suite), puis les logos des plateformes, trois au
maximum. Ils se remplissent après l'affichage de la grille, car ils coûtent un
appel TMDB par film — mis en cache 24 h.

TMDB liste chaque *offre commerciale* séparément : « Netflix » et « Netflix
Standard with Ads », « Paramount Plus », « Paramount+ Amazon Channel » et
« Paramount Plus Premium ». Trois badges pour un seul service n'apprennent rien,
donc les variantes d'un même service sont fusionnées et seule l'offre principale
est retenue.

Un piège évité : TMDB expose son propre fournisseur « Plex » (identifiant 538),
qui est son service de chaînes gratuites et n'a rien à voir avec ton serveur. Il
est retiré de la liste pour ne pas prêter à confusion.

## Serveur Plex

Une pastille orange sur l'affiche indique que le film est déjà dans ta
bibliothèque Plex, et la fiche détaille la version disponible (résolution,
codec, conteneur, taille) avec un lien pour l'ouvrir dans Plex.

### Connexion

Rien à recopier à la main :

```bash
python server.py --plex-login
```

La commande utilise le « Se connecter avec Plex » officiel (flux OAuth par code
PIN) : elle ouvre la page de Plex dans ton navigateur, **tu saisis tes
identifiants chez Plex** — ni Trouveur ni son serveur local ne les voient — puis
Plex renvoie un jeton. La commande liste ensuite les serveurs de ton compte, te
laisse choisir si tu en as plusieurs, teste leurs adresses (réseau local
d'abord, relais en dernier car il est bridé) et écrit dans `config.json`
l'adresse qui répond, le jeton du serveur et le réglage TLS adapté.

Rien n'est écrit tant qu'aucune adresse n'a effectivement répondu.

Le bloc obtenu reste modifiable à la main si besoin :

```jsonc
"plex": {
  "enabled": true,
  "base_url": "https://192-168-1-20.abc.plex.direct:32400",
  "token": "jeton-du-serveur",
  "client_id": "identifiant-de-cette-installation",
  "sections": [],          // vide = toutes les bibliothèques de type film
  "verify_tls": true,      // false si certificat auto-signé
  "refresh_seconds": 600
}
```

Plex conserve l'identifiant TMDB de chaque film matché, donc l'appariement est
exact — les deux formes sont lues, celle de l'agent actuel (`tmdb://335984`) et
celle des anciens agents (`com.plexapp.agents.themoviedb://335984`). Un film mal
matché, sans identifiant, est rapproché par titre et année, et l'interface le
signale.

La bibliothèque entière est chargée une fois puis indexée en mémoire pour la
durée de `refresh_seconds` : répondre pour les vingt films d'une grille ne coûte
alors **aucun** appel réseau. Comme pour les autres services, le jeton ne quitte
jamais le serveur — il n'apparaît ni dans la page ni dans les liens.

## Recherche par titre

Le champ en haut de page cherche un film par son nom, en français. Les résultats
s'affichent dans la même grille que les propositions : disponibilité Plex,
torrents, marquage et fiche complète fonctionnent à l'identique. « Revenir aux
critères » repasse en mode découverte.

L'ordre de pertinence de TMDB est conservé tel quel — reclasser par note ferait
remonter des documentaires obscurs devant le film cherché.

## Pour toi

L'onglet **Pour toi** construit des suggestions à partir des films déjà vus.
TMDB sait dire « les spectateurs de ce film ont aussi aimé… » mais ne sait rien
de vous ; ce module fait le pont.

Le principe : jusqu'à 25 films de l'historique servent de graines — les mieux
notés pour ce que vous aimez, les plus récemment marqués pour ce qui vous occupe
en ce moment. Leurs recommandations sont agrégées, purgées de ce que vous avez
déjà vu, puis classées. Chaque suggestion indique de quels films elle découle
(« d'après *Heat*, *Seven* »), pour que le conseil soit vérifiable plutôt que
magique.

Le classement combine trois signaux : la co-occurrence (un film recommandé par
plusieurs de vos films compte double), l'affinité de genre mesurée sur tout
l'historique, et la note TMDB. Trois garde-fous : un plancher de 150 votes et de
6,2 de note, et surtout un **plafond de trois suggestions par graine**. Sans ce
dernier, une saga présente plusieurs fois dans l'historique fournit plusieurs
graines qui se renforcent et monopolisent toute la première page.

Le classement est recalculé quand la liste des films vus change, et gardé en
mémoire trente minutes sinon.

### Filtrer par genre

Les pastilles de genre du panneau s'appliquent à cet onglet, et les deux états
n'ont volontairement pas le même effet.

Un genre **exclu** (deux clics) disparaît complètement : des suggestions, mais
aussi des graines et du calcul d'affinité. Sans quoi il continuerait de façonner
le profil de goût tout en restant invisible — c'est le remède quand la
synchronisation Plex a importé les films regardés par toute la maison.

Un genre **inclus** (un clic) ne filtre que l'affichage : l'apprentissage
continue de porter sur tout l'historique, seule la sélection montrée se
restreint. Le bandeau indique alors combien de suggestions ont été écartées.

Il n'y a pas de bouton de validation dans cet onglet : tout changement de
critère relance immédiatement le classement. Les clics rapprochés sont
regroupés, car passer une pastille de neutre à « exclu » en demande deux.

### Les autres critères

Époque, note minimum, popularité, langue, durée et plateformes s'appliquent
aussi. **Le tri est le seul critère écarté** : cet onglet a son propre ordre,
c'est tout son intérêt.

Ils ne coûtent pas la même chose. Note, popularité, époque et langue se lisent
sur les données déjà en main : le filtrage est instantané. Durée et plateformes
sont absentes des recommandations TMDB et demandent un appel par film — le
nombre de candidats examinés est donc plafonné à 120, au-delà duquel personne ne
descend de toute façon.

## Films proches

Chaque fiche se termine par une rangée « Dans le même esprit » : dix affiches
cliquables qui ouvrent directement la fiche du film choisi, de proche en proche.
Les films déjà vus y sont grisés. Un bouton déplie l'ensemble dans la grille
principale, avec pagination.

La source est `/movie/{id}/recommendations`, fondée sur les habitudes des
spectateurs — nettement plus pertinente que `/movie/{id}/similar`, qui ne
rapproche que par genres et mots-clés. Mais elle est vide pour les films
confidentiels : dans ce cas l'application bascule sur `similar` et le signale
plutôt que de laisser croire à une vraie recommandation.

## Sagas

Quand un film appartient à une collection TMDB, sa fiche affiche la saga
complète dans l'ordre de sortie, avec les épisodes déjà vus grisés et un
décompte (« 3 films dans la saga — tu en as vu 3 »). La donnée d'appartenance
arrive avec la fiche du film ; seule la liste des épisodes demande un appel
supplémentaire.

## Listes

Trois listes, un fichier chacune : `seen.json` pour les films vus,
`watchlist.json` pour ceux mis de côté, `ignored.json` pour ceux qu'on ne veut
plus voir proposés. Le format est identique, les deux sont
lisibles et modifiables à la main, et toutes deux bénéficient des mêmes
protections (écriture atomique, copie de secours, mise en quarantaine d'un
fichier illisible).

Sur une affiche : la pastille en bas à gauche marque « vu », le signet à droite
ajoute « à voir », la croix en dessous écarte le film. Les onglets au-dessus de
la grille affichent chaque liste avec son décompte.

### Écarter un film

La croix retire le film des propositions, des suggestions « Pour toi » et des
films proches — définitivement, jusqu'à ce que tu changes d'avis. La carte
disparaît immédiatement de la grille, et la page se complète pour rester pleine.

Ce retrait est **silencieux** : contrairement au masquage des films déjà vus, il
n'est pas annoncé dans le bandeau. C'est une décision déjà prise, la rappeler à
chaque recherche n'apprendrait rien.

Deux exceptions volontaires. La **recherche par titre** continue de trouver un
film écarté : chercher un nom précis et ne rien obtenir serait déroutant. Et
l'onglet **Ignorés** liste les films écartés avec leur affiche et leur note, où
la même croix les réhabilite — c'est là qu'on revient sur son choix.

Les fichiers ne retiennent qu'un identifiant, un titre, une année et une date.
C'est volontaire : une note ou une affiche recopiées s'y périmeraient. Les
cartes sont donc reconstituées depuis TMDB à l'affichage, en parallèle et avec
le cache de 24 h — comptez quelques secondes à la première ouverture d'une
longue liste, puis c'est instantané. Un film introuvable sur TMDB reste affiché
avec ce que la liste en sait.

### Synchronisation depuis Plex

Si Plex est configuré, les films que tu y as regardés (`viewCount` supérieur à
zéro) alimentent automatiquement la liste des déjà vus, au démarrage du serveur
et via `/api/plex/sync`.

La synchronisation est **à sens unique et purement additive** : Trouveur n'écrit
jamais dans ta bibliothèque Plex, et n'enlève jamais rien de ta liste. Chaque
entrée garde sa provenance (`manuel` ou `plex`) ; un marquage manuel n'est jamais
écrasé par la synchronisation.

Si tu retires à la main un film importé de Plex, il est mémorisé comme refusé et
la synchronisation suivante ne le remet pas — sans quoi tu le verrais revenir
indéfiniment. Le remarquer volontairement lève ce refus.

Seuls les films dont Plex connaît l'identifiant TMDB sont repris : sans lui, le
rapprochement ne serait pas sûr, et marquer le mauvais film comme vu serait pire
que de n'en marquer aucun. `"sync_watched": false` désactive le tout.

## Afficher plus

La grille sert 20 propositions, puis se prolonge : un bouton **Afficher plus**
sous la grille, doublé d'un défilement infini qui anticipe de 600 px. Le bouton
reste visible et utilisable au clavier — il n'est pas qu'un repli.

Le comportement dépend du tri. Un tri strict demande la page suivante et
prolonge le classement. Le tri « Au hasard » repioche une page au hasard à
chaque fois : il n'y a pas de « suite » à un tirage aléatoire.

Les critères sont figés au lancement de la recherche : bouger un curseur pendant
la lecture ne change pas ce que « afficher plus » va chercher. Il faut relancer
la recherche, qui repart d'une grille vide.

Les identifiants déjà affichés sont mémorisés et les doublons écartés — les
pages TMDB se chevauchent parfois, et un tirage au hasard peut retomber sur une
page déjà vue. Si une page n'apporte que des doublons, une seconde tentative est
faite avant de conclure qu'il n'y a plus rien.

Une réserve à connaître : l'ordre est strict *à l'intérieur* de chaque page, et
les pages sont ajoutées à la suite sans reclassement global. TMDB pagine sur sa
propre clé de tri alors que l'application affiche la date de sortie française,
donc une légère inversion à la jointure entre deux pages est possible. Reclasser
tout à chaque chargement ferait sauter les films déjà lus sous les yeux, ce qui
serait pire.

## Sur téléphone

L'interface est pensée pour le mobile, où elle est surtout consultée.

Le panneau de critères y devient un **tiroir** : il occupait 1120 px au-dessus
de la grille, ce qui imposait de défiler une fois et demie avant de voir un
seul film. Il s'ouvre par le bouton **Filtres**, qui affiche le nombre de
critères actifs — sans quoi, tiroir fermé, on ne saurait pas ce qui filtre les
résultats. Lancer une recherche le referme : on vient de demander des
résultats, autant les montrer.

L'en-tête passe de 220 à 127 px (accroche et état du tracker masqués, recherche
sur sa propre ligne), et les cibles tactiles respectent les seuils usuels :
44 px pour les onglets, boutons et champs, 40 px pour les pastilles. Les
commandes des cartes restent à 38 px — trois cercles de 44 px sur une affiche
de 147 px masqueraient le film.

Sur une carte étroite, la pastille « Vu » et les badges de disponibilité ne
tiennent pas côte à côte : les badges passent au-dessus, et sont limités à deux
(la fiche du film les montre tous).

Un piège de CSS Grid méritait d'être noté : une piste `1fr` a pour minimum
implicite `auto`, donc elle **s'élargit pour contenir son contenu** au lieu de
le contraindre. La barre d'onglets (~460 px) poussait ainsi la colonne au-delà
de l'écran : ascenseur horizontal, et la grille calculait trois colonnes de
films là où il n'y a la place que pour deux. `minmax(0, 1fr)` lève ce minimum,
et la barre d'onglets défile alors dans son propre cadre.

Au-dessus de 1080 px, rien ne change : le panneau reste fixe à gauche.

## Écran de réglages

Le bouton **Réglages** ouvre la configuration : clé TMDB, abonnements de
streaming, connexion Plex, tracker, et import de listes existantes. Tout se fait
depuis le navigateur — indispensable dans un add-on Home Assistant, où aucun
terminal n'est disponible.

Deux règles y sont tenues partout :

- **Aucun secret ne repart vers le navigateur.** L'interface reçoit un
  « renseignée / vide » par clé, jamais la valeur.
- **Un champ vide veut dire « inchangé »**, jamais « efface ». On peut
  enregistrer sans ressaisir ses clés.

La connexion Plex utilise le « Se connecter avec Plex » officiel : la page
d'approbation s'ouvre chez Plex, les serveurs du compte sont ensuite découverts
avec leurs adresses, et celle qui répond est retenue. Aucun jeton à recopier.

L'import de listes est additif : un film déjà présent garde sa date et sa
provenance. Réimporter la même sauvegarde deux fois est sans effet.

## Home Assistant

Trouveur s'installe comme add-on Home Assistant, avec son interface dans la
barre latérale (ingress).

```bash
python package_addon.py     # assemble addon/app/
```

Puis copier le dossier `addon/` dans `/addons/trouveur/` sur la machine Home
Assistant. Détails dans [addon/DOCS.md](addon/DOCS.md).

Deux points de conception valent d'être signalés.

**Les données ne vivent plus à côté du code.** En conteneur, `/opt/trouveur`
est remplacé à chaque mise à jour : listes, configuration et cache vont dans
`/data`, le volume persistant du superviseur. La variable `TROUVEUR_DATA_DIR`
déplace l'ensemble, et chaque fichier garde sa propre variable
(`TROUVEUR_SEEN_PATH`…) pour les tests. Sans ces variables, tout reste dans le
dossier du projet comme avant.

**L'image ne contient aucune donnée.** `package_addon.py` copie le code et
refuse de s'exécuter s'il détecte une clé, une liste ou un cache dans ce qu'il
s'apprête à assembler. Une image se partage et se reconstruit : y enfermer un
jeton d'API serait une fuite, et un historique de visionnage une donnée perdue
à la mise à jour suivante.

## Notes

- Le tri par défaut est « Meilleures notes ». Il est rendu strictement : deux
  recherches identiques donnent le même ordre. Idem pour les autres tris.
- Le tri « Au hasard » pioche dans les 20 premières pages de résultats TMDB
  puis mélange : relancer la même recherche donne alors d'autres films. C'est
  le seul tri qui ne soit pas reproductible, par construction.
- L'ordre affiché est recalculé sur la valeur montrée à l'écran. TMDB trie sur
  la date de sortie d'origine alors que l'application affiche la sortie
  française : sans ce recalage, un tri par ancienneté paraîtrait désordonné.
- Sur les genres, un premier clic inclut, un deuxième exclut, un troisième
  annule. À partir de deux genres inclus, un sélecteur permet de choisir entre
  « au moins un » et « tous à la fois ».
- SensCritique et Allociné n'ont pas d'API publique : les liens pointent vers
  leur recherche pré-remplie avec le titre français.
- La bande-annonce privilégie la version française ; à défaut la version
  originale est proposée, et l'interface l'indique.
- `config.json` et `.cache/` sont ignorés par git : la clé d'API ne part pas
  dans un dépôt.

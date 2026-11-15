"""Adaptateur tracker Torznab.

tr4ker.net et v3x.club exposent tous deux du Torznab : ce ne sont pas deux
protocoles, mais deux dialectes du meme. Ce qui les separe tient en trois
points, et chacun est un reglage, pas une branche de code :

  - **l'authentification.** tr4ker lit une cle brute dans X-Api-Key ; v3x veut
    un jeton Bearer (il accepte ?apikey= pour le scope torznab, mais une cle en
    query finit dans les journaux du serveur d'en face) ;
  - **l'adresse.** Chaque tracker a la sienne, api_path compris ;
  - **le telechargement du .torrent.** tr4ker se devine depuis la page de
    details ; v3x fournit l'adresse directement dans le flux. Le flux fait
    autorite quand il la donne : c'est le standard, et cela evite de deviner.

Un profil nomme porte ces valeurs (voir PROFILS) pour qu'une configuration se
resume a choisir un tracker, sans avoir a connaitre ces details.

La cle d'API ne sort jamais du serveur, quel que soit le tracker : l'interface
ne recoit qu'un identifiant opaque, qu'elle repasse au proxy local
/api/torrent.
"""

from __future__ import annotations

import json
import re
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Any

from . import cache
from .http_client import HttpError, get_bytes, register_secret

TORZNAB_NS = "{http://torznab.com/schemas/2015/feed}"

# Adresses de telechargement gardees en memoire. Au-dela, on repart a zero :
# une adresse oubliee se retrouve en relancant la recherche.
MEMOIRE_MAX = 4000

# Reglages connus, par tracker. Tout est surchargeable dans config.json : un
# profil ne fait que dispenser de les retrouver.
PROFILS: dict[str, dict[str, Any]] = {
    # Indexeur Torznab quelconque : aucune hypothese.
    "torznab": {},
    "tr4ker.net": {
        "name": "tr4ker.net",
        "base_url": "https://tr4ker.net",
        "api_path": "/api",
        "auth": "header",
        "header_name": "X-Api-Key",
        # Ce tracker ne publie pas l'adresse du .torrent dans le flux : elle se
        # reconstruit depuis l'identifiant lu sur la page de details.
        "download_path": "/api/torrents/{id}/download",
    },
    "v3x.club": {
        "name": "v3x.club",
        "base_url": "https://api.v3x.club",
        # Le Torznab de V3X n'est pas sous /api : il a son propre chemin, a
        # cote des routes /api/... (categories, me, torrents/exists).
        "api_path": "/torznab",
        # « Le parametre ?apikey= n'est accepte que pour le scope torznab [...]
        # une cle en query fuiterait dans les logs. » On prend donc le Bearer.
        "auth": "bearer",
        "header_name": "Authorization",
        # Le flux donne l'adresse du .torrent ; ceci ne sert qu'a la retrouver
        # apres un redemarrage. Noter l'absence de /api, conforme a la
        # documentation : api.v3x.club/torrents/<id>/download.
        "download_path": "/torrents/{id}/download",
        # Route d'identite, pour le diagnostic : elle repond avec n'importe
        # quelle cle et prouve d'un coup l'adresse et la cle, sans dependre du
        # chemin Torznab.
        "identity_path": "/api/me",
    },
}

# Etiquettes deduites du nom de release, faute de champs dedies cote tracker.
RESOLUTIONS = ("2160p", "1080p", "720p", "576p", "480p")
SOURCES = (
    "REMUX", "BLURAY", "BLU-RAY", "BDRIP", "BRRIP", "WEB-DL", "WEBDL",
    "WEBRIP", "HDTV", "DVDRIP", "DVDSCR", "CAM", "TS",
)
CODECS = ("X265", "H265", "HEVC", "X264", "H264", "AV1", "XVID", "DIVX")
LANGS = (
    "MULTI", "TRUEFRENCH", "VFF", "VFQ", "VFI", "VF2", "VOSTFR",
    "FRENCH", "SUBFRENCH", "VOSTA", "VO",
)


class TrackerError(RuntimeError):
    pass


def _avec_profil(settings: dict[str, Any]) -> dict[str, Any]:
    """Valeurs du profil choisi, surchargees par ce qui est ecrit en clair.

    Ce qui est dans config.json gagne toujours : un profil est une commodite,
    pas une contrainte.
    """
    profil = PROFILS.get(settings.get("profile") or "", {})
    fusion = dict(profil)
    for cle, valeur in settings.items():
        if valeur not in (None, ""):
            fusion[cle] = valeur
    return fusion


class Tracker:
    def __init__(self, config: dict[str, Any]):
        settings = _avec_profil(config.get("tracker", {}))
        self.profile: str = settings.get("profile") or "torznab"
        self.enabled: bool = bool(settings.get("enabled"))
        self.name: str = settings.get("name") or "tracker"
        self.base_url: str = (settings.get("base_url") or "").rstrip("/")
        self.api_path: str = settings.get("api_path") or "/api"
        self.api_key: str = (settings.get("api_key") or "").strip()
        self.auth: str = settings.get("auth") or "header"
        self.header_name: str = settings.get("header_name") or "X-Api-Key"
        self.download_path: str = settings.get("download_path") or ""
        self.identity_path: str = settings.get("identity_path") or ""
        # Mode d'authentification retenu pour le point d'acces Torznab, une
        # fois qu'on a vu lequel passe. None = pas encore determine.
        self._auth_torznab: str | None = settings.get("auth_torznab") or None
        # slug -> adresse du .torrent lue dans le flux. En memoire seulement :
        # cette adresse porte la cle d'API et n'a rien a faire sur le disque.
        self._telechargements: dict[str, str] = {}
        self.movie_category: str = str(settings.get("movie_category") or "2000")
        self.limit: int = int(settings.get("limit") or 100)
        self.timeout: int = int(settings.get("timeout") or 15)
        self.max_results: int = int(settings.get("max_results") or 40)
        register_secret(self.api_key)

        cache_settings = config.get("cache", {})
        self.cache_ttl: int = (
            int(cache_settings.get("tracker_ttl_seconds", 900))
            if cache_settings.get("enabled", True)
            else 0
        )

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.base_url and self.api_key)

    # -- transport ---------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return self._entetes()

    def _url(self, path: str, params: dict[str, Any] | None = None,
             auth: str | None = None) -> str:
        clean = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        if (auth or self.auth) not in ("header", "bearer"):
            clean["apikey"] = self.api_key
        query = urllib.parse.urlencode(clean)
        return self.base_url + path + ("?" + query if query else "")

    def _entetes(self, auth: str | None = None) -> dict[str, str]:
        mode = auth or self.auth
        if mode == "bearer":
            return {self.header_name or "Authorization": "Bearer " + self.api_key}
        if mode == "header":
            return {self.header_name: self.api_key}
        return {}

    def _lire(self, path: str, params: dict[str, Any] | None = None) -> bytes:
        """Lit une route du tracker, en s'adaptant a ce qu'il accepte vraiment.

        V3X documente le Bearer comme « valable pour tous les scopes », mais sa
        recherche Torznab le refuse : elle veut ?apikey=, « impose par
        Prowlarr/les *arr » -- le seul chemin que leur implementation exerce
        reellement. Plutot que de trancher a leur place, on essaie le mode
        configure puis l'autre, et on retient celui qui passe.
        """
        # Le mode retenu passe en premier -- il evite un aller-retour -- mais
        # l'autre reste toujours disponible. Un t=caps servi sans
        # authentification fait sinon retenir un mode que la recherche refuse :
        # c'est exactement ce qui se produit chez V3X.
        prefere = self._auth_torznab or self.auth
        alternatif = "query" if prefere in ("header", "bearer") else "bearer"
        modes = [prefere, alternatif]

        dernier: HttpError | None = None
        for mode in modes:
            try:
                brut = get_bytes(self._url(path, params, auth=mode),
                                 headers=self._entetes(mode), timeout=self.timeout)
                self._auth_torznab = mode
                return brut
            except HttpError as exc:
                if exc.status not in (401, 403):
                    raise
                dernier = exc
        raise dernier or HttpError("Echec inattendu sur %s" % path)

    def _query(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            raw = self._lire(self.api_path, params)
        except HttpError as exc:
            if exc.status in (401, 403):
                raise TrackerError(
                    "%s refuse la clé pour la recherche (HTTP %s), en en-tête "
                    "comme en paramètre d'URL. Vérifie qu'elle porte le scope "
                    "« torznab »." % (self.name, exc.status)
                ) from exc
            raise TrackerError(str(exc)) from exc

        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            raise TrackerError(
                "%s a renvoye une reponse illisible (XML Torznab attendu)." % self.name
            ) from exc

        return [self._item(node) for node in root.iter("item")]

    def _item(self, node: ET.Element) -> dict[str, Any]:
        attrs = {
            a.get("name"): a.get("value")
            for a in node.iter(TORZNAB_NS + "attr")
        }
        name = (node.findtext("title") or "").strip()
        details = node.findtext("comments") or node.findtext("guid") or None

        # Torznab publie l'adresse du .torrent dans <enclosure url> ou <link>.
        # Quand elle est la, elle fait autorite : la reconstruire reviendrait a
        # deviner ce que le tracker vient de nous dire.
        enclosure = node.find("enclosure")
        telechargement = (enclosure.get("url") if enclosure is not None else None)
        if not telechargement:
            lien = (node.findtext("link") or "").strip()
            # <link> pointe parfois sur la fiche, pas sur le fichier : on ne le
            # retient que s'il se distingue de l'adresse de details.
            telechargement = lien if lien and lien != details else None

        return {
            "name": name,
            "size": _to_int(attrs.get("size")),
            "seeders": _to_int(attrs.get("seeders")),
            "leechers": _to_int(attrs.get("leechers")),
            "grabs": _to_int(attrs.get("grabs")),
            "infohash": attrs.get("infohash"),
            "category": attrs.get("category"),
            "tmdb_id": _to_int(attrs.get("tmdbid")),
            "date": node.findtext("pubDate"),
            "details": details,
            # Freeleech : le tracker ne compte pas le telechargement.
            "freeleech": attrs.get("downloadvolumefactor") == "0",
            # Le slug identifie le .torrent ; l'interface le passe au proxy local
            # plutot que de manipuler une URL portant la cle d'API.
            "slug": _slug_from(details) or _slug_from(telechargement),
            # Jamais transmis au navigateur ni ecrit en cache : cette adresse
            # porte la cle d'API.
            "download": telechargement,
            "tags": parse_tags(name),
        }

    # -- recherche ---------------------------------------------------------

    def search_movie(
        self,
        tmdb_id: int | None,
        title: str,
        year: int | None = None,
        original_title: str | None = None,
    ) -> dict[str, Any]:
        """Cherche par tmdbid (exact), puis par titre en dernier recours."""
        cache_key = "torznab:%s:%s:%s" % (self.name, tmdb_id, title.lower())
        cached = cache.get(cache_key, self.cache_ttl)
        if cached is not None:
            return cached

        results: list[dict[str, Any]] = []
        matched_by = None

        if tmdb_id:
            results = self._query(
                {"t": "movie", "tmdbid": tmdb_id, "cat": self.movie_category,
                 "limit": self.limit}
            )
            if results:
                matched_by = "tmdbid"

        if not results:
            # Le repli par titre est bruite : le tracker ignore cat= et rapproche
            # largement. On ne garde que les releases citant vraiment le titre.
            for candidate in _title_candidates(title, original_title):
                found = self._query(
                    {"t": "movie", "q": candidate, "cat": self.movie_category,
                     "limit": self.limit}
                )
                found = [t for t in found if _looks_like(t, candidate, year)]
                if found:
                    results = found
                    matched_by = "titre"
                    break

        results = [t for t in results if t["name"] and self._is_movie(t)]
        results.sort(key=lambda t: (-(t["seeders"] or 0), -(t["size"] or 0)))
        results = [self._retenir(t) for t in results]
        payload = {
            "torrents": results[: self.max_results],
            "matched_by": matched_by,
            "query": title,
            "error": None,
        }
        cache.put(cache_key, payload)
        return payload

    def _retenir(self, torrent: dict[str, Any]) -> dict[str, Any]:
        """Met l'adresse de telechargement de cote et la retire du resultat.

        Elle porte la cle d'API : elle ne doit ni partir vers le navigateur, ni
        etre ecrite dans le cache sur disque. Seul l'identifiant circule, et le
        proxy local le retraduit au moment du telechargement.
        """
        torrent = dict(torrent)
        adresse = torrent.pop("download", None)
        slug = torrent.get("slug")
        if adresse and slug:
            if len(self._telechargements) >= MEMOIRE_MAX:
                # Borne simple : on repart a zero plutot que de grossir sans
                # fin. Une adresse oubliee est rejouable par une recherche.
                self._telechargements.clear()
            self._telechargements[slug] = adresse
        return torrent

    def _is_movie(self, torrent: dict[str, Any]) -> bool:
        """Le tracker n'applique pas cat= : on filtre nous-memes les categories."""
        category = torrent.get("category")
        if not category:
            return True
        base = self.movie_category[:1]
        return str(category).startswith(base)

    def test(self) -> dict[str, Any]:
        """Diagnostic pour l'ecran de reglages, etape par etape.

        « Aucun torrent trouve » peut vouloir dire une adresse fausse, une cle
        refusee, un scope manquant ou simplement un film absent du catalogue.
        Ces cas demandent des gestes differents : autant les distinguer.
        """
        etapes: list[dict[str, Any]] = []

        def etape(nom: str, action) -> Any:
            try:
                resultat = action()
                etapes.append({"etape": nom, "ok": True})
                return resultat
            except TrackerError as exc:
                etapes.append({"etape": nom, "ok": False, "erreur": str(exc)})
                return None

        if not self.configured:
            manque = "l'adresse" if not self.base_url else "la clé d'API"
            return {"ok": False, "steps": [],
                    "message": "Tracker incomplet : il manque %s." % manque}

        # L'identite d'abord, quand le tracker en expose une : elle prouve
        # l'adresse et la cle sans rien devoir au chemin Torznab. Sans elle,
        # une cle valide sur un mauvais chemin ressemble a une cle refusee.
        if self.identity_path:
            compte = etape("Authentification auprès de %s" % self.name,
                           self._identite)
            if compte is None:
                return {"ok": False, "steps": etapes}
            qui = compte.get("username")
            if qui:
                etapes[-1]["detail"] = "compte « %s »" % qui

        racine = etape("Point d'accès Torznab (%s)" % self.api_path,
                       lambda: self._caps())
        if racine is None:
            return {"ok": False, "steps": etapes}

        categories = [c.get("name") for c in racine.iter("category") if c.get("name")]
        etapes.append({
            "etape": "Réponse Torznab comprise", "ok": True,
            "detail": ("%d catégories annoncées" % len(categories)) if categories
                      else "aucune catégorie annoncée",
        })

        recherche = etape(
            "Recherche d'essai (tmdbid 949)",
            lambda: self._query({"t": "movie", "tmdbid": 949,
                                 "cat": self.movie_category, "limit": 5}))
        if recherche is None:
            return {"ok": False, "steps": etapes}
        etiquette = {"bearer": "en-tête Bearer", "header": "en-tête %s" % self.header_name,
                     "query": "paramètre ?apikey="}.get(self._auth_torznab or self.auth,
                                                        self._auth_torznab or self.auth)
        etapes[-1]["detail"] = "%d résultat(s), authentification par %s" % (
            len(recherche), etiquette)

        return {"ok": True, "steps": etapes, "categories": categories[:40],
                "auth_torznab": self._auth_torznab or self.auth}

    def _identite(self) -> dict[str, Any]:
        """Qui parle, d'apres le tracker. JSON, hors Torznab."""
        url = self._url(self.identity_path)
        try:
            brut = get_bytes(url, headers=self._headers(), timeout=self.timeout)
        except HttpError as exc:
            if exc.status in (401, 403):
                raise TrackerError(
                    "%s refuse la clé (HTTP %s). Vérifie-la, et qu'elle porte "
                    "bien le scope « torznab »." % (self.name, exc.status)
                ) from exc
            raise TrackerError(
                "%s ne répond pas sur %s%s (%s). L'adresse est-elle la bonne ?"
                % (self.name, self.base_url, self.identity_path, exc)
            ) from exc
        try:
            donnees = json.loads(brut.decode("utf-8", "replace"))
        except json.JSONDecodeError as exc:
            raise TrackerError(
                "%s a répondu autre chose que du JSON sur %s."
                % (self.name, self.identity_path)) from exc
        return donnees if isinstance(donnees, dict) else {}

    def _caps(self):
        """Capacites Torznab, en XML."""
        try:
            raw = self._lire(self.api_path, {"t": "caps"})
        except HttpError as exc:
            if exc.status in (401, 403):
                raise TrackerError(
                    "%s refuse la clé (HTTP %s). Vérifie-la, et pour v3x qu'elle "
                    "porte bien le scope « torznab »."
                    % (self.name, exc.status)) from exc
            if exc.status == 404:
                raise TrackerError(
                    "Rien sur %s%s. C'est le chemin du point d'accès Torznab "
                    "qui est faux, pas la clé." % (self.base_url, self.api_path)
                ) from exc
            raise TrackerError(str(exc)) from exc

        try:
            return ET.fromstring(raw)
        except ET.ParseError as exc:
            raise TrackerError(
                "%s a répondu, mais pas en Torznab (XML attendu). L'adresse "
                "pointe-t-elle bien sur le point d'accès Torznab ?" % self.name
            ) from exc

    def torrent_file(self, slug: str) -> bytes:
        """Recupere le .torrent, cle ajoutee cote serveur."""
        if not self.configured:
            raise TrackerError("Tracker non configure.")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,200}", slug):
            raise TrackerError("Identifiant de torrent invalide.")

        url = self._adresse_telechargement(slug)
        try:
            return get_bytes(url, headers=self._headers(), timeout=self.timeout)
        except HttpError as exc:
            raise TrackerError(str(exc)) from exc

    def _adresse_telechargement(self, slug: str) -> str:
        """L'adresse retenue lors de la recherche, sinon celle du profil."""
        connue = self._telechargements.get(slug)
        if connue:
            return connue
        if self.download_path:
            return self._url(self.download_path.replace("{id}", slug))
        # Ni l'une ni l'autre : le dire plutot que de fabriquer une adresse au
        # hasard, qui echouerait avec un message incomprehensible.
        raise TrackerError(
            "%s ne publie l'adresse du .torrent que dans ses résultats de "
            "recherche, et celle-ci n'est plus en mémoire (serveur redémarré ?). "
            "Relance la recherche pour ce film." % self.name
        )


# -- utilitaires ------------------------------------------------------------


def _slug_from(details_url: str | None) -> str | None:
    if not details_url:
        return None
    tail = details_url.rstrip("/").rsplit("/", 1)[-1]
    return tail or None


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _title_candidates(title: str, original_title: str | None) -> list[str]:
    out = []
    for candidate in (title, original_title):
        if candidate and candidate not in out:
            out.append(candidate)
    return out


def _looks_like(torrent: dict[str, Any], title: str, year: int | None) -> bool:
    """Verifie que la release correspond vraiment au film cherche.

    Ce filtre ne sert que lorsque la recherche par tmdbid n'a rien donne et que
    l'on retombe sur le titre. Le tracker rapproche alors tres largement : il
    vaut mieux ecarter un resultat valable que d'en afficher un faux.
    """
    raw = torrent.get("name") or ""
    name = _normalize(raw)

    words = [w for w in _normalize(title).split() if len(w) > 2]
    if not words:
        words = _normalize(title).split()
    if not all(word in name for word in words):
        return False

    # Un marqueur de serie exclut d'emblee un film.
    if re.search(r"(?<![A-Z0-9])(S\d{2}(E\d{2,3})?|SAISON\s*\d+)(?![A-Z0-9])", raw.upper()):
        return False

    # Une release de film porte son annee. Sans elle, impossible de distinguer
    # "La Haine" du documentaire "La Haine, la scene est a nous".
    if year:
        years = {int(y) for y in re.findall(r"\b(19\d{2}|20\d{2})\b", raw)}
        # Tolerance d'un an : sortie salle et edition video different parfois.
        if not years & {year - 1, year, year + 1}:
            return False

    return True


def parse_tags(name: str) -> dict[str, str | None]:
    upper = name.upper()

    def first(candidates: tuple[str, ...]) -> str | None:
        """Renvoie l'etiquette telle qu'on veut l'afficher, en cherchant en majuscules."""
        for candidate in candidates:
            pattern = r"(?<![A-Z0-9])" + re.escape(candidate.upper()) + r"(?![A-Z0-9])"
            if re.search(pattern, upper):
                return candidate
        return None

    resolution = first(RESOLUTIONS)
    if not resolution and re.search(r"(?<![A-Z0-9])(4K|UHD)(?![A-Z0-9])", upper):
        resolution = "2160p"

    return {
        "resolution": resolution,
        "source": first(SOURCES),
        "codec": first(CODECS),
        "language": first(LANGS),
    }


def _to_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    if text.replace(".", "", 1).isdigit():
        return int(float(text))
    match = re.match(r"^([\d.,]+)\s*(B|KB|KIB|MB|MIB|GB|GIB|TB|TIB)$", text, re.I)
    if match:
        amount = float(match.group(1).replace(",", "."))
        factors = {"B": 1, "KB": 1024, "KIB": 1024, "MB": 1024**2, "MIB": 1024**2,
                   "GB": 1024**3, "GIB": 1024**3, "TB": 1024**4, "TIB": 1024**4}
        return int(amount * factors[match.group(2).upper()])
    return None

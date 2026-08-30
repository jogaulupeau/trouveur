"""Adaptateur tracker Torznab (tr4ker.net par defaut).

tr4ker expose une API Torznab standard sur /api : la recherche de films accepte
un tmdbid, ce qui evite tout rapprochement approximatif par titre. Le protocole
etant un standard, cet adaptateur fonctionne avec n'importe quel indexeur
Torznab en changeant base_url dans config.json.

La cle d'API voyage en en-tete et ne sort jamais du serveur : les liens de
telechargement proposes a l'interface passent par le proxy local /api/torrent.
"""

from __future__ import annotations

import re
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Any

from . import cache
from .http_client import HttpError, get_bytes, register_secret

TORZNAB_NS = "{http://torznab.com/schemas/2015/feed}"

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


class Tracker:
    def __init__(self, config: dict[str, Any]):
        settings = config.get("tracker", {})
        self.enabled: bool = bool(settings.get("enabled"))
        self.name: str = settings.get("name") or "tracker"
        self.base_url: str = (settings.get("base_url") or "").rstrip("/")
        self.api_path: str = settings.get("api_path") or "/api"
        self.api_key: str = (settings.get("api_key") or "").strip()
        self.auth: str = settings.get("auth") or "header"
        self.header_name: str = settings.get("header_name") or "X-Api-Key"
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
        return {self.header_name: self.api_key} if self.auth == "header" else {}

    def _url(self, path: str, params: dict[str, Any] | None = None) -> str:
        clean = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        if self.auth != "header":
            clean["apikey"] = self.api_key
        query = urllib.parse.urlencode(clean)
        return self.base_url + path + ("?" + query if query else "")

    def _query(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        url = self._url(self.api_path, params)
        try:
            raw = get_bytes(url, headers=self._headers(), timeout=self.timeout)
        except HttpError as exc:
            if exc.status in (401, 403):
                raise TrackerError(
                    "%s refuse la cle d'API (HTTP %s). Verifie tracker.api_key "
                    "dans config.json." % (self.name, exc.status)
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
            "slug": _slug_from(details),
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
        payload = {
            "torrents": results[: self.max_results],
            "matched_by": matched_by,
            "query": title,
            "error": None,
        }
        cache.put(cache_key, payload)
        return payload

    def _is_movie(self, torrent: dict[str, Any]) -> bool:
        """Le tracker n'applique pas cat= : on filtre nous-memes les categories."""
        category = torrent.get("category")
        if not category:
            return True
        base = self.movie_category[:1]
        return str(category).startswith(base)

    def torrent_file(self, slug: str) -> bytes:
        """Recupere le .torrent, cle ajoutee cote serveur."""
        if not self.configured:
            raise TrackerError("Tracker non configure.")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,200}", slug):
            raise TrackerError("Identifiant de torrent invalide.")
        url = self._url("/api/torrents/%s/download" % slug)
        try:
            return get_bytes(url, headers=self._headers(), timeout=self.timeout)
        except HttpError as exc:
            raise TrackerError(str(exc)) from exc


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

"""Interrogation d'un serveur Plex : quels films sont deja dans la bibliotheque.

Plex conserve l'identifiant TMDB de chaque film matche : l'appariement est donc
exact, sans rapprochement approximatif par titre (le repli par titre + annee ne
sert que pour les entrees mal matchees).

La bibliotheque entiere est chargee une fois puis indexee en memoire : repondre
pour vingt films ne coute alors aucun appel reseau.
"""

from __future__ import annotations

import re
import ssl
import threading
import time
import unicodedata
import urllib.parse
from typing import Any

from .http_client import HttpError, get_json, register_secret

# Plex pagine ; au-dela de cette taille de page, les grosses bibliotheques
# renvoient des reponses tres lourdes.
PAGE_SIZE = 500
MAX_PAGES = 40

# Anciens agents : com.plexapp.agents.themoviedb://335984?lang=fr
# Agent actuel   : tmdb://335984 (dans les balises Guid)
TMDB_GUID = re.compile(r"(?:themoviedb|tmdb)://(\d+)")
IMDB_GUID = re.compile(r"(?:imdb)://(tt\d+)")


class PlexError(RuntimeError):
    pass


class Plex:
    def __init__(self, config: dict[str, Any]):
        settings = config.get("plex", {})
        self.enabled: bool = bool(settings.get("enabled"))
        self.base_url: str = (settings.get("base_url") or "").rstrip("/")
        self.token: str = (settings.get("token") or "").strip()
        self.timeout: int = int(settings.get("timeout") or 20)
        self.verify_tls: bool = bool(settings.get("verify_tls", True))
        self.refresh_seconds: int = int(settings.get("refresh_seconds") or 600)
        # Les films lus sur Plex alimentent la liste des « deja vus ».
        # Sens unique : Trouveur n'ecrit jamais dans ta bibliotheque Plex.
        self.sync_watched: bool = bool(settings.get("sync_watched", True))
        # Vide = toutes les bibliotheques de type film.
        self.sections: list[str] = [str(s) for s in (settings.get("sections") or [])]
        register_secret(self.token)

        self._lock = threading.Lock()
        self._index: dict[str, Any] | None = None
        self._indexed_at: float = 0.0
        self._machine_id: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.base_url and self.token)

    # -- transport ---------------------------------------------------------

    def _call(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        clean = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        query = urllib.parse.urlencode(clean)
        url = self.base_url + path + ("?" + query if query else "")

        # Beaucoup de serveurs Plex presentent un certificat auto-signe.
        context = None
        if not self.verify_tls and url.startswith("https"):
            context = ssl._create_unverified_context()  # noqa: S323 - choix explicite

        headers = {"X-Plex-Token": self.token, "Accept": "application/json"}
        try:
            data = get_json(url, headers=headers, timeout=self.timeout, ssl_context=context)
        except HttpError as exc:
            if exc.status in (401, 403):
                raise PlexError(
                    "Plex refuse le jeton (HTTP %s). Verifie plex.token dans config.json."
                    % exc.status
                ) from exc
            raise PlexError(str(exc)) from exc
        if not isinstance(data, dict):
            raise PlexError("Reponse Plex inattendue (JSON attendu).")
        return data

    # -- construction de l'index ------------------------------------------

    def _movie_sections(self) -> list[str]:
        if self.sections:
            return self.sections
        payload = self._call("/library/sections")
        directories = (payload.get("MediaContainer") or {}).get("Directory") or []
        return [str(d["key"]) for d in directories if d.get("type") == "movie" and d.get("key")]

    def _fetch_section(self, key: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for page in range(MAX_PAGES):
            payload = self._call(
                "/library/sections/%s/all" % key,
                {
                    "type": 1,               # 1 = film
                    "includeGuids": 1,       # sans cela, pas d'identifiant TMDB
                    "X-Plex-Container-Start": page * PAGE_SIZE,
                    "X-Plex-Container-Size": PAGE_SIZE,
                },
            )
            container = payload.get("MediaContainer") or {}
            batch = container.get("Metadata") or []
            items.extend(batch)
            if len(batch) < PAGE_SIZE:
                break
        return items

    def _build_index(self) -> dict[str, Any]:
        by_tmdb: dict[int, dict[str, Any]] = {}
        by_imdb: dict[str, dict[str, Any]] = {}
        by_title: dict[str, dict[str, Any]] = {}

        total = 0
        for key in self._movie_sections():
            for raw in self._fetch_section(key):
                entry = self._entry(raw)
                total += 1
                if entry["tmdb_id"] is not None:
                    by_tmdb[entry["tmdb_id"]] = entry
                if entry["imdb_id"]:
                    by_imdb[entry["imdb_id"]] = entry
                if entry["title"]:
                    by_title[_title_key(entry["title"], entry["year"])] = entry

        return {
            "by_tmdb": by_tmdb,
            "by_imdb": by_imdb,
            "by_title": by_title,
            "count": total,
        }

    def _entry(self, raw: dict[str, Any]) -> dict[str, Any]:
        guids = " ".join(
            [str(raw.get("guid") or "")]
            + [str(g.get("id") or "") for g in (raw.get("Guid") or [])]
        )
        tmdb_match = TMDB_GUID.search(guids)
        imdb_match = IMDB_GUID.search(guids)

        media = (raw.get("Media") or [{}])[0]
        resolution = str(media.get("videoResolution") or "").lower()
        if resolution.isdigit():
            resolution += "p"
        elif resolution in ("4k", "uhd"):
            resolution = "2160p"

        return {
            "rating_key": str(raw.get("ratingKey") or ""),
            "title": raw.get("title") or "",
            "year": raw.get("year") if isinstance(raw.get("year"), int) else None,
            "tmdb_id": int(tmdb_match.group(1)) if tmdb_match else None,
            "imdb_id": imdb_match.group(1) if imdb_match else None,
            "resolution": resolution or None,
            "codec": (media.get("videoCodec") or None),
            "container": (media.get("container") or None),
            "size": _first_part_size(media),
            "duration": raw.get("duration"),
            # Plex sait deja ce que tu as regarde.
            "view_count": int(raw.get("viewCount") or 0),
            "added_at": raw.get("addedAt"),
        }

    def index(self, force: bool = False) -> dict[str, Any]:
        with self._lock:
            fresh = (
                self._index is not None
                and time.time() - self._indexed_at < self.refresh_seconds
            )
            if fresh and not force:
                return self._index
            index = self._build_index()
            self._index = index
            self._indexed_at = time.time()
            return index

    # -- consultation ------------------------------------------------------

    def machine_id(self) -> str | None:
        if self._machine_id is None:
            try:
                payload = self._call("/identity")
                container = payload.get("MediaContainer") or {}
                self._machine_id = container.get("machineIdentifier") or ""
            except PlexError:
                self._machine_id = ""
        return self._machine_id or None

    def lookup(
        self,
        tmdb_id: int | None = None,
        imdb_id: str | None = None,
        title: str = "",
        year: int | None = None,
    ) -> dict[str, Any] | None:
        index = self.index()
        if tmdb_id and tmdb_id in index["by_tmdb"]:
            return dict(index["by_tmdb"][tmdb_id], matched_by="tmdb")
        if imdb_id and imdb_id in index["by_imdb"]:
            return dict(index["by_imdb"][imdb_id], matched_by="imdb")
        if title:
            hit = index["by_title"].get(_title_key(title, year))
            if hit:
                return dict(hit, matched_by="titre")
        return None

    def watched_movies(self) -> list[dict[str, Any]]:
        """Films lus au moins une fois sur Plex, identifiables cote TMDB.

        Sans identifiant TMDB, un film ne peut pas etre rapproche de façon sure :
        on prefere l'ignorer plutot que de marquer le mauvais film comme vu.
        """
        index = self.index()
        watched = []
        for entry in index["by_tmdb"].values():
            if entry["view_count"] > 0:
                watched.append({
                    "id": entry["tmdb_id"],
                    "title": entry["title"],
                    "year": entry["year"],
                    "poster": None,
                })
        return watched

    def availability(self, movies: list[dict[str, Any]]) -> dict[str, Any]:
        """Repond pour une liste entiere de films en un seul passage."""
        if not self.configured:
            return {"disabled": True, "items": {}}

        found: dict[str, Any] = {}
        for movie in movies:
            hit = self.lookup(
                tmdb_id=movie.get("tmdb_id"),
                imdb_id=movie.get("imdb_id"),
                title=movie.get("title") or "",
                year=movie.get("year"),
            )
            if hit:
                found[str(movie.get("tmdb_id") or movie.get("id"))] = self._public(hit)

        return {"items": found, "library_size": self.index()["count"]}

    def _public(self, entry: dict[str, Any]) -> dict[str, Any]:
        """Ce que l'interface recoit : jamais le jeton, ni d'URL le contenant."""
        machine = self.machine_id()
        web_url = None
        if machine and entry.get("rating_key"):
            # safe="" : Plex attend une cle entierement encodee, slashs compris.
            key = urllib.parse.quote("/library/metadata/" + entry["rating_key"], safe="")
            web_url = (
                "https://app.plex.tv/desktop/#!/server/%s/details?key=%s" % (machine, key)
            )
        return {
            "title": entry["title"],
            "year": entry["year"],
            "resolution": entry["resolution"],
            "codec": entry["codec"],
            "container": entry["container"],
            "size": entry["size"],
            "view_count": entry["view_count"],
            "matched_by": entry.get("matched_by"),
            "url": web_url,
        }


# -- utilitaires ------------------------------------------------------------


def _first_part_size(media: dict[str, Any]) -> int | None:
    for part in media.get("Part") or []:
        if part.get("size"):
            try:
                return int(part["size"])
            except (TypeError, ValueError):
                return None
    return None


def _title_key(title: str, year: int | None) -> str:
    text = unicodedata.normalize("NFKD", (title or "").lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    return "%s|%s" % (text, year or "")

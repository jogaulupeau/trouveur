"""Client TMDB : decouverte de films, fiches detaillees, genres."""

from __future__ import annotations

import random
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from . import cache
from .http_client import HttpError, build_url, get_json, register_secret

BASE = "https://api.themoviedb.org/3"
IMAGE_BASE = "https://image.tmdb.org/t/p"

# Au-dela, TMDB renvoie surtout du bruit : on borne le tirage aleatoire.
MAX_RANDOM_PAGE = 20

SORTS = {
    # "hasard" pioche dans le vivier des films populaires puis melange ; les
    # autres valeurs sont des tris stricts, rendus tels quels.
    "hasard": "popularity.desc",
    "populaire": "popularity.desc",
    "note": "vote_average.desc",
    "recent": "primary_release_date.desc",
    "ancien": "primary_release_date.asc",
    "revenus": "revenue.desc",
}


# Mentions qui distinguent deux offres d'un meme service, pas deux services.
_VARIANTES = re.compile(
    r"\b(amazon channel|apple tv channel|channel|standard with ads|with ads|"
    r"avec publicite|premium|basic|essentiel|standard)\b"
)


def _service_key(name: str) -> str:
    """Ramene « Paramount+ Amazon Channel » et « Paramount Plus » a une meme cle."""
    text = unicodedata.normalize("NFKD", (name or "").lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.replace("+", " plus ")
    text = _VARIANTES.sub(" ", text)
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _reorder(movies: list[dict[str, Any]], sort: str) -> list[dict[str, Any]]:
    """Range la page sur le champ effectivement affiche.

    TMDB trie sur primary_release_date, mais la date que nous montrons est la
    sortie francaise (region=FR) : sans ce passage, un tri par anciennete
    affiche 1972, 1974, 1972, 1974. On reordonne donc sur la valeur visible,
    pour que l'ordre corresponde toujours au libelle choisi.
    """
    if sort == "note":
        return sorted(movies, key=lambda m: (-(m["rating"] or 0), -(m["votes"] or 0)))
    if sort == "recent":
        return sorted(movies, key=lambda m: m.get("release_date") or "", reverse=True)
    if sort == "ancien":
        # Une date manquante ne doit pas se retrouver en tete du classement.
        return sorted(movies, key=lambda m: m.get("release_date") or "9999")
    # populaire, revenus, hasard : l'ordre de TMDB (ou le melange) fait foi.
    return movies


class Tmdb:
    def __init__(self, config: dict[str, Any]):
        tmdb_config = config["tmdb"]
        self.api_key: str = tmdb_config["api_key"].strip()
        self.language: str = tmdb_config.get("language", "fr-FR")
        self.region: str = tmdb_config.get("region", "FR")
        self.include_adult: bool = bool(tmdb_config.get("include_adult", False))
        cache_config = config.get("cache", {})
        self.cache_ttl: int = (
            cache_config.get("ttl_seconds", 86400)
            if cache_config.get("enabled", True)
            else 0
        )
        # Une cle v4 est un JWT ; elle passe par l'en-tete Authorization.
        # Abonnements de l'utilisateur : un badge pour un service auquel il
        # n'est pas abonne ne lui apprend rien d'utile.
        self.my_services: set[int] = {
            int(i) for i in (config.get("streaming", {}).get("my_services") or [])
        }
        self._use_bearer = self.api_key.startswith("eyJ")
        register_secret(self.api_key)

    # -- transport ---------------------------------------------------------

    def _call(self, path: str, params: dict[str, Any] | None = None) -> Any:
        params = dict(params or {})
        if not self._use_bearer:
            params["api_key"] = self.api_key
        url = build_url(BASE + path, params)

        cache_key = "tmdb:" + url
        cached = cache.get(cache_key, self.cache_ttl)
        if cached is not None:
            return cached

        headers = {}
        if self._use_bearer:
            headers["Authorization"] = "Bearer " + self.api_key
        data = get_json(url, headers=headers, timeout=15)
        cache.put(cache_key, data)
        return data

    # -- lectures ----------------------------------------------------------

    def genres(self) -> list[dict[str, Any]]:
        data = self._call("/genre/movie/list", {"language": self.language})
        return data.get("genres", []) if data else []

    def providers(self, limit: int = 24, toutes: bool = False) -> list[dict[str, Any]]:
        """Plateformes de streaming disponibles dans la region, les plus
        courantes d'abord."""
        data = self._call(
            "/watch/providers/movie",
            {"language": self.language, "watch_region": self.region},
        ) or {}
        results = sorted(
            data.get("results") or [],
            key=lambda p: (p.get("display_priority", 999), p.get("provider_name") or ""),
        )
        return [
            {
                "id": p["provider_id"],
                "name": p.get("provider_name") or "",
                "logo": self._image(p.get("logo_path"), "w92"),
            }
            for p in results
            # TMDB expose un fournisseur « Plex » (id 538) qui est son service
            # gratuit, sans rapport avec le serveur Plex de l'utilisateur :
            # les presenter cote a cote ne ferait qu'embrouiller.
            if p.get("provider_id") != 538 and p.get("provider_id")
            and (toutes or not self.my_services or p["provider_id"] in self.my_services)
        ][:limit]

    def watch_providers_for(self, movie_id: int) -> set[int]:
        """Plateformes ou le film est inclus dans l'abonnement, pour la region."""
        try:
            data = self._call("/movie/%d/watch/providers" % movie_id)
        except HttpError:
            return set()
        country = ((data or {}).get("results") or {}).get(self.region) or {}
        return {
            p["provider_id"]
            for p in (country.get("flatrate") or [])
            if p.get("provider_id")
        }

    def watch_providers_detailed(self, movie_id: int) -> list[dict[str, Any]]:
        """Comme watch_providers_for, mais avec le nom et le logo pour l'affichage."""
        try:
            data = self._call("/movie/%d/watch/providers" % movie_id)
        except HttpError:
            return []
        country = ((data or {}).get("results") or {}).get(self.region) or {}
        offres = sorted(
            (p for p in (country.get("flatrate") or []) if p.get("provider_id")),
            key=lambda p: p.get("display_priority", 999),
        )

        # TMDB liste chaque variante commerciale d'un meme service : « Netflix »
        # et « Netflix Standard with Ads », « Paramount Plus » et « Paramount+
        # Amazon Channel »… Trois badges pour une seule plateforme n'apprennent
        # rien : on ne garde que l'offre principale de chaque service.
        vus: set[str] = set()
        out = []
        for offre in offres:
            if self.my_services and offre["provider_id"] not in self.my_services:
                continue
            nom = offre.get("provider_name") or ""
            cle = _service_key(nom)
            if cle in vus:
                continue
            vus.add(cle)
            out.append({
                "id": offre["provider_id"],
                "name": nom,
                "logo": self._image(offre.get("logo_path"), "w92"),
            })
        return out

    def watch_providers_detailed_bulk(
        self, ids: list[int]
    ) -> dict[int, list[dict[str, Any]]]:
        if not ids:
            return {}
        out: dict[int, list[dict[str, Any]]] = {}
        with ThreadPoolExecutor(max_workers=min(8, len(ids))) as pool:
            futures = {pool.submit(self.watch_providers_detailed, i): i for i in ids}
            for future in as_completed(futures):
                movie_id = futures[future]
                try:
                    out[movie_id] = future.result()
                except Exception:  # noqa: BLE001 - un film sans info reste affichable
                    out[movie_id] = []
        return out

    def watch_providers_bulk(self, ids: list[int]) -> dict[int, set[int]]:
        if not ids:
            return {}
        out: dict[int, set[int]] = {}
        with ThreadPoolExecutor(max_workers=min(8, len(ids))) as pool:
            futures = {pool.submit(self.watch_providers_for, i): i for i in ids}
            for future in as_completed(futures):
                movie_id = futures[future]
                try:
                    out[movie_id] = future.result()
                except Exception:  # noqa: BLE001 - un film sans info reste affichable
                    out[movie_id] = set()
        return out

    def keyword_ids(self, text: str, limit: int = 3) -> list[int]:
        if not text.strip():
            return []
        data = self._call("/search/keyword", {"query": text.strip()})
        results = (data or {}).get("results", [])[:limit]
        return [item["id"] for item in results if "id" in item]

    def _filter_availability(
        self,
        movies: list[dict[str, Any]],
        criteria: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Ne garde que les films regardables sur les plateformes demandees.

        Trois cas, de plus en plus couteux :
          - plateformes seules : deja filtre par TMDB, rien a faire ici ;
          - serveur Plex seul  : l'index local suffit, aucun appel reseau ;
          - les deux           : il faut connaitre les plateformes de chaque
            film pour former l'union, soit un appel par film (mis en cache).
        """
        providers = set(criteria.get("providers") or ())
        want_plex = bool(criteria.get("want_plex"))
        if not providers and not want_plex:
            return movies
        if providers and not want_plex:
            return movies                      # TMDB a deja filtre

        plex_ids = set(criteria.get("plex_ids") or ())
        if not providers:
            return [m for m in movies if m["id"] in plex_ids]

        besoin = [m["id"] for m in movies if m["id"] not in plex_ids]
        dispo = self.watch_providers_bulk(besoin)
        return [
            m for m in movies
            if m["id"] in plex_ids or (dispo.get(m["id"], set()) & providers)
        ]

    def _page(self, params: dict[str, Any], page: int) -> tuple[list[dict], int, int]:
        payload = self._call("/discover/movie", dict(params, page=page)) or {}
        movies = [self._card(m) for m in payload.get("results", [])]
        # TMDB refuse d'aller au-dela de la page 500.
        total_pages = min(int(payload.get("total_pages") or 1), 500)
        return movies, total_pages, int(payload.get("total_results") or 0)

    def search(self, query: str, page: int = 1) -> dict[str, Any]:
        """Recherche par titre, la plus pertinente d'abord."""
        query = query.strip()
        if not query:
            return {"movies": [], "page": 1, "next_page": None, "total_results": 0,
                    "total_pages": 0, "has_more": False, "hidden_seen": 0}

        page = min(max(1, page), 500)
        payload = self._call("/search/movie", {
            "query": query,
            "language": self.language,
            "region": self.region,
            "include_adult": str(self.include_adult).lower(),
            "page": page,
        }) or {}

        total_pages = min(int(payload.get("total_pages") or 1), 500)
        movies = [self._card(m) for m in payload.get("results", [])]
        has_more = page < total_pages
        return {
            # L'ordre de pertinence de TMDB fait foi : pas de reclassement.
            "movies": movies,
            "page": page,
            "next_page": (page + 1) if has_more else None,
            "total_pages": total_pages,
            "total_results": int(payload.get("total_results") or 0),
            "has_more": has_more,
            "hidden_seen": 0,
        }

    def recommendations(self, movie_id: int, page: int = 1) -> dict[str, Any]:
        """Films proches d'un film donne.

        /recommendations s'appuie sur les habitudes des spectateurs et donne de
        bien meilleurs resultats que /similar, purement base sur les genres et
        les mots-cles. Mais il est vide pour les films confidentiels : on garde
        /similar en repli plutot que de ne rien proposer.
        """
        page = min(max(1, page), 500)
        empty = {
            "movies": [], "page": page, "next_page": None, "total_pages": 0,
            "total_results": 0, "has_more": False, "hidden_seen": 0,
            "source": "aucune",
        }

        try:
            payload = self._call(
                "/movie/%d/recommendations" % movie_id,
                {"language": self.language, "page": page},
            ) or {}
            source = "recommandations"

            if not payload.get("results") and page == 1:
                payload = self._call(
                    "/movie/%d/similar" % movie_id,
                    {"language": self.language, "page": page},
                ) or {}
                source = "similarite"
        except HttpError as exc:
            # Film inconnu de TMDB : une liste vide vaut mieux qu'une erreur
            # technique remontee jusqu'a l'interface.
            if exc.status == 404:
                return empty
            raise

        total_pages = min(int(payload.get("total_pages") or 1), 500)
        has_more = page < total_pages
        return {
            # L'ordre de pertinence de TMDB fait foi : pas de reclassement.
            "movies": [self._card(m) for m in payload.get("results", [])],
            "page": page,
            "next_page": (page + 1) if has_more else None,
            "total_pages": total_pages,
            "total_results": int(payload.get("total_results") or 0),
            "has_more": has_more,
            "hidden_seen": 0,
            "source": source,
        }

    def discover(self, criteria: dict[str, Any]) -> dict[str, Any]:
        """Renvoie une page de resultats.

        Le tri « hasard » pioche une page au hasard a chaque appel ; les tris
        stricts servent exactement la page demandee, pour que « afficher plus »
        prolonge le classement au lieu de le rejouer.
        """
        params = self._discover_params(criteria)
        shuffle = bool(criteria.get("shuffle"))
        limit = int(criteria.get("limit") or 20)
        exclude = set(criteria.get("exclude_ids") or ())
        sort = criteria.get("sort", "note")

        # TMDB renvoie une erreur au-dela de la page 500 : on borne la demande
        # plutot que de lui transmettre un numero qu'il refusera.
        page = min(max(1, int(criteria.get("page") or 1)), 500)
        movies, total_pages, total_results = self._page(params, page)

        if page > total_pages:
            return {
                "movies": [], "page": page, "next_page": None,
                "total_pages": total_pages, "total_results": total_results,
                "hidden_seen": 0, "has_more": False,
            }

        if shuffle:
            # Au-dela, TMDB renvoie surtout du bruit : on borne le vivier.
            pool = min(total_pages, MAX_RANDOM_PAGE)
            if pool > 1:
                page = random.randint(1, pool)
                movies, total_pages, total_results = self._page(params, page)
            random.shuffle(movies)

        hidden = 0

        ignores = set(criteria.get("ignore_ids") or ())

        def tamise(lot: list[dict[str, Any]]) -> list[dict[str, Any]]:
            nonlocal hidden
            if exclude:
                avant = len(lot)
                lot = [m for m in lot if m["id"] not in exclude]
                hidden += avant - len(lot)
            if ignores:
                # Retrait silencieux : l'utilisateur a deja tranche, inutile de
                # lui annoncer qu'on respecte sa decision.
                lot = [m for m in lot if m["id"] not in ignores]
            return self._filter_availability(lot, criteria)

        movies = tamise(movies)

        # Une page trop rabotee par les filtres ne remplit plus la grille :
        # on va chercher la suite (trois pages au plus).
        last_page = page
        while len(movies) < limit and last_page < total_pages and last_page - page < 3:
            last_page += 1
            extra, _, _ = self._page(params, last_page)
            if shuffle:
                random.shuffle(extra)
            movies.extend(tamise(extra))

        # En mode hasard la page suivante n'a pas de sens : on repioche.
        has_more = total_pages > 1 if shuffle else last_page < total_pages

        # Quand le tri se fait apres la requete, le total renvoye par TMDB
        # compte des films qu'on vient d'ecarter : l'annoncer serait mentir.
        apres_coup = bool(criteria.get("want_plex"))

        return {
            "movies": _reorder(movies[:limit], sort),
            "page": page,
            "next_page": (last_page + 1) if has_more and not shuffle else None,
            "total_pages": total_pages,
            "total_results": None if apres_coup else total_results,
            "hidden_seen": hidden,
            "has_more": has_more,
        }

    def _discover_params(self, criteria: dict[str, Any]) -> dict[str, Any]:
        params: dict[str, Any] = {
            "language": self.language,
            "region": self.region,
            "include_adult": str(self.include_adult).lower(),
            "include_video": "false",
            "sort_by": SORTS.get(criteria.get("sort", "populaire"), SORTS["populaire"]),
            # Sans plancher de votes, un tri par note remonte des films notes
            # 10/10 par trois personnes.
            "vote_count.gte": criteria.get("votes_min") or 100,
        }

        genres = criteria.get("genres") or []
        if genres:
            separator = "," if criteria.get("genre_mode") == "et" else "|"
            params["with_genres"] = separator.join(str(g) for g in genres)

        excluded = criteria.get("exclude_genres") or []
        if excluded:
            params["without_genres"] = ",".join(str(g) for g in excluded)

        if criteria.get("year_min"):
            params["primary_release_date.gte"] = str(int(criteria["year_min"])) + "-01-01"
        if criteria.get("year_max"):
            params["primary_release_date.lte"] = str(int(criteria["year_max"])) + "-12-31"

        if criteria.get("rating_min"):
            params["vote_average.gte"] = float(criteria["rating_min"])
        if criteria.get("rating_max"):
            params["vote_average.lte"] = float(criteria["rating_max"])

        if criteria.get("runtime_min"):
            params["with_runtime.gte"] = int(criteria["runtime_min"])
        if criteria.get("runtime_max"):
            params["with_runtime.lte"] = int(criteria["runtime_max"])

        if criteria.get("original_language"):
            params["with_original_language"] = criteria["original_language"]

        if criteria.get("keywords"):
            params["with_keywords"] = "|".join(str(k) for k in criteria["keywords"])

        # TMDB sait filtrer lui-meme par plateforme, mais il ignore tout du
        # serveur Plex : des que celui-ci est demande, le filtre doit se faire
        # apres coup pour que l'union des deux ait un sens.
        providers = criteria.get("providers") or []
        if providers and not criteria.get("want_plex"):
            params["with_watch_providers"] = "|".join(str(p) for p in providers)
            params["watch_region"] = self.region
            params["with_watch_monetization_types"] = "flatrate"

        return params

    def card(self, movie_id: int) -> dict[str, Any] | None:
        """Fiche allegee, juste de quoi dessiner une carte.

        Sans append_to_response : la reponse est bien plus legere que celle de
        movie(), et se met en cache separement.
        """
        try:
            data = self._call("/movie/" + str(movie_id), {"language": self.language})
        except HttpError:
            # Un film retire de TMDB ne doit pas faire echouer toute une liste.
            return None
        return self._card(data or {}) if data else None

    def cards_for(self, ids: list[int]) -> dict[int, dict[str, Any]]:
        """Reconstitue les cartes d'une liste entiere, en parallele.

        Les reponses TMDB etant mises en cache 24 h, seul le premier affichage
        d'une longue liste passe reellement par le reseau.
        """
        if not ids:
            return {}
        out: dict[int, dict[str, Any]] = {}
        workers = min(8, len(ids))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self.card, mid): mid for mid in ids}
            for future in as_completed(futures):
                movie_id = futures[future]
                try:
                    card = future.result()
                except Exception:  # noqa: BLE001 - une carte perdue n'est pas fatale
                    card = None
                if card:
                    out[movie_id] = card
        return out

    def movie(self, movie_id: int) -> dict[str, Any]:
        data = self._call(
            "/movie/" + str(movie_id),
            {
                "language": self.language,
                "append_to_response": "videos,credits,external_ids,release_dates,watch/providers",
                # Sans cela, un film sans bande-annonce FR n'en renvoie aucune.
                "include_video_language": "fr,en,null",
            },
        )
        return self._detail(data or {})

    # -- normalisation -----------------------------------------------------

    def _card(self, raw: dict[str, Any]) -> dict[str, Any]:
        release = raw.get("release_date") or ""
        return {
            "id": raw.get("id"),
            "title": raw.get("title") or raw.get("original_title") or "Sans titre",
            "original_title": raw.get("original_title"),
            "year": int(release[:4]) if release[:4].isdigit() else None,
            "release_date": release,
            "poster": self._image(raw.get("poster_path"), "w500"),
            "backdrop": self._image(raw.get("backdrop_path"), "w1280"),
            "rating": round(float(raw.get("vote_average") or 0), 1),
            "votes": int(raw.get("vote_count") or 0),
            # Absent des reponses /discover et /search, present sur /movie/{id} :
            # c'est ce qui permet de filtrer par duree une liste de suggestions.
            "runtime": raw.get("runtime") or None,
            "overview": raw.get("overview") or "",
            "genre_ids": (
                raw.get("genre_ids")
                or [g["id"] for g in (raw.get("genres") or []) if "id" in g]
            ),
            "original_language": raw.get("original_language"),
        }

    def _detail(self, raw: dict[str, Any]) -> dict[str, Any]:
        card = self._card(raw)
        credits = raw.get("credits") or {}
        crew = credits.get("crew") or []

        directors = [c["name"] for c in crew if c.get("job") == "Director"]
        writers = [
            c["name"] for c in crew if c.get("job") in ("Screenplay", "Writer", "Story")
        ]
        cast = [
            {
                "name": person.get("name"),
                "character": person.get("character"),
                "photo": self._image(person.get("profile_path"), "w185"),
            }
            for person in (credits.get("cast") or [])[:10]
        ]

        external = raw.get("external_ids") or {}

        card.update(
            {
                "tagline": raw.get("tagline") or "",
                "runtime": raw.get("runtime") or None,
                "status": raw.get("status"),
                "budget": raw.get("budget") or 0,
                "revenue": raw.get("revenue") or 0,
                "genres": [g["name"] for g in (raw.get("genres") or [])],
                "countries": [
                    c.get("iso_3166_1") for c in (raw.get("production_countries") or [])
                ],
                "companies": [
                    c.get("name") for c in (raw.get("production_companies") or [])[:4]
                ],
                "directors": directors,
                "writers": list(dict.fromkeys(writers))[:4],
                "cast": cast,
                "imdb_id": external.get("imdb_id") or raw.get("imdb_id"),
                "collection": self._collection_stub(raw.get("belongs_to_collection")),
                "trailer": self._trailer(raw.get("videos") or {}),
                "certification": self._certification(raw.get("release_dates") or {}),
                "providers": self._providers(raw.get("watch/providers") or {}),
            }
        )
        return card

    def _collection_stub(self, raw: dict[str, Any] | None) -> dict[str, Any] | None:
        if not raw or not raw.get("id"):
            return None
        return {"id": raw["id"], "name": raw.get("name") or "Saga"}

    def collection(self, collection_id: int) -> dict[str, Any] | None:
        """Films d'une saga, dans l'ordre de sortie."""
        try:
            data = self._call(
                "/collection/%d" % collection_id, {"language": self.language}
            )
        except HttpError:
            return None
        if not data:
            return None

        parts = [self._card(p) for p in data.get("parts") or []]
        # Une saga se lit dans l'ordre de sortie ; les films non datés en fin.
        parts.sort(key=lambda m: m.get("release_date") or "9999")
        return {
            "id": data.get("id"),
            "name": data.get("name") or "Saga",
            "overview": data.get("overview") or "",
            "poster": self._image(data.get("poster_path"), "w500"),
            "parts": parts,
        }

    def _image(self, path: str | None, size: str) -> str | None:
        return IMAGE_BASE + "/" + size + path if path else None

    def _trailer(self, videos: dict[str, Any]) -> dict[str, Any] | None:
        results = [
            v for v in (videos.get("results") or []) if v.get("site") == "YouTube" and v.get("key")
        ]
        if not results:
            return None

        def rank(video: dict[str, Any]) -> tuple:
            # Ordre voulu : bande-annonce FR, teaser FR, bande-annonce VO, reste.
            return (
                video.get("iso_639_1") != "fr",
                video.get("type") != "Trailer",
                not video.get("official", False),
                -(video.get("size") or 0),
            )

        best = sorted(results, key=rank)[0]
        key = best["key"]
        return {
            "key": key,
            "name": best.get("name"),
            "language": best.get("iso_639_1"),
            "type": best.get("type"),
            "url": "https://www.youtube.com/watch?v=" + key,
            "embed": (
                "https://www.youtube-nocookie.com/embed/" + key
                + "?rel=0&hl=fr&cc_lang_pref=fr&modestbranding=1"
            ),
        }

    def _certification(self, release_dates: dict[str, Any]) -> str | None:
        for entry in release_dates.get("results") or []:
            if entry.get("iso_3166_1") != self.region:
                continue
            for release in entry.get("release_dates") or []:
                if release.get("certification"):
                    return release["certification"]
        return None

    def _providers(self, payload: dict[str, Any]) -> dict[str, Any]:
        country = (payload.get("results") or {}).get(self.region) or {}

        def names(bucket: str) -> list[str]:
            return [p.get("provider_name") for p in (country.get(bucket) or [])][:6]

        return {
            "link": country.get("link"),
            "flatrate": names("flatrate"),
            "rent": names("rent"),
            "buy": names("buy"),
        }

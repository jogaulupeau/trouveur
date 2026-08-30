"""Suggestions personnelles, baties sur les films deja vus.

TMDB sait dire « les spectateurs de ce film ont aussi aime… ». Il ne sait rien
de vous. Ce module fait le pont : il prend vos films vus comme graines,
agrege leurs recommandations, et classe le resultat selon vos gouts observes
(genres et epoques les plus frequents dans votre historique).

Un film recommande par plusieurs de vos films compte davantage qu'un film
recommande par un seul : c'est le signal le plus fiable dont on dispose.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

# Assez de graines pour un signal stable, assez peu pour rester rapide.
MAX_SEEDS = 25
# Nombre de graines choisies pour leur note, le reste pour leur fraicheur.
TOP_RATED_SEEDS = 15
# Sans plancher de votes, on remonte des inconnus notes 10/10 par trois personnes.
MIN_VOTES = 150
# Suggerer un film mediocre parce qu'il ressemble a ce qu'on aime dessert le propos.
MIN_RATING = 6.2
# Une saga presente plusieurs fois dans l'historique fournit plusieurs graines
# qui se renforcent : sans plafond, elle rafle tout le haut du classement.
MAX_PER_SEED = 3
RANKING_SIZE = 200
# Au-dela, les filtres couteux (duree, plateformes) feraient trop d'appels.
COSTLY_LOOKUP_CAP = 120
CACHE_TTL = 1800


def _matches(movie: dict[str, Any], f: dict[str, Any]) -> bool:
    """Criteres lisibles directement sur la carte : aucun appel reseau."""
    if f.get("rating_min") and movie["rating"] < float(f["rating_min"]):
        return False
    if f.get("votes_min") and movie["votes"] < int(f["votes_min"]):
        return False
    annee = movie.get("year")
    if f.get("year_min") and (not annee or annee < int(f["year_min"])):
        return False
    if f.get("year_max") and (not annee or annee > int(f["year_max"])):
        return False
    if f.get("original_language") and movie.get("original_language") != f["original_language"]:
        return False
    return True


def _diversify(ranked: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Empeche une meme saga de monopoliser le haut du classement.

    Les films issus d'une meme graine principale sont repousses au-dela du
    troisieme, sans etre supprimes : la liste reste complete, mais la premiere
    page cesse d'etre quatre fois le meme film.
    """
    tete: list[dict[str, Any]] = []
    queue: list[dict[str, Any]] = []
    vus: Counter[str] = Counter()

    for movie in ranked:
        cle = movie["because"][0] if movie["because"] else ""
        if vus[cle] < MAX_PER_SEED:
            vus[cle] += 1
            tete.append(movie)
        else:
            queue.append(movie)
    return tete + queue


class ForYou:
    def __init__(self, tmdb: Any):
        self.tmdb = tmdb
        self._lock = threading.Lock()
        self._ranking: list[dict[str, Any]] = []
        self._fingerprint: str = ""
        self._built_at: float = 0.0

    # -- selection des graines --------------------------------------------

    def _seeds(
        self,
        entries: list[dict[str, Any]],
        exclude: set[int],
    ) -> tuple[list[dict], dict[int, dict]]:
        """Choisit les films de depart et renvoie leurs fiches."""
        ids = [e["id"] for e in entries if isinstance(e.get("id"), int)]
        cards = self.tmdb.cards_for(ids)

        # Un genre exclu doit disparaitre du profil, pas seulement de
        # l'affichage : sinon il continue de faconner le gout mesure.
        if exclude:
            cards = {
                mid: card for mid, card in cards.items()
                if not (set(card.get("genre_ids") or ()) & exclude)
            }

        # Les mieux notes disent ce que vous aimez ; les plus recents, ce qui
        # vous occupe en ce moment. Les deux comptent.
        rated = sorted(
            (c for c in cards.values() if c["votes"] >= MIN_VOTES),
            key=lambda c: -c["rating"],
        )[:TOP_RATED_SEEDS]

        recents = []
        for entry in entries:                      # deja trie du plus recent
            card = cards.get(entry.get("id"))
            if card:
                recents.append(card)
            if len(recents) >= MAX_SEEDS:
                break

        seeds: list[dict[str, Any]] = []
        vus: set[int] = set()
        for card in rated + recents:
            if card["id"] not in vus:
                vus.add(card["id"])
                seeds.append(card)
            if len(seeds) >= MAX_SEEDS:
                break
        return seeds, cards

    def _taste(self, cards: dict[int, dict[str, Any]]) -> dict[int, float]:
        """Part de chaque genre dans l'historique, ramenee a 0..1."""
        counts: Counter[int] = Counter()
        for card in cards.values():
            counts.update(card.get("genre_ids") or [])
        if not counts:
            return {}
        top = counts.most_common(1)[0][1]
        return {genre: count / top for genre, count in counts.items()}

    # -- construction du classement ---------------------------------------

    def _build(
        self,
        entries: list[dict[str, Any]],
        seen_ids: set[int],
        exclude: set[int],
    ) -> list[dict]:
        seeds, cards = self._seeds(entries, exclude)
        if not seeds:
            return []
        taste = self._taste(cards)

        # Les recommandations de chaque graine, en parallele.
        harvest: list[tuple[dict, list[dict]]] = []
        with ThreadPoolExecutor(max_workers=min(8, len(seeds))) as pool:
            futures = {
                pool.submit(self.tmdb.recommendations, seed["id"], 1): seed
                for seed in seeds
            }
            for future in as_completed(futures):
                seed = futures[future]
                try:
                    payload = future.result()
                except Exception:  # noqa: BLE001 - une graine perdue n'est pas fatale
                    continue
                harvest.append((seed, payload.get("movies") or []))

        scored: dict[int, dict[str, Any]] = {}
        for seed, movies in harvest:
            for movie in movies:
                movie_id = movie["id"]
                if movie_id in seen_ids or movie["votes"] < MIN_VOTES:
                    continue
                if movie["rating"] < MIN_RATING:
                    continue
                if exclude and set(movie.get("genre_ids") or ()) & exclude:
                    continue
                slot = scored.get(movie_id)
                if slot is None:
                    slot = {"movie": movie, "hits": 0, "because": set()}
                    scored[movie_id] = slot
                slot["hits"] += 1
                slot["because"].add(seed["title"])

        ranked = []
        for slot in scored.values():
            movie = slot["movie"]
            genres = movie.get("genre_ids") or []
            affinity = (
                sum(taste.get(g, 0) for g in genres) / len(genres) if genres else 0
            )
            quality = max(0.0, min(1.0, (movie["rating"] - MIN_RATING) / 2.3))
            # La co-occurrence reste le signal le plus fiable, mais la qualite
            # doit peser assez pour ne pas remonter un film mediocre.
            score = 2.0 * slot["hits"] + 1.2 * affinity + 1.5 * quality
            # Trie pour que la cle de regroupement soit stable d'un calcul a l'autre.
            because = sorted(slot["because"])
            ranked.append(dict(movie, score=round(score, 3),
                               hits=slot["hits"], because=because[:4]))

        ranked.sort(key=lambda m: (-m["score"], -m["votes"]))
        return _diversify(ranked)[:RANKING_SIZE]

    # -- acces -------------------------------------------------------------

    @staticmethod
    def _fingerprint_of(seen_ids: set[int], exclude: set[int]) -> str:
        raw = "%s|%s" % (
            ",".join(str(i) for i in sorted(seen_ids)),
            ",".join(str(g) for g in sorted(exclude)),
        )
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def ranking(
        self,
        entries: list[dict[str, Any]],
        seen_ids: set[int],
        exclude: set[int] | None = None,
    ) -> list[dict]:
        """Classement complet, recalcule si l'historique ou les exclusions changent."""
        exclude = set(exclude or ())
        fingerprint = self._fingerprint_of(seen_ids, exclude)
        with self._lock:
            fresh = (
                self._fingerprint == fingerprint
                and time.time() - self._built_at < CACHE_TTL
            )
            if not fresh:
                self._ranking = self._build(entries, seen_ids, exclude)
                self._fingerprint = fingerprint
                self._built_at = time.time()
            return self._ranking

    def _apply_costly(
        self,
        ranked: list[dict[str, Any]],
        f: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Duree et plateformes : absentes des recommandations TMDB.

        Elles demandent un appel par film. On borne donc le nombre de
        candidats examines : au-dela, la premiere ouverture de l'onglet
        deviendrait interminable pour un gain nul (personne ne descend a la
        centieme suggestion).
        """
        runtime_max = f.get("runtime_max")
        runtime_min = f.get("runtime_min")
        providers = set(f.get("providers") or ())
        plex_ids = set(f.get("plex_ids") or ())
        want_plex = bool(f.get("want_plex"))

        besoin_duree = bool(runtime_max or runtime_min)
        besoin_plateformes = bool(providers or want_plex)
        if not besoin_duree and not besoin_plateformes:
            return ranked

        candidats = ranked[:COSTLY_LOOKUP_CAP]
        ids = [m["id"] for m in candidats]

        durees: dict[int, dict[str, Any]] = {}
        if besoin_duree:
            durees = self.tmdb.cards_for(ids)

        dispo: dict[int, set[int]] = {}
        if providers:
            dispo = self.tmdb.watch_providers_bulk(
                [i for i in ids if i not in plex_ids]
            )

        garde = []
        for movie in candidats:
            if besoin_duree:
                minutes = (durees.get(movie["id"]) or {}).get("runtime")
                # Une duree inconnue ne doit pas faire disparaitre le film.
                if minutes:
                    if runtime_max and minutes > int(runtime_max):
                        continue
                    if runtime_min and minutes < int(runtime_min):
                        continue
                    # On vient de la chercher : autant la garder pour l'affichage.
                    movie = dict(movie, runtime=minutes)
            if besoin_plateformes:
                sur_plex = want_plex and movie["id"] in plex_ids
                sur_plateforme = bool(dispo.get(movie["id"], set()) & providers)
                if not (sur_plex or sur_plateforme):
                    continue
            garde.append(movie)
        return garde

    def page(
        self,
        entries: list[dict[str, Any]],
        seen_ids: set[int],
        page: int = 1,
        limit: int = 20,
        include: set[int] | None = None,
        exclude: set[int] | None = None,
        genre_mode: str = "ou",
        filters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        exclude = set(exclude or ())
        include = set(include or ())
        filters = filters or {}
        ranked = self.ranking(entries, seen_ids, exclude)
        total_avant = len(ranked)

        # Les genres inclus ne filtrent que l'affichage : on continue
        # d'apprendre de tout l'historique, on ne montre qu'une partie.
        if include:
            def garde(movie: dict[str, Any]) -> bool:
                genres = set(movie.get("genre_ids") or ())
                return include <= genres if genre_mode == "et" else bool(include & genres)
            ranked = [m for m in ranked if garde(m)]

        ranked = [m for m in ranked if _matches(m, filters)]
        ranked = self._apply_costly(ranked, filters)

        page = max(1, page)
        start = (page - 1) * limit
        chunk = ranked[start:start + limit]
        has_more = start + limit < len(ranked)

        return {
            "movies": chunk,
            "page": page,
            "next_page": (page + 1) if has_more else None,
            "total_results": len(ranked),
            "total_pages": (len(ranked) + limit - 1) // limit,
            "has_more": has_more,
            "hidden_seen": 0,
            "seeds": len(entries),
            "filtered_out": total_avant - len(ranked),
        }

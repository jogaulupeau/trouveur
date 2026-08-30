"""Listes de films : « deja vus », « a voir » et « ignores ».

Un fichier par liste, plutot que le stockage du navigateur : elles survivent a
un changement de navigateur ou a un vidage de cache, et restent lisibles et
sauvegardables a la main. Le format de seen.json n'a pas change : aucune
migration n'est necessaire.

Regle de conduite de ce module : ne jamais perdre de donnees en silence. Un
fichier illisible est mis de cote au lieu d'etre ecrase, chaque ecriture laisse
une copie de secours, et une liste vide n'est jamais ecrite par-dessus une
liste qui ne l'etait pas.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
from datetime import datetime, timezone
from typing import Any

# Surchargeables individuellement, afin que les tests ne touchent jamais les
# vraies listes ; voir trouveur/paths.py.
from .paths import ignored_path, seen_path, watchlist_path

STORE_PATH = seen_path()
WATCHLIST_PATH = watchlist_path()
IGNORED_PATH = ignored_path()


class MovieList:
    """Une liste de films persistee dans un fichier JSON."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    # -- fichier -----------------------------------------------------------

    @property
    def _backup(self) -> str:
        return self.path + ".backup"

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"version": 1, "movies": {}}

    @staticmethod
    def _valid(data: Any) -> bool:
        return isinstance(data, dict) and isinstance(data.get("movies"), dict)

    def _load_file(self, path: str) -> dict[str, Any] | None:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return None
        return data if self._valid(data) else None

    def _quarantine(self) -> None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        try:
            shutil.move(self.path, "%s.corrompu-%s" % (self.path, stamp))
        except OSError:
            pass

    def _read(self) -> dict[str, Any]:
        data = self._load_file(self.path)
        if data is not None:
            return data

        backup = self._load_file(self._backup)
        if os.path.exists(self.path):
            # Present mais inexploitable : on le conserve sous un autre nom.
            self._quarantine()
        return backup if backup is not None else self._empty()

    def _write(self, data: dict[str, Any]) -> None:
        tmp = "%s.%d.tmp" % (self.path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

        # Copie de secours APRES coup : elle doit refleter le dernier etat valide.
        try:
            shutil.copy2(self.path, self._backup)
        except OSError:
            pass

    # -- lecture -----------------------------------------------------------

    def all(self) -> dict[str, Any]:
        """Films de la liste, du plus recemment ajoute au plus ancien."""
        movies = list(self._read()["movies"].values())
        movies.sort(key=lambda m: m.get("seen_at") or "", reverse=True)
        return {"movies": movies, "count": len(movies)}

    def ids(self) -> set[int]:
        out: set[int] = set()
        for key in self._read()["movies"]:
            try:
                out.add(int(key))
            except (TypeError, ValueError):
                continue
        return out

    def plex_ignores(self) -> set[int]:
        """Films retires a la main : la synchro Plex ne doit pas les remettre."""
        raw = self._read().get("plex_ignores") or []
        return {int(i) for i in raw if str(i).lstrip("-").isdigit()}

    # -- ecriture ----------------------------------------------------------

    @staticmethod
    def _entry(movie: dict[str, Any], source: str) -> dict[str, Any]:
        movie_id = movie.get("id")
        if not isinstance(movie_id, int):
            raise ValueError("Identifiant de film manquant")
        return {
            "id": movie_id,
            "title": str(movie.get("title") or "")[:300],
            "year": movie.get("year") if isinstance(movie.get("year"), int) else None,
            "poster": str(movie.get("poster") or "")[:500] or None,
            "seen_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": source,
        }

    def mark(self, movie: dict[str, Any], source: str = "manuel") -> dict[str, Any]:
        entry = self._entry(movie, source)
        with self._lock:
            data = self._read()
            existing = data["movies"].get(str(entry["id"]))
            if existing:
                # Re-marquer ne rajeunit pas la date, et un marquage manuel
                # prime sur une provenance automatique.
                entry["seen_at"] = existing.get("seen_at") or entry["seen_at"]
                if existing.get("source") == "manuel":
                    entry["source"] = "manuel"
            # Un ajout volontaire annule un refus precedent.
            ignores = [i for i in (data.get("plex_ignores") or []) if int(i) != entry["id"]]
            if ignores or "plex_ignores" in data:
                data["plex_ignores"] = ignores
            data["movies"][str(entry["id"])] = entry
            self._write(data)
        return entry

    def unmark(self, movie_id: int) -> bool:
        with self._lock:
            data = self._read()
            removed = data["movies"].pop(str(movie_id), None)
            if removed is None:
                return False
            if removed.get("source") == "plex":
                # Sans cela, la prochaine synchro le remettrait aussitot.
                ignores = set(data.get("plex_ignores") or [])
                ignores.add(movie_id)
                data["plex_ignores"] = sorted(ignores)
            self._write(data)
        return True

    def import_movies(self, movies: dict[str, Any]) -> dict[str, Any]:
        """Fusionne une liste importee. N'ecrase jamais une entree existante.

        Un film deja present garde sa date et sa provenance : reimporter une
        vieille sauvegarde ne doit pas rajeunir l'historique ni effacer ce qui
        a ete fait depuis.
        """
        ajoutes, ignores, invalides = 0, 0, 0
        with self._lock:
            data = self._read()
            for cle, valeur in (movies or {}).items():
                if not isinstance(valeur, dict):
                    invalides += 1
                    continue
                try:
                    movie_id = int(valeur.get("id", cle))
                except (TypeError, ValueError):
                    invalides += 1
                    continue
                if str(movie_id) in data["movies"]:
                    ignores += 1
                    continue
                entree = self._entry(
                    {"id": movie_id, "title": valeur.get("title"),
                     "year": valeur.get("year"), "poster": valeur.get("poster")},
                    valeur.get("source") or "import",
                )
                # La date d'origine vaut mieux que celle de l'import.
                if isinstance(valeur.get("seen_at"), str):
                    entree["seen_at"] = valeur["seen_at"]
                data["movies"][str(movie_id)] = entree
                ajoutes += 1
            if ajoutes:
                self._write(data)
        return {"added": ajoutes, "skipped": ignores, "invalid": invalides,
                "total": len(self.ids())}

    def sync_from_plex(self, watched: list[dict[str, Any]]) -> dict[str, Any]:
        """Ajoute les films lus sur Plex. N'enleve jamais rien."""
        with self._lock:
            data = self._read()
            ignores = {int(i) for i in (data.get("plex_ignores") or [])}
            added = []
            for movie in watched:
                movie_id = movie.get("id")
                if not isinstance(movie_id, int):
                    continue
                if str(movie_id) in data["movies"] or movie_id in ignores:
                    continue
                data["movies"][str(movie_id)] = self._entry(movie, "plex")
                added.append(movie_id)
            if added:
                self._write(data)
        return {"added": len(added), "ids": added}


SEEN = MovieList(STORE_PATH)
WATCHLIST = MovieList(WATCHLIST_PATH)
# Films ecartes volontairement : ils ne reapparaissent plus dans les
# propositions, mais restent consultables et reversibles depuis leur onglet.
IGNORED = MovieList(IGNORED_PATH)


# -- compatibilite avec les appels existants --------------------------------

def all_seen() -> dict[str, Any]:
    return SEEN.all()


def ids() -> set[int]:
    return SEEN.ids()


def mark(movie: dict[str, Any]) -> dict[str, Any]:
    return SEEN.mark(movie)


def unmark(movie_id: int) -> bool:
    return SEEN.unmark(movie_id)

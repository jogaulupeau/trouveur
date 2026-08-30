#!/usr/bin/env python3
"""Trouveur — serveur local.

Sert l'interface web et fait le pont vers TMDB et le tracker. Les cles d'API
restent cote serveur : le navigateur ne les voit jamais.

    python server.py            # demarre sur http://127.0.0.1:8777
    python server.py --port 9000
    python server.py --clear-cache
    python server.py --plex-login   # connexion Plex, sans copier de jeton
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from trouveur import cache, certificates, config as config_module, seen
from trouveur.http_client import HttpError
from trouveur.certificates import CertificateError
from trouveur.deluge import Deluge, DelugeError
from trouveur.plex import Plex, PlexError
from trouveur.plex_auth import PlexAuthError
from trouveur.reco import ForYou
from trouveur import settings as settings_module
from trouveur.tmdb import Tmdb
from trouveur.tracker import Tracker, TrackerError

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(ROOT, "web")

CONFIG: dict[str, Any] = {}
TMDB: Tmdb
TRACKER: Tracker
PLEX: Plex
DELUGE: Deluge
FORYOU: ForYou


def reload_services() -> None:
    """Reconstruit les clients apres un changement de reglages.

    Sans cela il faudrait redemarrer pour qu'une cle saisie dans l'interface
    prenne effet — ce qui, dans un add-on sans terminal, serait penible.
    """
    global CONFIG, TMDB, TRACKER, PLEX, DELUGE, FORYOU
    CONFIG = config_module.load()
    TMDB = Tmdb(CONFIG)
    TRACKER = Tracker(CONFIG)
    PLEX = Plex(CONFIG)
    DELUGE = Deluge(CONFIG)
    FORYOU = ForYou(TMDB)

    if not config_module.is_configured(CONFIG):
        print("Aucune cle TMDB : l'interface s'ouvrira sur l'ecran de configuration.")


class Handler(BaseHTTPRequestHandler):
    server_version = "Trouveur"
    protocol_version = "HTTP/1.1"

    # -- routage -----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - impose par BaseHTTPRequestHandler
        parsed = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(parsed.path)
        params = urllib.parse.parse_qs(parsed.query)

        try:
            if path.startswith("/api/"):
                self._handle_api(path, params)
            else:
                self._serve_static(path)
        except BrokenPipeError:
            pass  # onglet ferme pendant la reponse
        except Exception as exc:  # noqa: BLE001 - le serveur ne doit pas tomber
            self._send_json({"error": str(exc)}, status=500)

    def do_POST(self) -> None:  # noqa: N802 - impose par BaseHTTPRequestHandler
        parsed = urllib.parse.urlsplit(self.path)
        chemin = parsed.path.rstrip("/")
        try:
            if chemin == "/api/settings":
                resultat = settings_module.update(self._read_json_body())
                reload_services()
                self._send_json(resultat)
                return
            if chemin == "/api/certificates":
                corps = self._read_json_body()
                resultat = _deposer_certificat(corps)
                reload_services()   # le contexte TLS de Deluge doit relire le fichier
                self._send_json(resultat)
                return
            if chemin == "/api/deluge/add":
                corps = self._read_json_body()
                self._send_json(_envoyer_vers_deluge(corps))
                return
            if chemin == "/api/import":
                corps = self._read_json_body()
                store = _store_for("/api/" + str(corps.get("list") or ""))
                if store is None:
                    self._send_json({"error": "Liste inconnue"}, status=400)
                    return
                movies = corps.get("movies")
                if not isinstance(movies, dict):
                    self._send_json(
                        {"error": "Fichier inattendu : un objet « movies » est attendu."},
                        status=400)
                    return
                self._send_json(store.import_movies(movies))
                return
            if chemin == "/api/plex/login/start":
                self._send_json(settings_module.plex_login_start())
                return
            if chemin == "/api/plex/login/finish":
                corps = self._read_json_body()
                resultat = settings_module.plex_login_finish(
                    int(corps.get("id") or 0), int(corps.get("index") or 0))
                if resultat.get("status") == "connecte":
                    reload_services()
                self._send_json(resultat)
                return
        except PlexAuthError as exc:
            self._send_json({"error": str(exc)}, status=502)
            return
        except DelugeError as exc:
            self._send_json({"error": str(exc)}, status=502)
            return
        except CertificateError as exc:
            self._send_json({"error": str(exc)}, status=400)
            return
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=400)
            return
        except Exception as exc:  # noqa: BLE001 - le serveur ne doit pas tomber
            self._send_json({"error": str(exc)}, status=500)
            return

        store = _store_for(parsed.path)
        if store is None:
            self._send_json({"error": "Route inconnue"}, status=404)
            return
        try:
            payload = self._read_json_body()
            self._send_json(store.mark(payload))
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=400)
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001 - le serveur ne doit pas tomber
            self._send_json({"error": str(exc)}, status=500)

    def do_DELETE(self) -> None:  # noqa: N802 - impose par BaseHTTPRequestHandler
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path.rstrip("/") == "/api/certificates":
            role = _one(urllib.parse.parse_qs(parsed.query), "role", "")
            try:
                retire = certificates.remove(role)
                _oublier_certificat(role)
                reload_services()
                self._send_json({"removed": retire})
            except CertificateError as exc:
                self._send_json({"error": str(exc)}, status=400)
            return

        base, _, movie_id = parsed.path.rpartition("/")
        store = _store_for(base)
        if store is None:
            self._send_json({"error": "Route inconnue"}, status=404)
            return
        if not movie_id.isdigit():
            self._send_json({"error": "Identifiant de film invalide"}, status=400)
            return
        try:
            self._send_json({"removed": store.unmark(int(movie_id))})
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001 - le serveur ne doit pas tomber
            self._send_json({"error": str(exc)}, status=500)

    def _read_json_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ValueError("En-tete Content-Length invalide") from None
        if length <= 0:
            raise ValueError("Corps de requete vide")
        # Assez large pour un certificat encode en base64.
        if length > 512 * 1024:
            raise ValueError("Corps de requete trop volumineux")
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValueError("Corps de requete JSON invalide") from None
        if not isinstance(body, dict):
            raise ValueError("Un objet JSON est attendu")
        return body

    def _handle_api(self, path: str, params: dict[str, list[str]]) -> None:
        try:
            if path == "/api/bootstrap":
                self._send_json(self._bootstrap())
            elif path == "/api/discover":
                self._send_json(self._discover(params))
            elif path.startswith("/api/movie/"):
                movie_id = path.rsplit("/", 1)[-1]
                if not movie_id.isdigit():
                    self._send_json({"error": "Identifiant de film invalide"}, status=400)
                    return
                self._send_json(TMDB.movie(int(movie_id)))
            elif path == "/api/torrents":
                self._send_json(self._torrents(params))
            elif path == "/api/torrent":
                self._send_torrent_file(params)
            elif path == "/api/deluge/test":
                self._send_json(DELUGE.test())
            elif path == "/api/settings":
                self._send_json(settings_module.current())
            elif path == "/api/plex/login/poll":
                pin = _int(params, "id")
                if not pin:
                    self._send_json({"error": "Demande manquante"}, status=400)
                    return
                self._send_json(_sans_interne(settings_module.plex_login_poll(pin)))
            elif path == "/api/providers":
                self._send_json(self._providers(_one(params, "all", "0") == "1"))
            elif path == "/api/foryou":
                self._send_json(FORYOU.page(
                    seen.SEEN.all()["movies"],
                    seen.SEEN.ids() | seen.IGNORED.ids(),
                    page=_int(params, "page") or 1,
                    include=set(_int_list(params.get("genres", []))),
                    exclude=set(_int_list(params.get("exclude_genres", []))),
                    genre_mode=_one(params, "genre_mode", "ou"),
                    filters=self._foryou_filters(params)))
            elif path == "/api/collection":
                collection_id = _int(params, "id")
                if not collection_id:
                    self._send_json({"error": "Identifiant de saga manquant"}, status=400)
                    return
                data = TMDB.collection(collection_id)
                self._send_json(data or {"parts": [], "name": None})
            elif path == "/api/similar":
                movie_id = _int(params, "id")
                if not movie_id:
                    self._send_json({"error": "Identifiant de film manquant"}, status=400)
                    return
                proches = TMDB.recommendations(movie_id, _int(params, "page") or 1)
                ecartes = seen.IGNORED.ids()
                proches["movies"] = [m for m in proches["movies"] if m["id"] not in ecartes]
                self._send_json(proches)
            elif path == "/api/search":
                self._send_json(TMDB.search(_one(params, "q", ""), _int(params, "page") or 1))
            elif path == "/api/seen":
                self._send_json(_list_payload(seen.SEEN))
            elif path == "/api/watchlist":
                self._send_json(_list_payload(seen.WATCHLIST))
            elif path == "/api/ignored":
                self._send_json(_list_payload(seen.IGNORED))
            elif path == "/api/plex/sync":
                self._send_json(sync_plex_watched())
            elif path == "/api/plex/refresh":
                self._send_json(refresh_plex_library())
            elif path == "/api/availability":
                self._send_json(self._availability(params))
            else:
                self._send_json({"error": "Route inconnue"}, status=404)
        except HttpError as exc:
            status = 502
            message = str(exc)
            if exc.status == 401:
                message = "TMDB refuse la cle d'API. Verifie tmdb.api_key dans config.json."
                status = 401
            self._send_json({"error": message}, status=status)
        except TrackerError as exc:
            self._send_json({"error": str(exc)}, status=502)
        except PlexError as exc:
            self._send_json({"error": str(exc)}, status=502)
        except DelugeError as exc:
            self._send_json({"error": str(exc)}, status=502)

    # -- points d'entree ---------------------------------------------------

    def _bootstrap(self) -> dict[str, Any]:
        # Sans cle valide, TMDB repond 401. Le demarrage de l'interface ne doit
        # pas en dependre : c'est precisement le cas ou il faut pouvoir ouvrir
        # l'ecran de configuration.
        try:
            genres = TMDB.genres() if config_module.is_configured(CONFIG) else []
        except HttpError:
            genres = []

        return {
            "genres": genres,
            "region": TMDB.region,
            "tracker": {
                "enabled": TRACKER.configured,
                "name": TRACKER.name,
            },
            "seen": sorted(seen.SEEN.ids()),
            "watchlist": sorted(seen.WATCHLIST.ids()),
            "ignored": sorted(seen.IGNORED.ids()),
            "plex": {"enabled": PLEX.configured, "sync": PLEX.sync_watched},
            "deluge": {"enabled": DELUGE.configured},
            "configured": config_module.is_configured(CONFIG),
        }

    def _foryou_filters(self, params: dict[str, list[str]]) -> dict[str, Any]:
        """Criteres du panneau applicables a un classement personnel.

        L'epoque, la note, la popularite, la langue, la duree et les
        plateformes ont un sens ici. Le tri, lui, est exclu : l'onglet a son
        propre ordre, c'est tout son interet.
        """
        demandees = params.get("providers", [])
        filters: dict[str, Any] = {
            "rating_min": _float(params, "rating_min"),
            "votes_min": _int(params, "votes_min"),
            "year_min": _int(params, "year_min"),
            "year_max": _int(params, "year_max"),
            "runtime_min": _int(params, "runtime_min"),
            "runtime_max": _int(params, "runtime_max"),
            "original_language": _one(params, "original_language", ""),
            "providers": _int_list(demandees),
            "want_plex": any("plex" in v.split(",") for v in demandees),
        }
        if filters["want_plex"]:
            if not PLEX.configured:
                filters["want_plex"] = False
            else:
                filters["plex_ids"] = set(PLEX.index()["by_tmdb"])
        return filters

    def _providers(self, toutes: bool = False) -> dict[str, Any]:
        """Plateformes proposees a l'utilisateur, son serveur Plex en tete.

        « toutes » ignore le filtre des abonnements : c'est ce dont l'ecran de
        reglages a besoin pour laisser choisir ses abonnements.
        """
        plateformes = []
        if PLEX.configured:
            # Ton serveur n'est pas un fournisseur TMDB : il est traite a part,
            # mais presente au meme niveau que les autres.
            plateformes.append({"id": "plex", "name": "Mon serveur Plex", "logo": None,
                                "local": True})
        plateformes.extend(dict(p, local=False) for p in TMDB.providers(toutes=toutes))
        return {"providers": plateformes, "region": TMDB.region}

    def _discover(self, params: dict[str, list[str]]) -> dict[str, Any]:
        criteria: dict[str, Any] = {
            "genres": _int_list(params.get("genres", [])),
            "exclude_genres": _int_list(params.get("exclude_genres", [])),
            "genre_mode": _one(params, "genre_mode", "ou"),
            "year_min": _int(params, "year_min"),
            "year_max": _int(params, "year_max"),
            "rating_min": _float(params, "rating_min"),
            "votes_min": _int(params, "votes_min"),
            "runtime_min": _int(params, "runtime_min"),
            "runtime_max": _int(params, "runtime_max"),
            "original_language": _one(params, "original_language", ""),
            "sort": _one(params, "sort", "note"),
            "limit": _int(params, "limit") or 20,
            "page": _int(params, "page") or 1,
        }
        # Un tri explicite doit etre rendu tel quel : seul "hasard" melange.
        criteria["shuffle"] = criteria["sort"] == "hasard"

        demandees = params.get("providers", [])
        criteria["providers"] = _int_list(demandees)
        criteria["want_plex"] = any("plex" in v.split(",") for v in demandees)
        if criteria["want_plex"]:
            if not PLEX.configured:
                criteria["want_plex"] = False
            else:
                # L'index Plex est deja en memoire : le filtre ne coute rien.
                criteria["plex_ids"] = set(PLEX.index()["by_tmdb"])
        if _one(params, "hide_seen", "0") == "1":
            criteria["exclude_ids"] = seen.SEEN.ids()
        # Un film ecarte volontairement ne doit plus jamais remonter, et ce
        # retrait n'a pas a etre annonce : c'est une decision deja prise.
        criteria["ignore_ids"] = seen.IGNORED.ids()

        keyword_text = _one(params, "keyword", "")
        if keyword_text:
            ids = TMDB.keyword_ids(keyword_text)
            if not ids:
                return {
                    "movies": [],
                    "total_results": 0,
                    "notice": 'Aucun mot-cle TMDB ne correspond a "%s".' % keyword_text,
                }
            criteria["keywords"] = ids

        return TMDB.discover(criteria)

    def _availability(self, params: dict[str, list[str]]) -> dict[str, Any]:
        """Ou regarder chaque film d'une grille : serveur Plex et plateformes.

        Un seul appel pour toute la page. Le volet Plex est gratuit (index en
        memoire) ; le volet plateformes coute un appel TMDB par film, mis en
        cache 24 h, d'ou le remplissage differe cote interface.
        """
        # Parametres repetes (id=…&title=…&year=…), alignes par position : le
        # titre sert de repli quand Plex n'a pas d'identifiant TMDB pour un film.
        ids = params.get("id", [])
        titles = params.get("title", [])
        years = params.get("year", [])

        movies = []
        for position, raw_id in enumerate(ids):
            if not raw_id.isdigit():
                continue
            year = years[position] if position < len(years) else ""
            movies.append({
                "tmdb_id": int(raw_id),
                "title": titles[position] if position < len(titles) else "",
                "year": int(year) if year.isdigit() else None,
            })
        plex = PLEX.availability(movies) if PLEX.configured else {"items": {}}
        plateformes = TMDB.watch_providers_detailed_bulk([m["tmdb_id"] for m in movies])

        items: dict[str, Any] = {}
        for movie in movies:
            movie_id = movie["tmdb_id"]
            entree = {
                "plex": plex["items"].get(str(movie_id)),
                # Trois suffisent : au-dela la carte devient illisible.
                "providers": plateformes.get(movie_id, [])[:3],
            }
            if entree["plex"] or entree["providers"]:
                items[str(movie_id)] = entree

        return {"items": items, "plex_enabled": PLEX.configured}

    def _torrents(self, params: dict[str, list[str]]) -> dict[str, Any]:
        title = _one(params, "title", "").strip()
        if not title:
            return {"torrents": [], "error": "Titre manquant"}
        if not TRACKER.configured:
            return {"torrents": [], "error": None, "disabled": True}
        return TRACKER.search_movie(
            tmdb_id=_int(params, "tmdb_id"),
            title=title,
            year=_int(params, "year"),
            original_title=_one(params, "original_title", "").strip() or None,
        )

    def _send_torrent_file(self, params: dict[str, list[str]]) -> None:
        """Relaie le .torrent en ajoutant la cle : elle ne passe pas par la page."""
        slug = _one(params, "slug", "").strip()
        if not slug:
            self._send_json({"error": "Torrent manquant"}, status=400)
            return
        try:
            body = TRACKER.torrent_file(slug)
        except TrackerError as exc:
            self._send_json({"error": str(exc)}, status=502)
        except PlexError as exc:
            self._send_json({"error": str(exc)}, status=502)
        except DelugeError as exc:
            self._send_json({"error": str(exc)}, status=502)
            return
        except CertificateError as exc:
            self._send_json({"error": str(exc)}, status=400)
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/x-bittorrent")
        self.send_header("Content-Length", str(len(body)))
        self.send_header(
            "Content-Disposition", 'attachment; filename="%s.torrent"' % slug[:120]
        )
        self.end_headers()
        self.wfile.write(body)

    # -- fichiers statiques ------------------------------------------------

    def _serve_static(self, path: str) -> None:
        relative = "index.html" if path in ("/", "") else path.lstrip("/")
        target = os.path.normpath(os.path.join(WEB_DIR, relative))
        # Empeche toute remontee hors de web/ via ../
        if not target.startswith(WEB_DIR) or not os.path.isfile(target):
            self._send_bytes(b"404 - introuvable", "text/plain; charset=utf-8", 404)
            return

        content_type, _ = mimetypes.guess_type(target)
        if content_type in ("text/html", "text/css", "application/javascript", "text/javascript"):
            content_type += "; charset=utf-8"
        with open(target, "rb") as fh:
            body = fh.read()
        self._send_bytes(body, content_type or "application/octet-stream", 200)

    # -- reponses ----------------------------------------------------------

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(body, "application/json; charset=utf-8", status)

    def _send_bytes(self, body: bytes, content_type: str, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        if os.environ.get("TROUVEUR_VERBOSE"):
            super().log_message(fmt, *args)


def _list_payload(store: seen.MovieList) -> dict[str, Any]:
    """Renvoie une liste sous forme de cartes completes.

    Le stockage ne retient qu'un identifiant, un titre et une date : c'est
    volontaire, pour que la note et l'affiche ne s'y periment pas. Les cartes
    sont donc reconstituees depuis TMDB (mis en cache 24 h) au moment de
    l'affichage, sans quoi les films importes de Plex apparaissent nus.
    """
    entries = store.all()["movies"]
    cards = TMDB.cards_for([e["id"] for e in entries if isinstance(e.get("id"), int)])

    movies = []
    for entry in entries:
        card = cards.get(entry.get("id"))
        if card:
            # Les metadonnees de la liste priment sur celles de la carte.
            movies.append(dict(card, seen_at=entry.get("seen_at"), source=entry.get("source")))
        else:
            # TMDB muet ou film retire : on montre au moins ce qu'on sait.
            movies.append({
                "id": entry.get("id"), "title": entry.get("title") or "Titre inconnu",
                "year": entry.get("year"), "poster": entry.get("poster"),
                "rating": 0, "votes": 0, "genre_ids": [], "overview": "",
                "seen_at": entry.get("seen_at"), "source": entry.get("source"),
                "incomplete": True,
            })
    return {"movies": movies, "count": len(movies)}


# Chaque role de certificat alimente une cle de configuration.
_CERTIFICATS = {
    "client": "client_cert",
    "client_key": "client_key",
    "ca": "ca_cert",
}


def _deposer_certificat(corps: dict[str, Any]) -> dict[str, Any]:
    """Enregistre un certificat envoye depuis l'interface et note son chemin."""
    role = str(corps.get("role") or "")
    if role not in _CERTIFICATS:
        raise CertificateError("Type de certificat inconnu.")

    depose = certificates.store(role, str(corps.get("filename") or ""),
                                str(corps.get("data") or ""))
    brut = config_module.load_raw() or {}
    brut.setdefault("deluge", {})[_CERTIFICATS[role]] = depose["path"]
    config_module.save(brut)
    return {"role": role, "name": depose["name"], "size": depose["size"]}


def _oublier_certificat(role: str) -> None:
    brut = config_module.load_raw() or {}
    if brut.get("deluge", {}).get(_CERTIFICATS[role]):
        brut["deluge"][_CERTIFICATS[role]] = ""
        config_module.save(brut)


def _envoyer_vers_deluge(corps: dict[str, Any]) -> dict[str, Any]:
    """Recupere le .torrent aupres du tracker puis le confie a Deluge.

    Le fichier transite par Trouveur, qui seul detient la cle du tracker :
    Deluge n'a besoin ni de cette cle, ni d'un acces au tracker.
    """
    slug = str(corps.get("slug") or "").strip()
    if not slug:
        raise ValueError("Torrent manquant")
    if not DELUGE.configured:
        return {"added": False, "message": "Deluge n'est pas configuré."}

    data = TRACKER.torrent_file(slug)
    return DELUGE.add_torrent_file(slug[:120] + ".torrent", data)


def _sans_interne(payload: dict[str, Any]) -> dict[str, Any]:
    """Retire les cles de service (prefixe _) avant l'envoi au navigateur."""
    return {k: v for k, v in payload.items() if not k.startswith("_")}


def _store_for(path: str) -> seen.MovieList | None:
    """Associe une route a sa liste. Les deux partagent le meme comportement."""
    return {
        "/api/seen": seen.SEEN,
        "/api/watchlist": seen.WATCHLIST,
        "/api/ignored": seen.IGNORED,
    }.get(path.rstrip("/"))


def sync_plex_watched() -> dict[str, Any]:
    """Reporte les films lus sur Plex dans la liste des deja vus.

    Sens unique, et purement additif : rien n'est jamais retire, et un film
    retire a la main n'est pas remis a la synchro suivante.
    """
    if not PLEX.configured:
        return {"added": 0, "disabled": True}
    try:
        watched = PLEX.watched_movies()
    except PlexError as exc:
        return {"added": 0, "error": str(exc)}
    result = seen.SEEN.sync_from_plex(watched)
    result["watched_on_plex"] = len(watched)
    return result


def refresh_plex_library() -> dict[str, Any]:
    """Relit la bibliotheque Plex sans attendre l'expiration de l'inventaire."""
    if not PLEX.configured:
        return {"disabled": True}
    try:
        return PLEX.refresh()
    except PlexError as exc:
        return {"error": str(exc)}


def _one(params: dict[str, list[str]], key: str, default: str) -> str:
    values = params.get(key)
    return values[0] if values else default


def _int(params: dict[str, list[str]], key: str) -> int | None:
    raw = _one(params, key, "")
    try:
        return int(raw)
    except ValueError:
        return None


def _float(params: dict[str, list[str]], key: str) -> float | None:
    raw = _one(params, key, "")
    try:
        return float(raw)
    except ValueError:
        return None


def _int_list(values: list[str]) -> list[int]:
    out: list[int] = []
    for value in values:
        for part in value.split(","):
            part = part.strip()
            if part.isdigit():
                out.append(int(part))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Trouveur — propositions de films")
    parser.add_argument("--port", type=int, help="port d'ecoute")
    parser.add_argument("--host", help="adresse d'ecoute")
    parser.add_argument("--no-browser", action="store_true", help="ne pas ouvrir le navigateur")
    parser.add_argument("--clear-cache", action="store_true", help="vider le cache puis quitter")
    parser.add_argument("--plex-login", action="store_true",
                        help="se connecter a Plex et remplir config.json")
    args = parser.parse_args()

    if args.clear_cache:
        print("Cache vide : %d entrees supprimees." % cache.clear())
        return 0

    if args.plex_login:
        from trouveur import plex_setup
        return plex_setup.run()

    global CONFIG, TMDB, TRACKER, PLEX, DELUGE, FORYOU
    try:
        CONFIG = config_module.load()
    except config_module.ConfigError as exc:
        print("Configuration : %s" % exc, file=sys.stderr)
        return 1

    TMDB = Tmdb(CONFIG)
    TRACKER = Tracker(CONFIG)
    PLEX = Plex(CONFIG)
    DELUGE = Deluge(CONFIG)
    FORYOU = ForYou(TMDB)

    if not config_module.is_configured(CONFIG):
        print("Aucune cle TMDB : l'interface s'ouvrira sur l'ecran de configuration.")

    host = args.host or CONFIG["server"]["host"]
    port = args.port or int(CONFIG["server"]["port"])
    url = "http://%s:%d/" % (host, port)

    try:
        httpd = ThreadingHTTPServer((host, port), Handler)
    except OSError as exc:
        print("Impossible d'ecouter sur %s : %s" % (url, exc), file=sys.stderr)
        return 1

    print("Trouveur en ligne sur %s" % url)
    print("Tracker %s : %s" % (TRACKER.name, "actif" if TRACKER.configured else "inactif (voir config.json)"))
    print("Plex : %s" % ("actif" if PLEX.configured else "inactif (voir config.json)"))
    print("Ctrl+C pour arreter.")

    if PLEX.configured and PLEX.sync_watched:
        # En tache de fond : indexer une bibliotheque distante prend du temps,
        # et l'interface doit rester disponible pendant ce temps-la.
        def _sync() -> None:
            resultat = sync_plex_watched()
            if resultat.get("error"):
                print("Synchro Plex : %s" % resultat["error"])
            elif resultat.get("added"):
                print("Synchro Plex : %d films ajoutes aux deja vus (%d lus sur Plex)."
                      % (resultat["added"], resultat.get("watched_on_plex", 0)))
        threading.Thread(target=_sync, daemon=True).start()

    if not args.no_browser and CONFIG["server"].get("open_browser", True):
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nArret.")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

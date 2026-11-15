"""Verifications de l'adaptateur tracker : tr4ker.net et v3x.club.

Lancement :  python tests/test_tracker.py

Aucun reseau : un faux indexeur Torznab repond en local. Les deux trackers
parlent le meme protocole ; ce qui est verifie ici, ce sont les trois points ou
ils different — authentification, adresse, et provenance du lien de
telechargement — ainsi que la regle qui prime sur tout : la cle d'API ne sort
jamais du serveur.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trouveur import config as cfg          # noqa: E402
from trouveur import tracker as tracker_module   # noqa: E402
from trouveur.tracker import Tracker, TrackerError  # noqa: E402

PORT = 8884
LOCAL = "http://127.0.0.1:%d" % PORT
CLE = "CLE-SECRETE-1234567890"

ok: list[bool] = []


def check(label: str, condition: object, detail: str = "") -> None:
    ok.append(bool(condition))
    print("%-56s %s%s" % (label, "OK" if condition else "ECHEC",
                          "  " + detail if detail and not condition else ""))


# -- un faux indexeur Torznab ------------------------------------------------

# tr4ker : pas d'<enclosure>, l'identifiant se lit sur la page de details.
FLUX_TR4KER = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:torznab="http://torznab.com/schemas/2015/feed"><channel>
<item><title>Heat.1995.MULTi.2160p.UHD.BluRay.REMUX.HEVC-GRP</title>
<comments>https://tr4ker.net/torrent/heat-1995-multi-2160p-abc123</comments>
<link>https://tr4ker.net/torrent/heat-1995-multi-2160p-abc123</link>
<torznab:attr name="category" value="2000"/>
<torznab:attr name="size" value="64424509440"/>
<torznab:attr name="seeders" value="17"/>
<torznab:attr name="leechers" value="3"/>
<torznab:attr name="tmdbid" value="949"/>
<torznab:attr name="downloadvolumefactor" value="0"/></item>
</channel></rss>"""

# v3x : l'adresse du .torrent est publiee dans le flux, cle comprise.
FLUX_V3X = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:torznab="http://torznab.com/schemas/2015/feed"><channel>
<item><title>Heat.1995.MULTi.1080p.BluRay.x264-V3X</title>
<comments>https://v3x.club/torrents/4242</comments>
<enclosure url="{LOCAL}/api/download/4242?apikey={CLE}"
           length="18253611008" type="application/x-bittorrent"/>
<torznab:attr name="category" value="2040"/>
<torznab:attr name="size" value="18253611008"/>
<torznab:attr name="seeders" value="42"/>
<torznab:attr name="leechers" value="1"/>
<torznab:attr name="tmdbid" value="949"/>
<torznab:attr name="downloadvolumefactor" value="1"/></item>
</channel></rss>""".replace("{CLE}", CLE).replace("{LOCAL}", LOCAL)

recu: list[dict] = []
FLUX = {"courant": FLUX_TR4KER}


class FauxIndexeur(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        parse = urllib.parse.urlsplit(self.path)
        recu.append({
            "chemin": parse.path,
            "params": urllib.parse.parse_qs(parse.query),
            "entetes": {k.lower(): v for k, v in self.headers.items()},
        })

        if "/download" in parse.path:
            corps, type_mime = b"d8:announce...e", "application/x-bittorrent"
        else:
            corps, type_mime = FLUX["courant"].encode("utf-8"), "application/xml"

        self.send_response(200)
        self.send_header("Content-Type", type_mime)
        self.send_header("Content-Length", str(len(corps)))
        self.end_headers()
        self.wfile.write(corps)


srv = ThreadingHTTPServer(("127.0.0.1", PORT), FauxIndexeur)
threading.Thread(target=srv.serve_forever, daemon=True).start()


def construire(profile: str, **extra) -> Tracker:
    conf = json.loads(json.dumps(cfg.DEFAULTS))
    conf["cache"]["enabled"] = False
    conf["tracker"] = dict({"enabled": True, "profile": profile,
                            "api_key": CLE, "base_url": LOCAL}, **extra)
    return Tracker(conf)


# -- les profils -------------------------------------------------------------

check("les deux trackers sont proposes",
      {"tr4ker.net", "v3x.club", "torznab"} <= set(tracker_module.PROFILS))

t = Tracker({"tracker": {"enabled": True, "profile": "v3x.club", "api_key": CLE}})
check("le profil fournit l'adresse", t.base_url == "https://api.v3x.club", t.base_url)
check("le profil fournit l'authentification", t.auth == "bearer", t.auth)

# Ce qui est ecrit en clair doit toujours gagner sur le profil.
t = Tracker({"tracker": {"enabled": True, "profile": "v3x.club", "api_key": CLE,
                         "base_url": "https://miroir.example"}})
check("une adresse explicite l'emporte sur le profil",
      t.base_url == "https://miroir.example", t.base_url)

# -- authentification --------------------------------------------------------

recu.clear()
FLUX["courant"] = FLUX_TR4KER
tr4ker = construire("tr4ker.net", base_url=LOCAL)
tr4ker.search_movie(tmdb_id=949, title="Heat", year=1995)
entetes = recu[0]["entetes"]
check("tr4ker : cle brute dans X-Api-Key", entetes.get("x-api-key") == CLE,
      str(entetes))
check("tr4ker : rien dans l'URL", "apikey" not in recu[0]["params"],
      str(recu[0]["params"]))

recu.clear()
FLUX["courant"] = FLUX_V3X
v3x = construire("v3x.club", base_url=LOCAL)
v3x.search_movie(tmdb_id=949, title="Heat", year=1995)
entetes = recu[0]["entetes"]
check("v3x : jeton Bearer", entetes.get("authorization") == "Bearer " + CLE,
      str(entetes))
check("v3x : la cle ne passe pas en query", "apikey" not in recu[0]["params"],
      str(recu[0]["params"]))
check("v3x : recherche par tmdbid", recu[0]["params"].get("tmdbid") == ["949"],
      str(recu[0]["params"]))

# -- la cle ne sort jamais du serveur ---------------------------------------

FLUX["courant"] = FLUX_V3X
v3x = construire("v3x.club", base_url=LOCAL)
charge = v3x.search_movie(tmdb_id=949, title="Heat", year=1995)
rendu = json.dumps(charge)
check("un resultat est renvoye", len(charge["torrents"]) == 1, rendu[:200])
check("la cle d'API n'apparait nulle part dans la reponse", CLE not in rendu,
      "la cle fuite vers le navigateur")
check("l'adresse de telechargement est retiree de la reponse",
      "download" not in charge["torrents"][0], str(charge["torrents"][0].keys()))
check("un identifiant opaque subsiste", charge["torrents"][0]["slug"] == "4242",
      str(charge["torrents"][0].get("slug")))

# -- telechargement : chaque tracker par sa voie ----------------------------

recu.clear()
v3x.torrent_file("4242")
check("v3x : l'adresse vient du flux",
      recu[-1]["chemin"] == "/api/download/4242", recu[-1]["chemin"])
check("v3x : la cle du flux est conservee",
      recu[-1]["params"].get("apikey") == [CLE], str(recu[-1]["params"]))

recu.clear()
FLUX["courant"] = FLUX_TR4KER
tr4ker = construire("tr4ker.net", base_url=LOCAL)
tr4ker.search_movie(tmdb_id=949, title="Heat", year=1995)
recu.clear()
tr4ker.torrent_file("heat-1995-multi-2160p-abc123")
check("tr4ker : l'adresse est reconstruite depuis le profil",
      recu[-1]["chemin"] == "/api/torrents/heat-1995-multi-2160p-abc123/download",
      recu[-1]["chemin"])

# Redemarrage : v3x ne publie son adresse que dans les resultats de recherche.
# Mieux vaut le dire que fabriquer une adresse au hasard.
neuf = construire("v3x.club", base_url=LOCAL)
try:
    neuf.torrent_file("4242")
    check("v3x : une adresse oubliee est signalee", False, "aucune erreur")
except TrackerError as exc:
    check("v3x : une adresse oubliee est signalee",
          "Relance la recherche" in str(exc), str(exc))

# -- garde-fous conserves ----------------------------------------------------

try:
    tr4ker.torrent_file("../../etc/passwd")
    check("un identifiant douteux est refuse", False, "accepte")
except TrackerError as exc:
    check("un identifiant douteux est refuse", "invalide" in str(exc), str(exc))

check("un tracker sans cle n'est pas configure",
      not Tracker({"tracker": {"enabled": True, "profile": "v3x.club"}}).configured)

# -- changer de tracker ------------------------------------------------------
# config.load() fusionne DEFAULTS par-dessus le fichier : si une adresse propre
# a un tracker y trainait, elle ressusciterait apres la bascule.

from trouveur import settings as settings_module   # noqa: E402

_ancienne_config = {}


def _faux_load_raw():
    return json.loads(json.dumps(_ancienne_config))


def _faux_save(nouvelle):
    _ancienne_config.clear()
    _ancienne_config.update(nouvelle)


settings_module.config_module.load_raw = _faux_load_raw
settings_module.config_module.save = _faux_save

# Une installation existante, avec les valeurs de tr4ker ecrites en clair.
_ancienne_config = {"tracker": {
    "enabled": True, "profile": "tr4ker.net", "name": "tr4ker.net",
    "base_url": "https://tr4ker.net", "api_path": "/api", "auth": "header",
    "header_name": "X-Api-Key", "api_key": CLE,
    "download_path": "/api/torrents/{id}/download",
}}

settings_module.update({"tracker": {"profile": "v3x.club"}})
apres = _ancienne_config["tracker"]
check("la bascule retient le nouveau profil", apres["profile"] == "v3x.club",
      str(apres))
check("l'ancienne adresse est effacee", "base_url" not in apres, str(apres))
check("l'ancien mode d'authentification est efface", "auth" not in apres,
      str(apres))
check("la cle d'API n'est pas perdue en chemin", apres.get("api_key") == CLE)

# Et c'est bien v3x qui parle ensuite, defauts fusionnes compris.
_fusion = cfg._deep_merge(cfg.DEFAULTS, _ancienne_config)
_apres = Tracker(_fusion)
check("apres bascule, l'adresse est celle de v3x",
      _apres.base_url == "https://api.v3x.club", _apres.base_url)
check("apres bascule, l'authentification est celle de v3x",
      _apres.auth == "bearer", _apres.auth)
# Le profil par defaut s'appelle « tr4ker.net », c'est voulu. Ce qui ne doit
# pas s'y trouver, ce sont les valeurs qu'un changement de tracker rend fausses.
_propres_au_tracker = {"base_url", "api_path", "auth", "header_name",
                       "download_path", "name"}
check("les defauts ne portent aucune valeur propre a un tracker",
      not (_propres_au_tracker & set(cfg.DEFAULTS["tracker"])),
      str(sorted(_propres_au_tracker & set(cfg.DEFAULTS["tracker"]))))

# -- le diagnostic -----------------------------------------------------------

CAPS = """<?xml version="1.0" encoding="UTF-8"?>
<caps><categories>
  <category id="2000" name="Movies"><subcat id="2040" name="Movies/HD"/></category>
</categories></caps>"""

_etat = {"caps": 200}


class FauxAvecCaps(FauxIndexeur):
    def do_GET(self):
        parse = urllib.parse.urlsplit(self.path)
        params = urllib.parse.parse_qs(parse.query)
        if params.get("t") == ["caps"]:
            recu.append({"chemin": parse.path, "params": params,
                         "entetes": {k.lower(): v for k, v in self.headers.items()}})
            code = _etat["caps"]
            corps = (CAPS if code == 200 else "nope").encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/xml")
            self.send_header("Content-Length", str(len(corps)))
            self.end_headers()
            self.wfile.write(corps)
            return
        FauxIndexeur.do_GET(self)


srv.shutdown()
srv = ThreadingHTTPServer(("127.0.0.1", PORT), FauxAvecCaps)
threading.Thread(target=srv.serve_forever, daemon=True).start()

FLUX["courant"] = FLUX_V3X
_diag = construire("v3x.club", base_url=LOCAL)
_r = _diag.test()
check("le diagnostic aboutit", _r["ok"] is True, json.dumps(_r, ensure_ascii=False))
check("il deroule trois etapes", len(_r["steps"]) == 3, str(_r["steps"]))
check("les categories sont lues", "Movies" in (_r.get("categories") or []),
      str(_r.get("categories")))

# Cle refusee : le message doit parler du scope, pas d'une panne.
_etat["caps"] = 403
_r = construire("v3x.club", base_url=LOCAL).test()
_erreur = next((e.get("erreur", "") for e in _r["steps"] if not e["ok"]), "")
check("une cle refusee est nommee comme telle", "refuse la clé" in _erreur, _erreur)
check("le scope torznab est rappele", "torznab" in _erreur, _erreur)

# Mauvaise adresse : la reponse n'est pas du Torznab.
_etat["caps"] = 500
_r = construire("v3x.club", base_url=LOCAL).test()
check("un echec laisse ok a faux", _r["ok"] is False, json.dumps(_r, ensure_ascii=False))
_etat["caps"] = 200

# Tracker incomplet : le dire avant d'appeler quoi que ce soit.
_vide = Tracker({"tracker": {"enabled": True, "profile": "v3x.club"}})
_r = _vide.test()
check("un tracker sans cle est signale sans appel reseau",
      _r["ok"] is False and "incomplet" in _r.get("message", ""), str(_r))

srv.shutdown()
print("\n%d/%d verifications passees" % (sum(ok), len(ok)))
sys.exit(0 if all(ok) else 1)

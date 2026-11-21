"""Le lancement d'un film sur un lecteur de Home Assistant.

Lancement :  python tests/test_homeassistant.py

Un faux Home Assistant tourne en local (port libre choisi par le systeme) : rien
ne sort sur le reseau et aucun vrai appareil n'est touche. On verifie ce qui part
vers lui -- le bon service, la bonne entite, le bon identifiant -- et ce qui ne
doit pas partir : une commande vers un lecteur qui regarde peut-etre quelque
chose, sans confirmation ; le jeton dans un message d'erreur.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

from trouveur import homeassistant as ha  # noqa: E402

ok: list[bool] = []


def check(label: str, condition: object, detail: str = "") -> None:
    ok.append(bool(condition))
    print("%-66s %s%s" % (label, "OK" if condition else "ECHEC",
                          "  " + detail if detail and not condition else ""))


JETON = "jeton-secret-0123456789"

ETATS = {
    "media_player.lecteur_salon": {
        "state": "idle", "attributes": {"friendly_name": "Lecteur du salon",
                                        "device_class": "tv", "app_name": "Plex"}},
    "media_player.autre": {
        "state": "playing", "attributes": {"friendly_name": "Autre", "media_title": "Un film"}},
    "media_player.absent": {"state": "unavailable", "attributes": {"friendly_name": "Absent"}},
    "light.salon": {"state": "on", "attributes": {"friendly_name": "Salon"}},
}
APPELS: list[dict] = []
ECHEC = {"message": None}


class FauxHomeAssistant(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _envoyer(self, statut, corps):
        brut = json.dumps(corps).encode()
        self.send_response(statut)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(brut)))
        self.end_headers()
        self.wfile.write(brut)

    def _autorise(self):
        if self.headers.get("Authorization") != "Bearer " + JETON:
            self._envoyer(401, {"message": "401: Unauthorized"})
            return False
        return True

    def do_GET(self):
        if not self._autorise():
            return
        if self.path == "/api/states":
            return self._envoyer(200, [dict(entity_id=k, **v) for k, v in ETATS.items()])
        entite = self.path.rsplit("/", 1)[-1]
        if self.path.startswith("/api/states/") and entite in ETATS:
            return self._envoyer(200, dict(entity_id=entite, **ETATS[entite]))
        self._envoyer(404, {"message": "Entity not found."})

    def do_POST(self):
        if not self._autorise():
            return
        longueur = int(self.headers.get("Content-Length") or 0)
        corps = json.loads(self.rfile.read(longueur) or b"{}")
        if ECHEC["message"]:
            # Home Assistant peut citer l'en-tete recu dans un message : on le simule.
            return self._envoyer(500, {"message": ECHEC["message"].replace("{jeton}", JETON)})
        APPELS.append({"chemin": self.path, "corps": corps})
        self._envoyer(200, [])


serveur = ThreadingHTTPServer(("127.0.0.1", 0), FauxHomeAssistant)
threading.Thread(target=serveur.serve_forever, daemon=True).start()
os.environ["TROUVEUR_HA_URL"] = "http://127.0.0.1:%d/api" % serveur.server_address[1]
os.environ["TROUVEUR_HA_TOKEN"] = JETON
os.environ.pop("SUPERVISOR_TOKEN", None)

FILM = {"title": "L'Été meurtrier", "year": 1983, "library": "Films", "rating_key": "4242"}


def client(methode="plex", player="media_player.lecteur_salon"):
    return ha.HomeAssistant({"homeassistant": {
        "player": player, "player_name": "Lecteur du salon", "method": methode}})


# -- les lecteurs ---------------------------------------------------------------
lecteurs = client().players()
check("seuls les lecteurs multimedia sont listes",
      [p["entity_id"] for p in lecteurs] == [
          "media_player.absent", "media_player.autre", "media_player.lecteur_salon"],
      str(lecteurs))
check("le nom affiche est le nom convivial de Home Assistant",
      lecteurs[2]["name"] == "Lecteur du salon", str(lecteurs[2]))

fiche = client().describe()
check("la description montre l'etat et les attributs utiles",
      fiche["state"] == "idle" and fiche["attributes"].get("app_name") == "Plex", str(fiche))

# -- une entite qui n'est pas un lecteur n'est jamais retenue -----------------------------
check("« light.salon » n'est pas un lecteur valide",
      not ha.entite_valide("light.salon") and client(player="light.salon").player == "")
check("une entite bien formee est acceptee", ha.entite_valide("media_player.lecteur_salon"))
check("une valeur louche est refusee",
      not ha.entite_valide("media_player.x; drop") and not ha.entite_valide(None))

# -- plex : l'identifiant nomme la bibliotheque et le titre ----------------------------------
APPELS.clear()
resultat = client("plex").launch(FILM, None)
appel = APPELS[0]
contenu = appel["corps"]["media_content_id"]
check("le lancement appelle media_player.play_media",
      appel["chemin"] == "/api/services/media_player/play_media" and resultat["status"] == "ok",
      str(appel))
check("il vise le lecteur choisi",
      appel["corps"]["entity_id"] == "media_player.lecteur_salon")
check("l'identifiant commence par plex:// et type « movie »",
      contenu.startswith("plex://") and appel["corps"]["media_content_type"] == "movie", contenu)
recherche = json.loads(contenu[len("plex://"):])
check("il nomme la bibliotheque, le titre exact et l'annee",
      recherche == {"library_name": "Films", "title": "L'Été meurtrier", "year": 1983},
      str(recherche))
check("les accents partent tels quels, pas echappes", "Été" in contenu, contenu)

try:
    client("plex").launch(dict(FILM, library=""), None)
    check("sans bibliotheque connue, le lancement refuse", False)
except ha.HomeAssistantError as exc:
    check("sans bibliotheque connue, le lancement refuse avec un message", "bibliotheque" in str(exc), str(exc))

# -- lien : lien profond vers le film ---------------------------------------------------------
APPELS.clear()
client("lien").launch(FILM, "MACHINE123")
corps = APPELS[0]["corps"]
check("le lien profond porte l'identifiant du serveur et du film",
      corps["media_content_type"] == "url" and corps["media_content_id"] ==
      "plex://server://MACHINE123/com.plexapp.plugins.library/library/metadata/4242", str(corps))

# -- app : ouvre seulement Plex -----------------------------------------------------------------
APPELS.clear()
client("app").launch(FILM, None)
corps = APPELS[0]["corps"]
check("« app » ouvre l'application Plex, sans choisir de film",
      corps["media_content_type"] == "app" and corps["media_content_id"] == ha.PLEX_ANDROID, str(corps))

# -- un lecteur occupe : on demande avant de remplacer ---------------------------------------------
APPELS.clear()
occupe = ha.HomeAssistant({"homeassistant": {"player": "media_player.autre", "method": "plex"}})
reponse = occupe.launch(FILM, None)
check("un lecteur qui lit quelque chose demande confirmation",
      reponse["status"] == "busy" and reponse["title"] == "Un film", str(reponse))
check("et rien n'est envoye tant qu'on n'a pas confirme", APPELS == [], str(APPELS))
reponse = occupe.launch(FILM, None, confirm=True)
check("confirme, la commande part", reponse["status"] == "ok" and len(APPELS) == 1, str(reponse))

# -- un lecteur injoignable ------------------------------------------------------------------
APPELS.clear()
absent = ha.HomeAssistant({"homeassistant": {
    "player": "media_player.absent", "player_name": "Lecteur absent", "method": "plex"}})
try:
    absent.launch(FILM, None, confirm=True)
    check("un lecteur indisponible n'est pas commande", False)
except ha.HomeAssistantError as exc:
    check("un lecteur indisponible est signale par son nom, sans commande",
          "Lecteur absent" in str(exc) and APPELS == [], str(exc))

# -- le message de Home Assistant remonte, jamais le jeton ------------------------------------
ECHEC["message"] = "Plex integration not configured. Authorization: Bearer {jeton}"
try:
    client("plex").launch(FILM, None)
    check("une erreur de Home Assistant est remontee", False)
except ha.HomeAssistantError as exc:
    check("le message de Home Assistant est conserve", "Plex integration not configured" in str(exc), str(exc))
    check("le jeton n'apparait jamais dans l'erreur", JETON not in str(exc), str(exc))
ECHEC["message"] = None

# -- sans acces a Home Assistant -------------------------------------------------------------
os.environ.pop("TROUVEUR_HA_URL")
os.environ.pop("TROUVEUR_HA_TOKEN")
seul = ha.HomeAssistant({"homeassistant": {"player": "media_player.x", "method": "plex"}})
check("sans jeton, Home Assistant n'est pas disponible et rien ne part",
      not seul.available and not seul.ready and not ha.disponible())
try:
    seul.players()
    check("sans acces, lister les lecteurs donne une explication", False)
except ha.HomeAssistantError as exc:
    check("sans acces, lister les lecteurs donne une explication", "joignable" in str(exc), str(exc))

serveur.shutdown()
print("\n%d/%d verifications passees" % (sum(ok), len(ok)))
sys.exit(0 if all(ok) else 1)

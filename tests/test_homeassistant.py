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
    # Entrees de l'integration Plex : une par application Plex ouverte.
    "media_player.plex_salon": {"state": "idle", "attributes": {"friendly_name": "Plex (Salon)"}},
    "media_player.plex_ferme": {"state": "unavailable", "attributes": {"friendly_name": "Plex (Ferme)"}},
    # Android TV Remote : allume ou eteint, jamais « playing ».
    "media_player.boitier": {"state": "on", "attributes": {"friendly_name": "Boitier"}},
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

FILM = {"title": "L'Été meurtrier", "year": 1983, "library": "Films", "rating_key": "4242",
        "web_url": "https://app.plex.tv/desktop/#!/server/MACHINE123/details?key=%2Flibrary%2Fmetadata%2F4242"}


def client(methode="plex", player="media_player.lecteur_salon"):
    return ha.HomeAssistant({"homeassistant": {
        "player": player, "player_name": "Lecteur du salon", "method": methode}})


# -- les lecteurs ---------------------------------------------------------------
lecteurs = client().players()
check("seuls les lecteurs multimedia sont listes",
      [p["entity_id"] for p in lecteurs] == [
          "media_player.absent", "media_player.autre", "media_player.boitier",
          "media_player.lecteur_salon", "media_player.plex_ferme", "media_player.plex_salon"],
      str(lecteurs))
check("le nom affiche est le nom convivial de Home Assistant",
      lecteurs[3]["name"] == "Lecteur du salon", str(lecteurs[3]))

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

# -- client Plex : le JSON seul, sans prefixe ---------------------------------------------------
APPELS.clear()
plex_client = ha.HomeAssistant({"homeassistant": {
    "player": "media_player.plex_salon", "player_name": "Plex (Salon)", "method": "plex_client"}})
reponse = plex_client.launch(FILM, None)
corps = APPELS[0]["corps"]
check("« plex_client » envoie le JSON seul : pas de prefixe plex://",
      not corps["media_content_id"].startswith("plex://")
      and json.loads(corps["media_content_id"]) == {
          "library_name": "Films", "title": "L'Été meurtrier", "year": 1983}
      and corps["media_content_type"] == "movie" and corps["entity_id"] == "media_player.plex_salon",
      str(corps))
check("une entree Plex au repos est commandee directement (rien ne s'y joue)",
      reponse["status"] == "ok" and reponse["method"] == "plex_client", str(reponse))
APPELS.clear()
client("plex").launch(FILM, None)
check("« plex » (Chromecast) garde le prefixe, lui",
      APPELS[0]["corps"]["media_content_id"].startswith("plex://{"), str(APPELS[0]))

APPELS.clear()
ferme = ha.HomeAssistant({"homeassistant": {
    "player": "media_player.plex_ferme", "player_name": "Plex (Ferme)", "method": "plex_client"}})
try:
    ferme.launch(FILM, None)
    check("une entree Plex indisponible n'est pas commandee", False)
except ha.HomeAssistantError as exc:
    check("une entree Plex indisponible dit d'ouvrir Plex sur l'appareil, sans commande",
          "ouverte" in str(exc) and "Plex" in str(exc) and APPELS == [], str(exc))

# -- lien web : le lien https de Plex ----------------------------------------------------------
APPELS.clear()
client("lien_web").launch(FILM, "MACHINE123")
corps = APPELS[0]["corps"]
check("« lien_web » envoie le lien https de Plex en type url",
      corps["media_content_type"] == "url" and corps["media_content_id"] == FILM["web_url"], str(corps))

# -- les autres formes de lien Plex ---------------------------------------------------------------
for forme in ("preplay", "play"):
    APPELS.clear()
    client(forme).launch(FILM, "MACHINE123")
    contenu = APPELS[0]["corps"]["media_content_id"]
    check("« %s » : lien plex://%s/ avec la cle encodee et le serveur" % (forme, forme),
          contenu == "plex://%s/?metadataKey=%%2Flibrary%%2Fmetadata%%2F4242&metadataType=1&server=MACHINE123" % forme
          and APPELS[0]["corps"]["media_content_type"] == "url", contenu)

APPELS.clear()
client("intent").launch(FILM, "MACHINE123")
contenu = APPELS[0]["corps"]["media_content_id"]
check("« intent » : un intent Android avec l'indicateur de lecture de Plex",
      contenu.startswith("intent://server://MACHINE123/") and "scheme=plex" in contenu
      and "package=com.plexapp.android" in contenu
      and "B.android.intent.extra.START_PLAYBACK=true" in contenu and contenu.endswith(";end"), contenu)

for forme in ("lien", "preplay", "play", "intent"):
    try:
        client(forme).launch(FILM, None)
        check("« %s » sans identifiant de serveur : refuse" % forme, False)
    except ha.HomeAssistantError as exc:
        check("« %s » sans identifiant de serveur : refuse avec un message" % forme,
              "serveur" in str(exc), str(exc))

# -- essayer une autre methode que celle des reglages, pour une fois ---------------------------
APPELS.clear()
reponse = client("lien").launch(FILM, "MACHINE123", method="preplay")
check("une methode demandee pour un essai remplace celle des reglages, une fois",
      reponse["method"] == "preplay"
      and APPELS[0]["corps"]["media_content_id"].startswith("plex://preplay/"), str(reponse))
APPELS.clear()
reponse = client("lien").launch(FILM, "MACHINE123", method="n-importe-quoi")
check("une methode inconnue demandee retombe sur celle des reglages",
      reponse["method"] == "lien"
      and APPELS[0]["corps"]["media_content_id"].startswith("plex://server://"), str(reponse))

# -- la methode par defaut convient a Android TV Remote (app, url, channel) ------------------------
par_defaut = ha.HomeAssistant({"homeassistant": {"player": "media_player.lecteur_salon"}})
check("sans reglage, la methode est le lien Plex, pas la commande Chromecast",
      par_defaut.method == "lien" and par_defaut.confirm_on is True, par_defaut.method)
check("une methode inconnue retombe sur le lien",
      ha.HomeAssistant({"homeassistant": {"method": "n-importe-quoi"}}).method == "lien")
check("« plex_client » est une methode connue",
      ha.HomeAssistant({"homeassistant": {"method": "plex_client"}}).method == "plex_client")
check("« plex » seul n'est pas un type accepte par Android TV Remote : il n'est pas le defaut",
      "plex" in ha.METHODES and ha.METHODES[0] != "plex")

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

# -- un boitier Android TV Remote : allume, sans savoir s'il lit quelque chose ------------------
APPELS.clear()
boitier = ha.HomeAssistant({"homeassistant": {"player": "media_player.boitier", "method": "lien"}})
reponse = boitier.launch(FILM, "MACHINE123")
check("un lecteur allume demande confirmation, meme sans titre en cours",
      reponse["status"] == "busy" and reponse["reason"] == "allume" and reponse["title"] is None,
      str(reponse))
check("et rien n'est envoye tant qu'on n'a pas confirme", APPELS == [], str(APPELS))
check("confirme, la commande part", boitier.launch(FILM, "MACHINE123", confirm=True)["status"] == "ok"
      and len(APPELS) == 1)

APPELS.clear()
libre = ha.HomeAssistant({"homeassistant": {
    "player": "media_player.boitier", "method": "lien", "confirm_on": False}})
check("avec la case decochee, un lecteur allume est commande directement",
      libre.launch(FILM, "MACHINE123")["status"] == "ok" and len(APPELS) == 1)
APPELS.clear()
reponse = ha.HomeAssistant({"homeassistant": {
    "player": "media_player.autre", "method": "lien", "confirm_on": False}}).launch(FILM, "M")
check("la case decochee ne desactive pas la protection d'un lecteur qui lit",
      reponse["status"] == "busy" and reponse["reason"] == "lecture" and APPELS == [], str(reponse))

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

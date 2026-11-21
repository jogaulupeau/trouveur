"""Pont vers Home Assistant : lister les lecteurs, lancer un film sur l'un d'eux.

Dans l'add-on, Home Assistant fournit un jeton a l'add-on (SUPERVISOR_TOKEN) et
l'API est joignable sur http://supervisor/core/api : rien a copier. Hors add-on,
pour essayer en local, TROUVEUR_HA_URL et TROUVEUR_HA_TOKEN jouent ce role.

Rien n'est ecrit en dur sur l'installation de quelqu'un : le lecteur vise est un
reglage, choisi dans la liste que Home Assistant donne lui-meme.

Plusieurs facons de lancer, au choix, parce qu'elles ne conviennent pas au meme
materiel et qu'on ne peut pas deviner de loin laquelle convient :

  plex_client  l'entite « Plex (...) » que l'integration Plex de Home Assistant cree
            pour une application Plex ouverte : play_media avec un JSON
            {"library_name", "title"} SANS prefixe. C'est la voie documentee, la
            seule qui ne passe pas par un lien. L'entite n'existe que quand
            l'application Plex est ouverte sur l'appareil.

  lien      un lien profond vers le film, ouvert dans l'application Plex.
            L'integration Android TV Remote n'accepte que « app », « url » et
            « channel » : c'est la methode qui lui convient.
  lien_web  le meme, avec le lien web de Plex (https://app.plex.tv/...).
  preplay   plex://preplay/?metadataKey=...&server=... : la fiche du film.
  play      plex://play/?metadataKey=...&server=... : la lecture.
  intent    un « intent: » Android avec l'indicateur de lecture de Plex
            (android.intent.extra.START_PLAYBACK). Experimental.
  app       ouvre seulement l'application Plex. Ne choisit pas le film.
  plex      un Chromecast, par l'integration Plex : le meme JSON, AVEC le prefixe
            « plex:// ». Ne fonctionne ni avec Android TV Remote ni avec une
            entite « Plex (...) ».

Android TV Remote ne dit pas si quelque chose est en cours de lecture : son etat
est « on » ou « off ». Pour ne pas couper un film a quelqu'un, un lecteur allume
demande donc confirmation avant le lancement (reglable).
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
from typing import Any

from .http_client import HttpError, get_json, post_json, redact, register_secret

METHODES = ("plex_client", "lien", "lien_web", "preplay", "play", "intent", "app", "plex")

# Methodes dont le lien nomme le serveur et le film : il leur faut l'identifiant
# du serveur Plex.
AVEC_SERVEUR = ("lien", "preplay", "play", "intent")
PLEX_ANDROID = "com.plexapp.android"
URL_SUPERVISEUR = "http://supervisor/core/api"

# Etats ou quelqu'un regarde peut-etre quelque chose : on demande avant de remplacer.
OCCUPE = ("playing", "paused", "buffering")

ENTITE = re.compile(r"^media_player\.[a-z0-9_]{1,80}$")

# Ce qu'on laisse voir d'un lecteur : de quoi comprendre ce qu'il sait faire,
# sans rien de superflu.
ATTRIBUTS_VISIBLES = (
    "friendly_name", "device_class", "supported_features", "app_name", "app_id",
    "media_title", "source", "source_list",
)


class HomeAssistantError(RuntimeError):
    pass


def _acces() -> tuple[str, str]:
    """(adresse, jeton), ou deux chaines vides quand Home Assistant n'est pas la."""
    jeton = os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("TROUVEUR_HA_TOKEN") or ""
    adresse = os.environ.get("TROUVEUR_HA_URL") or (
        URL_SUPERVISEUR if os.environ.get("SUPERVISOR_TOKEN") else "")
    return adresse.rstrip("/"), jeton


def disponible() -> bool:
    adresse, jeton = _acces()
    return bool(adresse and jeton)


def entite_valide(valeur: Any) -> bool:
    return isinstance(valeur, str) and bool(ENTITE.match(valeur))


class HomeAssistant:
    def __init__(self, config: dict[str, Any]):
        bloc = config.get("homeassistant") or {}
        self.player: str = bloc.get("player") if entite_valide(bloc.get("player")) else ""
        self.player_name: str = str(bloc.get("player_name") or "")[:80]
        self.method: str = bloc.get("method") if bloc.get("method") in METHODES else "lien"
        # Vrai par defaut : mieux vaut un clic de trop qu'un film coupe.
        self.confirm_on: bool = bloc.get("confirm_on", True) is not False
        self._base, self._jeton = _acces()
        register_secret(self._jeton)

    @property
    def available(self) -> bool:
        return bool(self._base and self._jeton)

    @property
    def ready(self) -> bool:
        return self.available and bool(self.player)

    # -- transport ---------------------------------------------------------

    def _entetes(self) -> dict[str, str]:
        return {"Authorization": "Bearer " + self._jeton}

    def _get(self, chemin: str) -> Any:
        if not self.available:
            raise HomeAssistantError(
                "Home Assistant n'est pas joignable d'ici. Dans l'add-on l'acces est "
                "automatique ; en local, TROUVEUR_HA_URL et TROUVEUR_HA_TOKEN le donnent.")
        try:
            return get_json(self._base + chemin, headers=self._entetes(), timeout=10)
        except HttpError as exc:
            raise HomeAssistantError(_message(exc)) from exc

    def _post(self, chemin: str, donnees: dict[str, Any]) -> Any:
        try:
            return post_json(
                self._base + chemin, headers=self._entetes(), timeout=20,
                body=json.dumps(donnees).encode("utf-8"))
        except HttpError as exc:
            raise HomeAssistantError(_message(exc)) from exc

    # -- lecture -----------------------------------------------------------

    def players(self) -> list[dict[str, Any]]:
        etats = self._get("/states") or []
        liste = []
        for etat in etats:
            entite = str(etat.get("entity_id") or "")
            if not entite.startswith("media_player."):
                continue
            nom = (etat.get("attributes") or {}).get("friendly_name") or entite
            liste.append({"entity_id": entite, "name": str(nom), "state": etat.get("state")})
        return sorted(liste, key=lambda p: p["name"].lower())

    def describe(self, entite: str | None = None) -> dict[str, Any]:
        """Ce que le lecteur annonce savoir faire, tel que Home Assistant le dit."""
        entite = entite or self.player
        if not entite_valide(entite):
            raise HomeAssistantError("Aucun lecteur choisi.")
        etat = self._get("/states/" + entite) or {}
        attributs = etat.get("attributes") or {}
        visibles = {cle: attributs[cle] for cle in ATTRIBUTS_VISIBLES if cle in attributs}
        if isinstance(visibles.get("source_list"), list):
            visibles["source_list"] = visibles["source_list"][:30]
        return {"entity_id": entite, "state": etat.get("state"), "attributes": visibles}

    # -- lancement ---------------------------------------------------------

    def commande(self, film: dict[str, Any], machine: str | None,
                 methode: str | None = None) -> dict[str, Any]:
        """Les donnees envoyees a media_player.play_media, selon la methode."""
        methode = methode if methode in METHODES else self.method
        if methode in AVEC_SERVEUR and not (machine and film.get("rating_key")):
            raise HomeAssistantError(
                "Il manque l'identifiant du serveur ou du film pour faire le lien.")
        cle = film.get("rating_key")
        if methode in ("preplay", "play"):
            # Meme forme que les liens que Plex publie : cle de metadonnees encodee.
            return {
                "entity_id": self.player,
                "media_content_type": "url",
                "media_content_id": "plex://%s/?metadataKey=%s&metadataType=1&server=%s" % (
                    methode, urllib.parse.quote("/library/metadata/%s" % cle, safe=""), machine),
            }
        if methode == "intent":
            return {
                "entity_id": self.player,
                "media_content_type": "url",
                "media_content_id": (
                    "intent://server://%s/com.plexapp.plugins.library/library/metadata/%s"
                    "#Intent;scheme=plex;package=%s;B.android.intent.extra.START_PLAYBACK=true;end"
                    % (machine, cle, PLEX_ANDROID)),
            }
        if methode in ("plex", "plex_client"):
            if not film.get("library"):
                raise HomeAssistantError(
                    "Plex n'a pas dit dans quelle bibliotheque est ce film : "
                    "relis la bibliotheque dans les Reglages.")
            recherche: dict[str, Any] = {"library_name": film["library"], "title": film["title"]}
            if film.get("year"):
                recherche["year"] = film["year"]
            identifiant = json.dumps(recherche, ensure_ascii=False)
            return {
                "entity_id": self.player,
                "media_content_type": "movie",
                # Un Chromecast veut le prefixe ; une entite « Plex (...) » veut le JSON seul.
                "media_content_id": ("plex://" + identifiant) if methode == "plex" else identifiant,
            }
        if methode == "lien_web":
            if not film.get("web_url"):
                raise HomeAssistantError(
                    "Il manque l'identifiant du serveur ou du film pour faire le lien.")
            return {
                "entity_id": self.player,
                "media_content_type": "url",
                "media_content_id": film["web_url"],
            }
        if methode == "lien":
            return {
                "entity_id": self.player,
                "media_content_type": "url",
                "media_content_id": "plex://server://%s/com.plexapp.plugins.library"
                                    "/library/metadata/%s" % (machine, film["rating_key"]),
            }
        return {
            "entity_id": self.player,
            "media_content_type": "app",
            "media_content_id": PLEX_ANDROID,
        }

    def launch(self, film: dict[str, Any], machine: str | None,
               confirm: bool = False, method: str | None = None) -> dict[str, Any]:
        if not self.ready:
            raise HomeAssistantError("Aucune télé choisie dans les Réglages.")
        etat = self._get("/states/" + self.player) or {}
        statut = etat.get("state")
        attributs = etat.get("attributes") or {}
        nom = self.player_name or attributs.get("friendly_name") or self.player

        methode = method if method in METHODES else self.method
        if statut == "unavailable":
            conseil = (" L'application Plex doit être ouverte sur l'appareil : "
                       "l'entité n'existe que tant qu'elle l'est."
                       if methode == "plex_client" else "")
            raise HomeAssistantError(
                "%s ne répond pas à Home Assistant (état « indisponible »).%s" % (nom, conseil))
        # Quelqu'un regarde peut-etre : on ne remplace pas sans qu'on nous le dise.
        # Certains lecteurs disent ce qu'ils lisent ; les autres (Android TV Remote)
        # disent seulement qu'ils sont allumes, ce qui suffit a demander.
        if not confirm:
            if statut in OCCUPE:
                return {"status": "busy", "reason": "lecture", "name": nom, "state": statut,
                        "title": attributs.get("media_title")}
            if statut == "on" and self.confirm_on:
                return {"status": "busy", "reason": "allume", "name": nom, "state": statut,
                        "title": attributs.get("media_title")}

        self._post("/services/media_player/play_media", self.commande(film, machine, methode))
        return {"status": "ok", "name": nom, "method": methode}


def _message(exc: HttpError) -> str:
    """Le message de Home Assistant, quand il en donne un, sans jamais le jeton."""
    texte = ""
    try:
        texte = str(json.loads(exc.body).get("message") or "")
    except (ValueError, AttributeError, TypeError):
        texte = (exc.body or "").strip()
    if not texte:
        texte = str(exc)
    return redact(texte)[:400]

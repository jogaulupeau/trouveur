"""Envoi des torrents vers un serveur Deluge.

Deluge expose une API JSON-RPC sur son interface web : POST /json, session
maintenue par cookie apres auth.login. On s'y connecte avec la bibliotheque
standard, certificat client compris (ssl.SSLContext.load_cert_chain).

Le fichier .torrent est recupere par Trouveur — qui detient la cle du tracker —
puis transmis en base64 via core.add_torrent_file. Deluge n'a donc jamais
besoin de connaitre cette cle, ni d'atteindre le tracker lui-meme.
"""

from __future__ import annotations

import base64
import http.cookiejar
import json
import os
import socket
import ssl
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from . import pkcs12
from .http_client import register_secret

TIMEOUT = 20


class DelugeError(RuntimeError):
    pass


class Deluge:
    def __init__(self, config: dict[str, Any]):
        settings = config.get("deluge", {})
        self.enabled: bool = bool(settings.get("enabled"))
        self.base_url: str = (settings.get("base_url") or "").rstrip("/")
        self.password: str = settings.get("password") or ""
        self.client_cert: str = settings.get("client_cert") or ""
        self.client_key: str = settings.get("client_key") or ""
        self.client_key_password: str = settings.get("client_key_password") or ""
        self.ca_cert: str = settings.get("ca_cert") or ""
        self.verify_tls: bool = bool(settings.get("verify_tls", True))
        self.timeout: int = int(settings.get("timeout") or TIMEOUT)
        # Options appliquees a chaque ajout.
        self.add_paused: bool = bool(settings.get("add_paused", False))
        self.download_location: str = settings.get("download_location") or ""
        self.label: str = settings.get("label") or ""

        register_secret(self.password)
        register_secret(self.client_key_password)

        self._lock = threading.Lock()
        self._opener: urllib.request.OpenerDirector | None = None
        self._id = 0

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.base_url)

    # -- transport ---------------------------------------------------------

    def _context(self) -> ssl.SSLContext | None:
        """Contexte TLS, avec certificat client si l'acces l'exige."""
        if not self.base_url.startswith("https"):
            return None

        context = ssl.create_default_context()
        if self.ca_cert:
            # Autorite privee : sans cela le certificat du serveur est refuse.
            context.load_verify_locations(cafile=self.ca_cert)
        if not self.verify_tls:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE

        if self.client_cert:
            if pkcs12.is_pkcs12(self.client_cert):
                self._charger_p12(context)
            else:
                self._charger_pem(context)
        return context

    def _charger_pem(self, context: ssl.SSLContext) -> None:
        for chemin in (self.client_cert, self.client_key or self.client_cert):
            if not os.path.isfile(chemin):
                raise DelugeError("Fichier de certificat introuvable : %s" % chemin)
        try:
            context.load_cert_chain(
                certfile=self.client_cert,
                keyfile=self.client_key or None,
                password=self.client_key_password or None,
            )
        except ssl.SSLError as exc:
            raise DelugeError(
                "Certificat client refusé : %s. Vérifie le format (PEM attendu) "
                "et la phrase de passe de la clé." % exc
            ) from exc
        except OSError as exc:
            raise DelugeError("Lecture du certificat impossible : %s" % exc) from exc

    def _charger_p12(self, context: ssl.SSLContext) -> None:
        """PKCS#12 : converti en PEM le temps du chargement seulement.

        La cle privee n'est jamais ecrite en clair et les fichiers sont
        supprimes des que le contexte TLS les a lus.
        """
        try:
            with pkcs12.pem_temporaire(
                self.client_cert, self.client_key_password
            ) as (cert, cle):
                context.load_cert_chain(
                    certfile=cert,
                    keyfile=cle,
                    password=self.client_key_password or None,
                )
        except pkcs12.Pkcs12Error as exc:
            raise DelugeError(str(exc)) from exc
        except ssl.SSLError as exc:
            raise DelugeError(
                "Certificat extrait du PKCS#12 mais refusé : %s" % exc
            ) from exc

    def _hote(self) -> tuple[str, int]:
        decoupe = urllib.parse.urlsplit(self.base_url)
        return decoupe.hostname or "", decoupe.port or 443

    def _certificat_du_serveur(self) -> dict[str, str]:
        """Qui a signé le certificat que presente Deluge ?

        « unable to get local issuer certificate » ne dit pas quelle autorite
        manque. On rouvre donc une connexion sans verification — uniquement
        pour lire le certificat et le decrire, jamais pour echanger quoi que
        ce soit.
        """
        hote, port = self._hote()
        if not hote:
            return {}
        brut = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        brut.check_hostname = False
        brut.verify_mode = ssl.CERT_NONE
        try:
            with socket.create_connection((hote, port), timeout=self.timeout) as tcp:
                with brut.wrap_socket(tcp, server_hostname=hote) as tls:
                    der = tls.getpeercert(binary_form=True)
        except (OSError, ssl.SSLError):
            return {}
        if not der:
            return {}

        try:
            resultat = subprocess.run(
                ["openssl", "x509", "-noout", "-subject", "-issuer", "-dates"],
                input=ssl.DER_cert_to_PEM_cert(der),
                capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return {}
        if resultat.returncode != 0:
            return {}

        champs: dict[str, str] = {}
        for ligne in resultat.stdout.splitlines():
            cle, _, valeur = ligne.partition("=")
            champs[cle.strip().lower()] = valeur.strip()
        return champs

    def _explique_verification(self, raison: ssl.SSLError) -> str:
        """Traduit un echec de verification en geste a faire."""
        certificat = self._certificat_du_serveur()
        emetteur = certificat.get("issuer") or ""
        sujet = certificat.get("subject") or ""

        qui = ""
        if emetteur:
            qui = " Le serveur présente un certificat émis par « %s »" % emetteur
            qui += (" pour « %s »." % sujet) if sujet else "."
            if emetteur == sujet:
                qui += " Émetteur et sujet identiques : c'est un certificat auto-signé."

        return (
            "Le certificat du serveur Deluge n'est pas reconnu (%s).%s "
            "Dépose l'autorité qui l'a signé dans « Autorité de certification », "
            "ou décoche « Vérifier le certificat du serveur » si tu l'acceptes "
            "tel quel. Ton certificat client, lui, a bien été lu." % (raison, qui)
        )

    def _build_opener(self) -> urllib.request.OpenerDirector:
        # Un cookie jar par session : Deluge identifie la session par cookie.
        jar = http.cookiejar.CookieJar()
        handlers: list[Any] = [urllib.request.HTTPCookieProcessor(jar)]
        context = self._context()
        if context is not None:
            handlers.append(urllib.request.HTTPSHandler(context=context))
        return urllib.request.build_opener(*handlers)

    def _rpc(self, method: str, params: list[Any]) -> Any:
        if not self.configured:
            raise DelugeError("Deluge n'est pas configuré.")
        if self._opener is None:
            self._opener = self._build_opener()

        self._id += 1
        corps = json.dumps({"method": method, "params": params, "id": self._id}).encode()
        requete = urllib.request.Request(
            self.base_url + "/json",
            data=corps,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with self._opener.open(requete, timeout=self.timeout) as reponse:
                brut = reponse.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise DelugeError(
                    "Accès refusé par le serveur (HTTP %s). Si l'accès est protégé "
                    "par certificat client, vérifie qu'il est bien renseigné." % exc.code
                ) from exc
            raise DelugeError("Deluge a répondu HTTP %s." % exc.code) from exc
        except urllib.error.URLError as exc:
            raison = getattr(exc, "reason", exc)
            if isinstance(raison, ssl.SSLError):
                # Deux echecs tres differents portent le meme nom. Celui-ci
                # concerne le certificat du serveur, pas le notre : le dire
                # evite de chercher du mauvais cote.
                texte_tls = str(raison).lower()
                if "certificate_verify_failed" in texte_tls or "verify failed" in texte_tls:
                    raise DelugeError(self._explique_verification(raison)) from exc
                raise DelugeError(
                    "Échec TLS : %s. Certificat client absent ou refusé." % raison
                ) from exc
            texte = str(raison).lower()
            coupee = (
                isinstance(raison, (ConnectionResetError, ConnectionAbortedError))
                or "10054" in texte
                or "reset" in texte
            )
            if coupee and not self.client_cert:
                raise DelugeError(
                    "Le serveur a coupé la connexion pendant la négociation TLS. "
                    "C'est le comportement d'un accès protégé par certificat "
                    "client : renseigne-le dans les réglages."
                ) from exc
            raise DelugeError("Serveur Deluge injoignable : %s" % raison) from exc
        except TimeoutError as exc:
            raise DelugeError("Délai dépassé en contactant Deluge.") from exc

        try:
            charge = json.loads(brut.decode("utf-8", "replace"))
        except json.JSONDecodeError as exc:
            raise DelugeError(
                "Réponse illisible de Deluge (JSON attendu). L'adresse pointe-t-elle "
                "bien sur l'interface web ?"
            ) from exc

        if charge.get("error"):
            message = (charge["error"] or {}).get("message") or str(charge["error"])
            raise DelugeError("Deluge : %s" % message)
        return charge.get("result")

    # -- session -----------------------------------------------------------

    def _login(self) -> None:
        if not self._rpc("auth.login", [self.password]):
            raise DelugeError(
                "Mot de passe refusé par l'interface web de Deluge."
            )

    def _ensure_daemon(self) -> None:
        """L'interface web doit elle-même être reliée à un démon deluged."""
        if self._rpc("web.connected", []):
            return
        hotes = self._rpc("web.get_hosts", []) or []
        if not hotes:
            raise DelugeError(
                "L'interface web de Deluge n'est reliée à aucun démon."
            )
        self._rpc("web.connect", [hotes[0][0]])
        if not self._rpc("web.connected", []):
            raise DelugeError("Connexion au démon Deluge impossible.")

    def _session(self) -> None:
        """Ouvre une session si besoin. Deluge ne dit pas « non authentifié »
        de façon uniforme : on vérifie plutôt que la session répond."""
        try:
            authentifie = bool(self._rpc("auth.check_session", []))
        except DelugeError:
            authentifie = False
        if not authentifie:
            self._login()
        self._ensure_daemon()

    # -- actions -----------------------------------------------------------

    def _options(self) -> dict[str, Any]:
        options: dict[str, Any] = {"add_paused": self.add_paused}
        if self.download_location:
            options["download_location"] = self.download_location
        return options

    def add_torrent_file(self, filename: str, data: bytes) -> dict[str, Any]:
        """Ajoute un .torrent déjà téléchargé. Renvoie l'identifiant Deluge."""
        with self._lock:
            self._session()
            charge = base64.b64encode(data).decode("ascii")
            torrent_id = self._rpc(
                "core.add_torrent_file", [filename, charge, self._options()]
            )
            if not torrent_id:
                # Deluge renvoie None quand le torrent est deja present.
                return {"added": False, "id": None,
                        "message": "Ce torrent est déjà dans Deluge."}
            if self.label:
                self._apply_label(torrent_id)
            return {"added": True, "id": torrent_id,
                    "message": "Ajouté à Deluge." + (" En pause." if self.add_paused else "")}

    def add_magnet(self, uri: str) -> dict[str, Any]:
        with self._lock:
            self._session()
            torrent_id = self._rpc("core.add_torrent_magnet", [uri, self._options()])
            if not torrent_id:
                return {"added": False, "id": None,
                        "message": "Ce torrent est déjà dans Deluge."}
            if self.label:
                self._apply_label(torrent_id)
            return {"added": True, "id": torrent_id, "message": "Ajouté à Deluge."}

    def _apply_label(self, torrent_id: str) -> None:
        """Le greffon Label est facultatif : son absence ne doit pas faire
        échouer un ajout par ailleurs réussi."""
        try:
            self._rpc("label.set_torrent", [torrent_id, self.label])
        except DelugeError:
            pass

    def test(self) -> dict[str, Any]:
        """Diagnostic pour l'écran de réglages, étape par étape."""
        etapes: list[dict[str, Any]] = []

        def etape(nom: str, action) -> bool:
            try:
                action()
                etapes.append({"etape": nom, "ok": True})
                return True
            except DelugeError as exc:
                etapes.append({"etape": nom, "ok": False, "erreur": str(exc)})
                return False

        if not self.configured:
            return {"ok": False, "steps": [],
                    "message": "Deluge n'est pas configuré."}

        # La lecture du certificat peut echouer a elle seule : c'est le premier
        # point a signaler, et il doit apparaitre comme une etape, pas comme
        # une erreur brute.
        def ouvrir() -> None:
            self._opener = self._build_opener()

        if not etape("Lecture du certificat client", ouvrir):
            return {"ok": False, "steps": etapes}
        if not etape("Connexion TLS et authentification", self._login):
            return {"ok": False, "steps": etapes}
        if not etape("Liaison au démon Deluge", self._ensure_daemon):
            return {"ok": False, "steps": etapes}

        version = None
        try:
            version = self._rpc("daemon.info", [])
        except DelugeError:
            pass
        etapes.append({"etape": "Démon interrogé", "ok": True,
                       "detail": "version %s" % version if version else None})
        return {"ok": True, "steps": etapes, "version": version}

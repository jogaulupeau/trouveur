"""Appels HTTP JSON sur la stdlib (aucune dependance externe)."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

USER_AGENT = "Trouveur/1.0 (+local)"

# Cles d'API connues, masquees dans tout message d'erreur remontant a l'interface.
_SECRETS: set[str] = set()


def register_secret(value: str | None) -> None:
    if value and len(value) >= 8:
        _SECRETS.add(value)


class HttpError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


def build_url(base: str, params: dict[str, Any] | None = None) -> str:
    if not params:
        return base
    clean = {k: v for k, v in params.items() if v is not None and v != ""}
    if not clean:
        return base
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}{urllib.parse.urlencode(clean, doseq=True)}"


def get_bytes(
    url: str,
    headers: dict[str, str] | None = None,
    timeout: int = 15,
    ssl_context: Any = None,
) -> bytes:
    return _fetch(url, headers, timeout, accept="*/*", ssl_context=ssl_context)


def get_json(
    url: str,
    headers: dict[str, str] | None = None,
    timeout: int = 15,
    ssl_context: Any = None,
) -> Any:
    raw = _fetch(url, headers, timeout, accept="application/json", ssl_context=ssl_context)
    text = raw.decode("utf-8", "replace").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise HttpError(
            f"Reponse non-JSON de {_redact(url)} : {text[:200]}"
        ) from exc


def post_json(
    url: str,
    headers: dict[str, str] | None = None,
    timeout: int = 15,
) -> Any:
    raw = _fetch(url, headers, timeout, accept="application/json", method="POST")
    text = raw.decode("utf-8", "replace").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise HttpError(f"Reponse non-JSON de {_redact(url)} : {text[:200]}") from exc


def _fetch(
    url: str,
    headers: dict[str, str] | None,
    timeout: int,
    accept: str,
    ssl_context: Any = None,
    method: str = "GET",
) -> bytes:
    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept": accept,
    }
    if headers:
        request_headers.update(headers)

    data = b"" if method == "POST" else None
    request = urllib.request.Request(
        url, data=data, headers=request_headers, method=method
    )
    try:
        with urllib.request.urlopen(
            request, timeout=timeout, context=ssl_context
        ) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:600]
        except Exception:  # noqa: BLE001 - diagnostic best-effort
            pass
        raise HttpError(
            f"HTTP {exc.code} sur {_redact(url)}", status=exc.code, body=body
        ) from exc
    except urllib.error.URLError as exc:
        raise HttpError(f"Echec reseau sur {_redact(url)} : {exc.reason}") from exc
    except TimeoutError as exc:
        raise HttpError(f"Delai depasse sur {_redact(url)}") from exc

    return raw


def redact(text: str) -> str:
    """Retire toute cle d'API connue d'un texte destine a l'utilisateur."""
    for secret in _SECRETS:
        text = text.replace(secret, "***")
    return text


def _redact(url: str) -> str:
    """Masque les cles d'API avant de faire remonter une URL dans un message."""
    parsed = urllib.parse.urlsplit(url)
    if not parsed.query:
        return redact(f"{parsed.scheme}://{parsed.netloc}{parsed.path}")
    pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    sensitive = ("key", "token", "pass", "secret", "auth", "apikey")
    safe = [
        (k, "***" if any(word in k.lower() for word in sensitive) else v)
        for k, v in pairs
    ]
    rebuilt = urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(safe), "")
    )
    # Filet de securite : un parametre nomme "k" ou "u" porte parfois la cle.
    return redact(rebuilt)

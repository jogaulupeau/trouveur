"""Cache disque minimaliste (JSON) pour limiter les appels reseau."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from typing import Any

from .paths import cache_dir

_lock = threading.Lock()


def _path_for(key: str) -> str:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    return os.path.join(cache_dir(), f"{digest}.json")


def get(key: str, ttl: int) -> Any | None:
    if ttl <= 0:
        return None
    path = _path_for(key)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            entry = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if time.time() - entry.get("stored_at", 0) > ttl:
        return None
    return entry.get("value")


def put(key: str, value: Any) -> None:
    os.makedirs(cache_dir(), exist_ok=True)
    path = _path_for(key)
    payload = {"stored_at": time.time(), "key": key, "value": value}
    tmp = f"{path}.{os.getpid()}.tmp"
    with _lock:
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.replace(tmp, path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def clear() -> int:
    if not os.path.isdir(cache_dir()):
        return 0
    removed = 0
    for name in os.listdir(cache_dir()):
        if name.endswith(".json"):
            try:
                os.unlink(os.path.join(cache_dir(), name))
                removed += 1
            except OSError:
                pass
    return removed

#!/usr/bin/env python3
"""
store.py — the one place that knows *where* state lives.

Locally you want zero setup: state is JSON on disk next to the code, and
`uvicorn server.app:app` works with no database at all.

On a serverless host (Vercel) that doesn't work — the filesystem is read-only,
every request may land on a different instance, and nothing survives between
them. So if Upstash Redis credentials are present, the same calls go there
instead. Nothing else in the app has to know which one is in play.

    UPSTASH_REDIS_REST_URL=...      # -> Redis backend (serverless-safe, HTTP)
    UPSTASH_REDIS_REST_TOKEN=...
    (neither set)                   # -> local JSON files under data/

Keys used by the app: mission, targets, campaign, log, results.
"""
import json
import os
import threading
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

_URL = os.getenv("UPSTASH_REDIS_REST_URL", "").rstrip("/")
_TOK = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")
REMOTE = bool(_URL and _TOK)
PREFIX = os.getenv("STORE_PREFIX", "va")

_lock = threading.Lock()
_mem = {}          # process-local cache; also the fallback if disk is read-only


def backend() -> str:
    return "upstash-redis" if REMOTE else "local-files"


# ---------------- upstash (REST, one HTTP call per op) ----------------
def _cmd(*args):
    r = requests.post(f"{_URL}/", headers={"Authorization": f"Bearer {_TOK}"},
                      json=list(args), timeout=10)
    r.raise_for_status()
    return r.json().get("result")


# ---------------- local files ----------------
def _path(key) -> Path:
    return DATA / f"state_{key}.json"


def _read_local(key, default):
    if key in _mem:
        return _mem[key]
    p = _path(key)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default


def _write_local(key, value):
    _mem[key] = value
    try:
        DATA.mkdir(exist_ok=True)
        _path(key).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass       # read-only fs — the in-memory copy still serves this request


# ---------------- public api ----------------
def get(key, default=None):
    if REMOTE:
        try:
            raw = _cmd("GET", f"{PREFIX}:{key}")
            return json.loads(raw) if raw else default
        except Exception:
            return default
    with _lock:
        return _read_local(key, default)


def set(key, value):                      # noqa: A001 - deliberate, mirrors dict/redis
    if REMOTE:
        try:
            _cmd("SET", f"{PREFIX}:{key}", json.dumps(value, ensure_ascii=False))
            return
        except Exception:
            return
    with _lock:
        _write_local(key, value)


def append(key, item, cap=None):
    """Append to a stored list and return the new list."""
    lst = get(key, []) or []
    lst.append(item)
    if cap:
        lst = lst[-cap:]
    set(key, lst)
    return lst


def merge(key, patch: dict):
    """Shallow-merge into a stored dict and return it."""
    cur = get(key, {}) or {}
    cur.update(patch)
    set(key, cur)
    return cur

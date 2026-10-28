"""Keys and settings saved from the Setup page, layered under the environment.

Where a value comes from, first match wins:
1. the process environment (in Docker: .env and data.keys through docker-compose's env_file);
2. data.keys and .env read from the repo root (outside Docker, the way config.py's loader reads them);
3. a value saved on the Setup page (table app_settings);
4. the default in code.

A saved value is applied to `settings` in place, so code that reads settings.<field> when it runs sees it without a
restart; Deep End code reads it through deepend/cfg.py. Every process re-reads the table when it changes: the dashboard
right after it saves, ingest and the host worker through refresh() every 30 seconds. on_change() lets a long-running
task (the alerts bot, the Telegram listener) restart when one of its keys changes.

Never log or return a value from here; the Setup API shows only whether a key is set and its last four characters.
"""

import functools
import inspect
import logging
import os
from collections.abc import Callable
from typing import Any

import asyncpg
from dotenv import dotenv_values
from pydantic import TypeAdapter, ValidationError

from albatross import db
from albatross.config import ROOT, Settings, settings

log = logging.getLogger(__name__)

OVERLAY: dict[str, str] = {}            # NAME -> value saved on the Setup page and not set in the environment
_DEFAULTS: dict[str, Any] = {}          # settings field -> its value before any saved value replaced it
_listeners: list[Callable] = []
_sig: tuple | None = None


@functools.cache
def _env_files() -> dict[str, str]:
    out: dict[str, str] = {}
    for path in (ROOT / "data.keys", ROOT / ".env"):
        if path.exists():
            out.update({k: v.strip() for k, v in dotenv_values(path).items() if v and v.strip()})
    return out


def from_env(name: str) -> bool:
    """True when the environment sets this name, so a value saved here is ignored."""
    return bool((os.environ.get(name) or "").strip() or _env_files().get(name))


def value(name: str) -> str | None:
    """The value in force: the environment's, else the saved one."""
    return (os.environ.get(name) or "").strip() or _env_files().get(name) or OVERLAY.get(name) or None


def source(name: str) -> str:
    return "env" if from_env(name) else "saved" if name in OVERLAY else "missing"


def field(name: str) -> str | None:
    f = name.lower()
    return f if f in Settings.model_fields else None


def coerce(name: str, raw: str) -> Any:
    """A saved string as the settings field's type ("true" -> True); ValidationError when it isn't one."""
    f = field(name)
    return raw if f is None else TypeAdapter(Settings.model_fields[f].annotation).validate_python(raw)


def _apply(rows: dict[str, str]) -> set[str]:
    wanted = {k: v for k, v in rows.items() if not from_env(k)}
    changed: set[str] = set()
    for name in set(OVERLAY) | set(wanted):
        new = wanted.get(name)
        if OVERLAY.get(name) == new:
            continue
        if (f := field(name)) is not None:
            _DEFAULTS.setdefault(f, getattr(settings, f))
            try:
                setattr(settings, f, coerce(name, new) if new is not None else _DEFAULTS[f])
            except ValidationError:
                log.warning("setup: the saved %s isn't a valid value; ignored", name)
                wanted.pop(name, None)
                if name not in OVERLAY:
                    continue
                setattr(settings, f, _DEFAULTS[f])
        changed.add(name)
    OVERLAY.clear()
    OVERLAY.update(wanted)
    return changed


async def _rows() -> dict[str, str]:
    pool = await db.pool()
    try:
        return {r["name"]: r["value"] for r in await pool.fetch("SELECT name, value FROM app_settings")}
    except asyncpg.UndefinedTableError:         # the dashboard can start before ingest has run the migrations
        return {}


async def load() -> set[str]:
    """Re-read every saved value and apply it -> the names whose value in force changed."""
    changed = _apply(await _rows())
    for fn in list(_listeners):
        try:
            out = fn(changed)
            if inspect.isawaitable(out):
                await out
        except Exception as e:  # noqa: BLE001 - one listener must not stop the others
            log.warning("setup: change listener %s failed: %s", getattr(fn, "__name__", fn), type(e).__name__)
    return changed


async def refresh() -> int:
    """Every 30 s in ingest and the host worker: reload only when the table changed -> names changed."""
    global _sig
    pool = await db.pool()
    try:
        sig = tuple(await pool.fetchrow("SELECT count(*), max(updated_at) FROM app_settings"))
    except asyncpg.UndefinedTableError:
        return 0
    if sig == _sig:
        return 0
    _sig = sig
    return len(await load())


async def save(name: str, raw: str) -> set[str]:
    pool = await db.pool()
    await pool.execute(
        """INSERT INTO app_settings (name, value) VALUES ($1, $2)
           ON CONFLICT (name) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""", name, raw)
    return await load()


async def delete(name: str) -> set[str]:
    pool = await db.pool()
    await pool.execute("DELETE FROM app_settings WHERE name = $1", name)
    return await load()


class NotConfigured(RuntimeError):
    """A job can't run because the key or account it needs isn't set up. ingest/health.tracked() records the run as
    skipped, never failed, so a fresh install with no keys raises no alerts."""


def on_change(fn: Callable[[set[str]], Any]) -> Callable[[set[str]], Any]:
    """Call fn(changed_names) after every load(); fn may be async."""
    _listeners.append(fn)
    return fn

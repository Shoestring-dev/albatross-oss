"""Deep End settings from the environment, else data.keys, else the Setup page (albatross/keys.py).

The ingest container gets data.keys as its environment (docker-compose env_file), but the host worker (systemd,
where the Grok CLI runs) does not, so a value set in data.keys is also read from the file itself here, the way the
pydantic settings loader reads it for config.py's fields. The process environment wins.
"""

import functools
import os

from dotenv import dotenv_values

from albatross.config import ROOT


@functools.cache
def _keys_file() -> dict:
    path = ROOT / "data.keys"
    return {k: v for k, v in dotenv_values(path).items() if v} if path.exists() else {}


def _get(name: str) -> str | None:
    from albatross import keys                   # late: keys imports db, which this module's importers may not need

    v = (os.environ.get(name) or "").strip()
    return v or (_keys_file().get(name) or "").strip() or keys.OVERLAY.get(name) or None


def env_str(name: str, default: str | None = None) -> str | None:
    return _get(name) or default


def env_float(name: str, default: float) -> float:
    try:
        return float(_get(name) or default)
    except ValueError:
        return default


def env_int(name: str, default: int) -> int:
    try:
        return int(float(_get(name) or default))
    except ValueError:
        return default

"""One place every event collector writes through, so one real-world event is one row however many feeds report it.

A collector builds `Event`s and calls `upsert(events, collector=...)`. Rows are keyed by make_key(kind, subject,
day, venue); on a repeat (in the same batch or a later run) the sources are merged (by URL), importance takes the max,
status only moves forward (rumored -> scheduled -> happened / cancelled), the time keeps whichever report is more
precise (or the same source's newer word at equal precision), and the newest probability wins, bringing its summary.
Nothing here calls a vendor or an LLM.
"""

import re
import unicodedata
from datetime import UTC, datetime, time

import orjson
from pydantic import BaseModel, Field, field_validator

from albatross import db

KINDS = {"tge", "listing", "delisting", "unlock", "mainnet", "upgrade", "airdrop", "governance", "macro",
         "conference", "other"}
STATUS_RANK = {"rumored": 0, "scheduled": 1, "happened": 2, "cancelled": 2}
PRECISION_RANK = {"quarter": 0, "month": 1, "week": 2, "day": 3, "exact": 4}


class Source(BaseModel):
    name: str                                   # "OKX announcements", "CoinDesk", "Polymarket", "Oracle (web)"
    url: str | None = None
    excerpt: str | None = Field(default=None, description="the sentence that states the event, <= 300 chars")


class Event(BaseModel):
    kind: str
    title: str
    subject: str
    starts_at: datetime
    precision: str = "day"
    venue: str | None = None
    ends_at: datetime | None = None
    assets: list[str] = Field(default_factory=list)
    chains: list[str] = Field(default_factory=list)
    importance: int = Field(default=2, ge=1, le=5)
    status: str = "scheduled"
    probability: float | None = Field(default=None, ge=0, le=1)
    summary: str | None = None
    sources: list[Source] = Field(default_factory=list)

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        v = v.lower().strip()
        return v if v in KINDS else "other"

    @field_validator("starts_at", "ends_at")
    @classmethod
    def _utc(cls, v):
        return v if v is None or v.tzinfo else v.replace(tzinfo=UTC)


def _slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:60] or "x"


def make_key(kind: str, subject: str, day: datetime, venue: str | None = None) -> str:
    """kind:subject[:venue]:YYYY-MM-DD, so 'OKX lists VVV on the 30th' from two feeds is one row. Month/quarter
    precision events key on the first day of their period (collectors pass that as starts_at)."""
    return ":".join(x for x in (kind, _slug(subject), _slug(venue) if venue else None, day.strftime("%Y-%m-%d")) if x)


def day_start(d) -> datetime:
    return datetime.combine(d, time(0), tzinfo=UTC)


def merge(old: dict, new: Event, collector: str, now: datetime) -> dict:
    """The row after `new` is folded into `old` (a dict of the stored columns)."""
    out = dict(old)
    seen = {s.get("url") or s.get("name") for s in old["sources"]}
    out["sources"] = old["sources"] + [
        {**s.model_dump(), "collector": collector, "seen_at": now.isoformat()}
        for s in new.sources if (s.url or s.name) not in seen]
    out["importance"] = max(old["importance"], new.importance)
    if STATUS_RANK[new.status] >= STATUS_RANK.get(old["status"], 0):
        out["status"] = new.status
    # A source read again (same URL) is that source correcting itself, e.g. an exchange postponing a listing within
    # the day: its time wins at equal precision too. Another source has to be more precise to move the time.
    reread = any(s.url and s.url in {o.get("url") for o in old["sources"]} for s in new.sources)
    p_new, p_old = PRECISION_RANK.get(new.precision, 0), PRECISION_RANK.get(old["precision"], 0)
    if p_new > p_old or (reread and p_new == p_old):
        out["starts_at"], out["precision"] = new.starts_at, new.precision
    out["summary"] = old["summary"] or new.summary
    if new.probability is not None:
        # A priced report's summary quotes its odds, so it moves with them.
        out["probability"], out["summary"] = new.probability, new.summary or out["summary"]
    out["assets"] = sorted(set(old["assets"]) | {a.upper() for a in new.assets})
    out["chains"] = sorted(set(old["chains"]) | set(new.chains))
    out["ends_at"] = old["ends_at"] or new.ends_at
    return out


COLS = ["key", "kind", "title", "subject", "venue", "starts_at", "precision", "ends_at", "assets", "chains",
        "importance", "status", "probability", "summary", "sources", "updated_at"]


async def upsert(events: list[Event], collector: str) -> int:
    """Write or merge events; returns how many rows were new."""
    if not events:
        return 0
    now = datetime.now(UTC)
    keyed = [(make_key(e.kind, e.subject, e.starts_at, e.venue), e) for e in events]
    pool = await db.pool()
    stored = {r["key"]: dict(r) | {"sources": orjson.loads(r["sources"])} for r in await pool.fetch(
        "SELECT * FROM events WHERE key = ANY($1::text[])", list({k for k, _ in keyed}))}
    # Repeats inside one batch merge like repeats across runs (two notices of one listing keep both sources).
    rows: dict[str, dict] = {}
    for key, e in keyed:
        if key in rows or key in stored:
            rows[key] = merge(rows.get(key) or stored[key], e, collector, now)
        else:
            rows[key] = {"key": key, **e.model_dump(exclude={"sources"}),
                         "assets": sorted({a.upper() for a in e.assets}),
                         "sources": [{**s.model_dump(), "collector": collector, "seen_at": now.isoformat()}
                                     for s in e.sources]}
    for r in rows.values():
        r["updated_at"] = now
    await db.upsert("events", COLS, [tuple(orjson.dumps(r[c]).decode() if c == "sources" else r[c] for c in COLS)
                                     for r in rows.values()], ["key"])
    return len(rows.keys() - stored.keys())

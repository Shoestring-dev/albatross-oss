"""albatross/events/store.py: one real-world event is one row."""
from datetime import UTC, datetime

from albatross.events.store import Event, make_key, merge

NOW = datetime(2026, 9, 30, 12, tzinfo=UTC)


def _ev(**kw):
    base = dict(kind="listing", title="OKX lists VVV", subject="VVV", venue="OKX",
                starts_at=datetime(2026, 9, 30, tzinfo=UTC), sources=[{"name": "OKX", "url": "https://okx.com/a"}])
    return Event(**(base | kw))


def test_key_ignores_time_of_day_and_case():
    a = make_key("listing", "VVV", datetime(2026, 9, 30, 8, tzinfo=UTC), "OKX")
    b = make_key("listing", "vvv", datetime(2026, 9, 30, 23, tzinfo=UTC), "okx")
    assert a == b == "listing:vvv:okx:2026-09-30"
    assert make_key("tge", "Ink Protocol!", datetime(2026, 10, 1, tzinfo=UTC)) == "tge:ink-protocol:2026-10-01"


def test_unknown_kind_becomes_other():
    assert _ev(kind="Rumour").kind == "other"


def test_merge_keeps_sources_raises_status_and_precision():
    old = {"sources": [{"name": "OKX", "url": "https://okx.com/a"}], "importance": 3, "status": "rumored",
           "precision": "day", "starts_at": datetime(2026, 9, 30, tzinfo=UTC), "probability": None,
           "assets": ["VVV"], "chains": [], "summary": None, "ends_at": None}
    new = _ev(status="scheduled", precision="exact", starts_at=datetime(2026, 9, 30, 8, tzinfo=UTC), importance=2,
              probability=0.8, sources=[{"name": "CoinDesk", "url": "https://coindesk.com/x"}, {"name": "OKX", "url": "https://okx.com/a"}])
    m = merge(old, new, "news_events", NOW)
    assert [s["url"] for s in m["sources"]] == ["https://okx.com/a", "https://coindesk.com/x"]
    assert m["importance"] == 3 and m["status"] == "scheduled" and m["precision"] == "exact"
    assert m["starts_at"].hour == 8 and m["probability"] == 0.8
    # status never moves backwards
    assert merge(m | {"status": "happened"}, _ev(status="rumored"), "x", NOW)["status"] == "happened"


def _row(**kw):
    base = {"sources": [{"name": "Upbit notices", "url": "https://upbit.com/n/1"}], "importance": 4,
            "status": "scheduled", "precision": "exact", "starts_at": datetime(2026, 9, 10, 6, tzinfo=UTC),
            "probability": None, "assets": [], "chains": [], "summary": "opens 15:00 KST", "ends_at": None}
    return base | kw


def test_the_same_source_read_again_moves_the_time_at_equal_precision():
    later = datetime(2026, 9, 10, 12, tzinfo=UTC)            # Upbit postponed the listing inside the day
    reread = _ev(precision="exact", starts_at=later, sources=[{"name": "Upbit", "url": "https://upbit.com/n/1"}])
    m = merge(_row(), reread, "events_listings", NOW)
    assert m["starts_at"] == later
    # Another source at the same precision doesn't; a less precise re-read doesn't either.
    other = _ev(precision="exact", starts_at=later, sources=[{"name": "CoinDesk", "url": "https://coindesk.com/x"}])
    assert merge(_row(), other, "events_news", NOW)["starts_at"].hour == 6
    vague = _ev(precision="day", starts_at=datetime(2026, 9, 10, tzinfo=UTC),
                sources=[{"name": "Upbit", "url": "https://upbit.com/n/1"}])
    assert merge(_row(), vague, "events_listings", NOW)["starts_at"].hour == 6


def test_a_priced_report_brings_its_summary():
    old = _row(probability=0.4, summary="Polymarket odds: Dec 31 40%")
    m = merge(old, _ev(kind="tge", probability=0.55, summary="Polymarket odds: Dec 31 55%"), "events_polymarket", NOW)
    assert (m["probability"], m["summary"]) == (0.55, "Polymarket odds: Dec 31 55%")
    # An unpriced report keeps the first summary.
    assert merge(old, _ev(summary="news says so"), "events_news", NOW)["summary"] == "Polymarket odds: Dec 31 40%"


def test_repeats_in_one_batch_merge_their_sources(monkeypatch):
    import asyncio

    from albatross.events import store

    class Pool:
        async def fetch(self, *_):
            return []

    written = {}

    async def pool():
        return Pool()

    async def upsert(table, cols, rows, conflict):
        written["rows"] = [dict(zip(cols, r)) for r in rows]

    monkeypatch.setattr(store.db, "pool", pool)
    monkeypatch.setattr(store.db, "upsert", upsert)
    a = _ev(sources=[{"name": "OKX", "url": "https://okx.com/usdt"}])
    b = _ev(sources=[{"name": "OKX", "url": "https://okx.com/usd"}], importance=3)
    assert asyncio.run(store.upsert([a, b], "events_listings")) == 1
    [row] = written["rows"]
    assert row["importance"] == 3
    assert "https://okx.com/usd\"" in row["sources"] and "https://okx.com/usdt" in row["sources"]

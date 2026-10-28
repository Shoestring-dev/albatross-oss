"""The call-feed push endpoint (albatross/api/ingest.py)."""


def test_paper_positions_map_to_the_plain_token():
    # A feed may key paper positions "<address>#paper"; they must not become a second, unpriceable token.
    from albatross.api import ingest
    assert ingest._addr("robinhood", "0xC6080ea62997b33c8e46360850cb4473913b61f0#paper") == "0xc6080ea62997b33c8e46360850cb4473913b61f0"
    assert ingest._addr("solana", "resideK2Ejv9apu1op2VBv89poumxCDCs8nzBxQ5q1y#paper") == "resideK2Ejv9apu1op2VBv89poumxCDCs8nzBxQ5q1y"
    assert ingest._paper("abc#paper") and not ingest._paper("abc") and not ingest._paper("abc#other")


def test_one_odd_row_never_rejects_a_batch(monkeypatch):
    # 2026-09-30: one bot trade's ~200-character "INUINU..." symbol got every batch rejected (422) for 90 minutes,
    # and the exporter re-sends a rejected batch forever. Text is cut to length; an unusable row is skipped.
    from contextlib import asynccontextmanager

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from albatross.api import ingest
    from albatross.config import settings
    written = {}

    class Conn:
        async def execute(self, *a):
            pass

        async def executemany(self, sql, rows):
            written[sql.split("INTO", 1)[1].split()[0]] = list(rows)

        async def fetchval(self, *a):
            return 0

        @asynccontextmanager
        async def transaction(self):
            yield

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield Conn()

    async def pool():
        return Pool()

    monkeypatch.setattr(ingest.db, "pool", pool)
    monkeypatch.setattr(settings, "call_feed_token", "t")
    app = FastAPI()
    app.include_router(ingest.router)
    mint = "So11111111111111111111111111111111111111112"
    body = {"source": "feed",
            "calls": [{"src_id": 1, "chain": "solana", "address": mint, "group_name": "g" * 500, "called_at": 1.7e9},
                      {"src_id": 2, "chain": "solana", "address": "short", "called_at": 1.7e9}],
            "trades": [{"src_id": 3, "trader": "x", "chain": "solana", "address": mint, "symbol": "INU" * 70,
                        "entry_at": 1.7e9, "status": "open", "txs": ["t"] * 80}]}
    r = TestClient(app).post("/api/ingest/calls", json=body, headers={"Authorization": "Bearer t"})
    assert r.status_code == 200
    j = r.json()
    assert j["calls"] == 1 and j["trades"] == 1 and [s["src_id"] for s in j["skipped"]] == [2]
    assert written["micro_trades"][0][4] == ("INU" * 70)[:64]         # the symbol, cut to 64
    assert len(written["micro_trades"][0][19]) == 50                  # txs, cut to 50
    assert written["micro_calls"][0][4] == "g" * 200                  # group name, cut to 200


def _feed(monkeypatch):
    """The endpoint with an in-memory database: -> (client, {table: rows written})."""
    from contextlib import asynccontextmanager

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from albatross.api import ingest
    from albatross.config import settings
    written: dict[str, list] = {}

    class Conn:
        async def execute(self, *a):
            pass

        async def executemany(self, sql, rows):
            written.setdefault(sql.split("INTO", 1)[1].split()[0], []).extend(rows)

        async def fetchval(self, *a):
            return 0

        @asynccontextmanager
        async def transaction(self):
            yield

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield Conn()

    async def pool():
        return Pool()

    monkeypatch.setattr(ingest.db, "pool", pool)
    monkeypatch.setattr(settings, "call_feed_token", "t")
    app = FastAPI()
    app.include_router(ingest.router)
    return TestClient(app), written


MINT = "So11111111111111111111111111111111111111112"


def test_two_programs_counting_from_one_never_collide(monkeypatch):
    from albatross.api import ingest
    c, written = _feed(monkeypatch)
    for source in ("bot-a", "bot-b", "feed"):
        r = c.post("/api/ingest/calls", headers={"Authorization": "Bearer t"},
                   json={"source": source, "calls": [{"src_id": 1, "chain": "solana", "address": MINT, "called_at": 1.7e9}]})
        assert r.json()["calls"] == 1
    ids = [row[0] for row in written["micro_calls"]]
    assert len(set(ids)) == 3 and 1 in ids                     # the default feed keeps its own id
    assert all(i < 2 ** 53 for i in ids)                        # exact in JavaScript
    assert ingest.row_id("bot-a", 1) == ingest.row_id("bot-a", 1) != ingest.row_id("bot-a", 2)
    assert ingest.row_id("telegram", 2 ** 52 + 5) == 2 ** 52 + 5   # the listener's ids are its own


def test_bad_times_ids_and_chains_are_skipped_with_a_reason(monkeypatch):
    c, written = _feed(monkeypatch)
    call = {"chain": "solana", "address": MINT}
    r = c.post("/api/ingest/calls", headers={"Authorization": "Bearer t"}, json={"calls": [
        {**call, "src_id": 1, "called_at": 1.7e12},             # milliseconds
        {**call, "src_id": 2, "called_at": 0},
        {**call, "src_id": 3, "called_at": 1.7e9, "chain": "tron"},
        {**call, "src_id": 2 ** 52, "called_at": 1.7e9},
        {**call, "src_id": 5, "called_at": 1.7e9},
    ], "trades": [{"src_id": 6, "trader": "t", "chain": "solana", "address": MINT, "entry_at": 1.7e12}]})
    j = r.json()
    assert r.status_code == 200 and j["calls"] == 1 and j["trades"] == 0
    why = {s["src_id"]: s["error"] for s in j["skipped"]}
    assert "milliseconds" in why[1] and "2015" in why[2] and "tron" in why[3] and "2^51" in why[2 ** 52]
    assert "milliseconds" in why[6]

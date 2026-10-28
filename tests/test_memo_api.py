"""The CEO memo on Home (albatross/api/memo.py): old memos read back from their markdown, which actions get which
buttons, and that a change needs the action header."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from albatross.api import memo

MD = """No paper book is mature enough to establish skill.

## Findings
- **Paper results.** Every book is too early.

## Recommended
- Investigate: The opposite of the quant model (1h). Audit entry timestamps across every graded source because entering before a call exists undermines executable performance claims.
- Pause: Quant model. Its recorded harmful performance warrants reduced-budget sampling while the grading audit checks that record.
- Budget: HTF analyst. Reduce its subscription allocation because it remains active without demonstrated skill.
- Investigate: Deep End fit research. Review the usefulness of its research outputs against its $3.01 API spend without demanding a market-call grade.
- Investigate: Deep End X Scout. Diagnose its 2 failed calls before allocating more scout capacity.

_Recommendations only: nothing changes until the owner acts on them._"""


def test_an_old_memo_is_read_back_from_its_markdown_in_order():
    acts = memo.parse_markdown(MD)
    assert [a["kind"] for a in acts] == ["investigate", "pause", "budget", "investigate", "investigate"]
    assert acts[0]["name"] == "The opposite of the quant model (1h)"
    assert acts[3]["why"].startswith("Review the usefulness") and "$3.01" in acts[3]["why"]
    assert acts[4]["name"] == "Deep End X Scout"


def test_a_memo_without_actions_has_none():
    assert memo.parse_markdown("Nothing to do.\n\n_Recommendations only._") == []


def test_only_agents_on_the_ladder_get_buttons():
    assert memo.on_ladder("quant") and memo.on_ladder("htf_analyst")        # on the ladder; buttons: see rules()
    assert not memo.on_ladder("ensemble") and not memo.on_ladder("factor_rank")       # exempt from culling
    assert not memo.on_ladder("book:fade_quant") and not memo.on_ladder(None)
    assert memo.ACTS["pause"] == ("pause",) and memo.ACTS["budget"] == ("budget",) and "investigate" not in memo.ACTS


def test_a_change_needs_the_action_header():
    app = FastAPI()
    app.include_router(memo.router)
    c = TestClient(app)
    assert c.post("/api/memo/1/act", json={"idx": 0, "action": "pause"}).status_code == 403
    assert c.post("/api/memo/1/undo", json={"idx": 0}).status_code == 403
    assert c.post("/api/memo/1/act", json={"idx": 0, "action": "delete"},
                  headers={"X-Albatross-Action": "1"}).status_code == 422


# ─── act and undo, against a fake of the tables they touch ─────────────────────────────────────────────────────────
import asyncio  # noqa: E402
from contextlib import asynccontextmanager  # noqa: E402
from datetime import UTC, datetime, timedelta  # noqa: E402

import orjson  # noqa: E402
import pytest  # noqa: E402

from albatross import controls, db  # noqa: E402

H = {"X-Albatross-Action": "1"}
SINCE = datetime(2026, 9, 30, 1, 0, tzinfo=UTC)
ACTIONS = [{"kind": "pause", "target": "htf_analyst", "name": "HTF analyst", "why": "w", "plain": ""},
           {"kind": "resume", "target": "htf_analyst", "name": "HTF analyst", "why": "w", "plain": ""},
           {"kind": "budget", "target": "strategist", "name": "Strategist", "why": "w", "plain": ""},
           {"kind": "cull", "target": "htf_analyst", "name": "HTF analyst", "why": "w", "plain": ""},
           {"kind": "budget", "target": "quant", "name": "Quant model", "why": "w", "plain": ""},
           {"kind": "pause", "target": "quant", "name": "Quant model", "why": "w", "plain": ""}]


class Tables:
    """agent_status, agent_controls, memo_acts, reports and agent_reviews, answering the SQL that memo.py,
    cull.set_rung and controls send, with the same upsert rules."""
    def __init__(self, status=None, scales=None):
        self.status = {k: {"rung": r, "streak": n, "since": SINCE, "reason": "review said so", "set_by": "review"}
                       for k, (r, n) in (status or {}).items()}
        self.controls = {k: {"budget_scale": v, "note": "the owner's", "paused": False} for k, v in (scales or {}).items()}
        self.acts, self.reviews = {}, []
        self.reports = {59: {"id": 59, "title": "Oct 5, 12:55 UTC", "body_md": "", "created_at": SINCE,
                             "meta": orjson.dumps({"actions": ACTIONS}).decode()},
                        60: {"id": 60, "title": "Oct 6, 00:55 UTC", "body_md": "", "created_at": SINCE + timedelta(hours=12),
                             "meta": orjson.dumps({"actions": ACTIONS}).decode()}}

    async def fetchrow(self, sql, *a):
        if "FROM reports WHERE kind = 'ceo'" in sql:
            return self.reports.get(a[0]) if a else self.reports[max(self.reports)]
        if "FROM paper_reports" in sql:
            return None
        if "SELECT * FROM memo_acts" in sql:
            return self.acts.get((a[0], a[1]))
        if "FROM agent_status WHERE agent" in sql:
            s = self.status.get(a[0])
            return {"rung": s["rung"], "streak": s["streak"], "since": s["since"], "reason": s["reason"], "set_by": s["set_by"]} if s else None
        if "FROM agent_controls WHERE key" in sql:
            c = self.controls.get(a[0])
            return {"budget_scale": c["budget_scale"], "note": c["note"]} if c else None
        raise AssertionError(sql)

    async def fetch(self, sql, *a):
        if "SELECT agent, rung FROM agent_status" in sql:
            return [{"agent": k, "rung": v["rung"]} for k, v in self.status.items()]
        if "SELECT key, paused, budget_scale FROM agent_controls" in sql:
            return [{"key": k, **v} for k, v in self.controls.items()]
        if "FROM memo_acts WHERE report_id = $1" in sql:
            return [r for (rid, _), r in self.acts.items() if rid == a[0]]
        if "FROM memo_acts a JOIN reports r" in sql:
            return [{**r, "title": self.reports[r["report_id"]]["title"]} for (rid, _), r in self.acts.items()
                    if rid != a[0] and r["undone_at"] is None and r["acted_at"] > datetime.now(UTC) - a[1]]
        raise AssertionError(sql)

    async def fetchval(self, sql, *a):
        if "SELECT rung FROM agent_status WHERE agent" in sql:
            return (self.status.get(a[0]) or {}).get("rung")
        raise AssertionError(sql)

    async def execute(self, sql, *a):
        if "INSERT INTO agent_status" in sql and "set_by = 'owner'" in sql:          # cull.set_rung
            agent, rung, at, reason = a
            old = self.status.get(agent)
            streak = 0 if old is None or rung == "active" else old["streak"]
            self.status[agent] = {"rung": rung, "streak": streak, "since": at, "reason": reason, "set_by": "owner"}
        elif "INSERT INTO agent_status" in sql:                                    # memo's restore
            self.status[a[0]] = {"rung": a[1], "streak": a[2], "since": a[3], "reason": a[4], "set_by": a[5]}
        elif "DELETE FROM agent_status" in sql:
            self.status.pop(a[0], None)
        elif "INSERT INTO agent_reviews" in sql:
            self.reviews.append(a)
        elif "INSERT INTO memo_acts" in sql:
            rid, idx, target, action, after, before, undo_for = a
            old = self.acts.get((rid, idx))
            if old and old["undone_at"] is None and old["acted_at"] > datetime.now(UTC) - undo_for:
                before = old["before"]
            self.acts[(rid, idx)] = {"report_id": rid, "idx": idx, "target": target, "action": action, "after": after,
                                     "before": before, "acted_at": datetime.now(UTC), "undone_at": None}
        elif "UPDATE memo_acts SET undone_at" in sql:
            self.acts[(a[0], a[1])]["undone_at"] = datetime.now(UTC)
        elif "INSERT INTO agent_controls" in sql:                                  # memo._set_scale
            old = self.controls.get(a[0], {"paused": False})
            self.controls[a[0]] = {"budget_scale": a[1], "note": a[2], "paused": old["paused"]}
        elif "UPDATE agent_controls SET budget_scale" in sql:
            self.controls[a[0]].update(budget_scale=a[1], note=a[2])
        else:
            raise AssertionError(sql)

    @asynccontextmanager
    async def _same(self):
        yield self

    def acquire(self):
        return self._same()

    def transaction(self):
        return self._same()


@pytest.fixture
def tables(monkeypatch):
    def make(**kw):
        t = Tables(**kw)

        async def pool():
            return t
        monkeypatch.setattr(db, "pool", pool)
        app = FastAPI()
        app.include_router(memo.router)
        return t, TestClient(app)
    controls.forget()
    yield make
    controls.forget()


def test_acting_twice_keeps_the_first_state_to_go_back_to(tables):
    t, c = tables(scales={"strategist": 1.0})
    assert c.post("/api/memo/60/act", json={"idx": 2, "action": "budget", "scale": 0.5}, headers=H).status_code == 200
    assert c.post("/api/memo/60/act", json={"idx": 2, "action": "budget", "scale": 0.25}, headers=H).status_code == 200
    assert t.controls["strategist"]["budget_scale"] == 0.25
    assert c.post("/api/memo/60/undo", json={"idx": 2}, headers=H).status_code == 200
    assert t.controls["strategist"] == {"budget_scale": 1.0, "note": "the owner's", "paused": False}   # note and all


def test_a_pause_never_starts_a_retired_agent_and_an_act_that_changes_nothing_is_refused(tables):
    t, c = tables(status={"htf_analyst": ("retired", 4)})
    r = c.post("/api/memo/60/act", json={"idx": 0, "action": "pause"}, headers=H)
    assert r.status_code == 409 and "retired" in r.json()["detail"]
    assert c.post("/api/memo/60/act", json={"idx": 3, "action": "retire"}, headers=H).status_code == 409
    assert t.status["htf_analyst"]["rung"] == "retired" and not t.acts
    t, c = tables(status={"htf_analyst": ("paused", 3)})
    assert c.post("/api/memo/60/act", json={"idx": 0, "action": "pause"}, headers=H).status_code == 409
    t, c = tables()
    assert c.post("/api/memo/60/act", json={"idx": 1, "action": "reinstate"}, headers=H).status_code == 409


def test_undo_puts_back_the_whole_ladder_row(tables):
    t, c = tables(status={"htf_analyst": ("probation", 3)})
    was = dict(t.status["htf_analyst"])
    assert c.post("/api/memo/60/act", json={"idx": 1, "action": "reinstate"}, headers=H).status_code == 200
    assert t.status["htf_analyst"]["rung"] == "active" and t.status["htf_analyst"]["streak"] == 0
    assert c.post("/api/memo/60/undo", json={"idx": 1}, headers=H).status_code == 200
    assert t.status["htf_analyst"] == was                                   # streak 3, since, reason, set_by
    assert t.reviews[-1][1:3] == ("active", "probation")                    # logged like the Agents page's changes


def test_an_agent_the_ladder_never_placed_goes_back_to_having_no_row(tables):
    t, c = tables()
    assert c.post("/api/memo/60/act", json={"idx": 0, "action": "pause"}, headers=H).status_code == 200
    assert t.status["htf_analyst"]["rung"] == "paused"
    assert c.post("/api/memo/60/undo", json={"idx": 0}, headers=H).status_code == 200
    assert "htf_analyst" not in t.status


def test_undo_is_refused_once_the_agent_has_moved_since(tables):
    t, c = tables(status={"htf_analyst": ("probation", 1)})
    assert c.post("/api/memo/60/act", json={"idx": 0, "action": "pause"}, headers=H).status_code == 200
    assert c.post("/api/memo/60/act", json={"idx": 3, "action": "retire"}, headers=H).status_code == 200
    r = c.post("/api/memo/60/undo", json={"idx": 0}, headers=H)             # the pause: retired since
    assert r.status_code == 409 and "retired now" in r.json()["detail"]
    assert t.status["htf_analyst"]["rung"] == "retired"
    assert c.post("/api/memo/60/undo", json={"idx": 3}, headers=H).status_code == 200      # newest first works
    assert t.status["htf_analyst"]["rung"] == "paused"
    assert c.post("/api/memo/60/undo", json={"idx": 0}, headers=H).status_code == 200
    assert t.status["htf_analyst"]["rung"] == "probation"


def test_rule_based_agents_get_no_buttons(tables):
    # their runs never read a budget or a sample; retiring deep_compose would stop the job that builds every dossier
    assert memo.rules("quant") and memo.rules("deep_compose") and not memo.rules("htf_analyst")
    assert memo.acts_for("budget", "quant") == memo.acts_for("pause", "quant") == memo.acts_for("cull", "deep_compose") == []
    assert memo.acts_for("budget", "strategist") == ["budget"] and memo.acts_for("pause", "htf_analyst") == ["pause"]
    t, c = tables()
    acts = c.get("/api/memo/latest").json()["memo"]["actions"]
    assert acts[4]["acts"] == acts[5]["acts"] == [] and acts[4]["rules"] and acts[5]["rules"] and not acts[0]["rules"]
    assert c.post("/api/memo/60/act", json={"idx": 5, "action": "pause"}, headers=H).status_code == 409
    assert "quant" not in t.status


def test_what_an_earlier_memo_did_stays_undoable_on_home_after_the_next_memo(tables):
    t, c = tables(scales={"strategist": 1.0})
    assert c.post("/api/memo/59/act", json={"idx": 2, "action": "budget", "scale": 0.5}, headers=H).status_code == 200
    d = c.get("/api/memo/latest").json()
    assert d["memo"]["id"] == 60 and [(x["report_id"], x["idx"], x["name"]) for x in d["recent"]] == [(59, 2, "Strategist")]
    assert c.post("/api/memo/59/undo", json={"idx": 2}, headers=H).status_code == 200
    assert c.get("/api/memo/latest").json()["recent"] == []


def test_the_header_is_checked_before_the_body():
    app = FastAPI()
    app.include_router(memo.router)
    c = TestClient(app)
    assert c.post("/api/memo/1/act", json={"idx": 99, "action": "delete"}).status_code == 403   # not 422
    assert c.post("/api/memo/1/undo", json={"idx": -1}).status_code == 403

"""The Agents page (albatross/api/agents.py): the map's metadata, model names, grades and the assembled payload."""

from datetime import UTC, datetime

from albatross.agents import graph
from albatross.api import agents as ag


def test_every_agent_on_the_map_has_a_place_and_a_role():
    nodes = set(graph.depth())
    assert nodes <= set(graph.ROLE) and set(graph.LANE.values()) <= {"majors", "deep"}
    assert {d for lands in graph.LANDS.values() for d in lands} <= set(graph.DEST) and set(graph.LANDS) <= nodes
    assert set(graph.RAW) == {lane["id"] for lane in ag.LANES}
    assert not graph.cycles()                                      # factor rank's edges add no loop
    assert sorted(graph.ORDER) == sorted(nodes)                     # every node has its place in a column


def test_model_names_come_from_what_each_agent_actually_called():
    assert ag.model_of("deepseek/deepseek-v4.1-flash", graph.MODEL) == {"group": "api", "name": "DeepSeek V4.1 Flash",
                                                                         "slug": "deepseek/deepseek-v4.1-flash"}
    assert ag.model_of("google/gemini-3.8-flash", graph.MODEL)["name"] == "Gemini 3.8 Flash"
    assert ag.model_of("typesafe/jev-1.13.0", graph.MODEL)["group"] == "jev"
    assert ag.model_of("grok:default", graph.MODEL)["name"] == "Grok (CLI)"
    assert ag.model_of("claude:opus", graph.MODEL) == {"group": "cli", "name": "Claude Opus (CLI)", "slug": "claude:opus"}
    assert ag.model_of(None, graph.RULES)["group"] == "code" and ag.model_of(None, graph.MODEL)["group"] == "idle"


def test_critics_are_judged_only_on_enough_graded_ideas():
    few = [{"status": "proposed", "r": -0.3, "jev_veto": 0.1}] * 20 + [{"status": "vetoed", "r": -0.5, "jev_veto": 0.4}] * 5
    g = ag.critic_grades(few)
    assert g["strategist_critic"]["kind"] == "early" and g["jev_critic"]["kind"] == "early"
    # 90 ideas: what Jev most wanted vetoed lost 1R, what it liked made 0.5R; the critic's vetoes did 0.6R worse
    rows = ([{"status": "proposed", "r": 0.5, "jev_veto": 0.05}] * 30 + [{"status": "proposed", "r": 0.0, "jev_veto": 0.2}] * 30
            + [{"status": "vetoed", "r": -1.0, "jev_veto": 0.4}] * 30)
    g = ag.critic_grades(rows)
    assert g["jev_critic"]["kind"] == "edge" and g["strategist_critic"]["kind"] == "edge"


def test_the_payload_places_every_node_and_grades_it_from_the_record():
    out = {"hours": 24, "summary": {"calls": 10, "outputs": 30, "spend_usd": 1.5, "pool_usd": 0.5},
           "graph": {"loops": [], "attention_loops": [{"agents": ["oracle", "social_pulse"]}]},
           "agents": [{"agent": "ltf_analyst", "calls": 7, "outputs": {"signals": 20}, "output_total": 20, "cost_usd": 1.0,
                       "pool_usd": 0.0, "flags": [{"kind": "idle", "label": "idle"}], "paused": False, "budget_scale": 1.0},
                      {"agent": "strategist", "calls": 3, "outputs": {"ideas": 2}, "output_total": 2, "cost_usd": 0.5,
                       "pool_usd": 0.0, "flags": [], "quality": {"graded": 84, "won": 0.05, "avg_r": -0.27}}]}
    intel = {"edge": [{"source": "ltf_analyst", "status": "none", "headline_tf": "15m",
                       "tfs": [{"tf": "15m", "n": 1055, "hit_rate": 0.46, "edge_pts": -9.6}]},
                      {"source": "strategist", "status": "worse", "headline_tf": "1h",
                       "tfs": [{"tf": "1h", "n": 400, "hit_rate": 0.44, "edge_pts": -8.0}]}],
             "record": {"backtest": {"weeks": 57, "mean_spread_pct": 2.3, "won_pct": 67, "t": 2.0}, "live": {"weeks": 0}}}
    body = ag.assemble(out, intel, {"ltf_analyst": "deepseek/deepseek-v4.1-flash"}, {}, datetime(2026, 10, 1, tzinfo=UTC))
    by = {n["id"]: n for n in body["nodes"]}
    dep = graph.depth()
    assert all(by[n]["col"] == dep[n] + 1 for n in dep) and len(body["columns"]) == max(dep.values()) + 3
    assert by["ideas"]["kind"] == "dest" and by["ideas"]["col"] == len(body["columns"]) - 1
    assert by["ltf_analyst"]["grade"] == {"kind": "none", "text": "46% right on 15m, 9.6 points behind a one-way caller (1,055 graded)"}
    assert by["ltf_analyst"]["flags"] == [] and by["ltf_analyst"]["model"]["name"] == "DeepSeek V4.1 Flash"
    assert by["strategist"]["grade"]["kind"] == "behind" and "ideas -0.27R average" in by["strategist"]["grade"]["text"]
    assert by["factor_rank"]["grade"]["kind"] == "edge" and "2.30% a week" in by["factor_rank"]["grade"]["text"]
    assert by["deep_fit"]["grade"]["text"] == "Not graded yet"
    kinds = {(e["from"], e["to"]): e["kind"] for e in body["edges"]}
    assert kinds[("factor_rank", "strategist")] == "reads" and kinds[("strategist", "social_pulse")] == "attention"
    assert kinds[("jev_scout", "ideas")] == "lands" and body["summary"]["attention_loops"] == 1

"""Who reads whom: the map that keeps the agents from feeding on their own conclusions.

READS[x] lists the producers whose *output* agent x takes as evidence (their signals, reads, reports or scores);
raw data (candles, derivatives, posts, headlines) is not a producer. ATTENTION[x] lists producers that only decide
*what* x looks at, not what it concludes (e.g. Social Pulse reads the coins in open Strategist ideas).

A cycle in READS is a recursive loop: an agent's conclusion comes back to it as someone else's evidence, so it
ends up agreeing with itself. tests/test_output.py fails on any cycle not in KNOWN_LOOPS, and the Output page
shows each loop, each agent's depth (how many model hops separate it from raw data) and the attention loops.
Keep this map in step with the code: an agent that starts reading another agent's output adds the edge here.
"""

MODEL = "model"          # an LLM (or Jev) call
RULES = "rules"          # deterministic code

KIND = {
    "quant": RULES, "ensemble": RULES, "deep_compose": RULES, "factor_rank": RULES,
    "ltf_analyst": MODEL, "htf_analyst": MODEL, "flow_analyst": MODEL, "macro_analyst": MODEL,
    "social_pulse": MODEL, "news_scout": MODEL, "strategist": MODEL, "strategist_critic": MODEL,
    "jev_critic": MODEL, "oracle": MODEL, "reports": MODEL, "events_news": MODEL, "oracle_calendar": MODEL,
    "jev_analyst": MODEL, "jev_scout": MODEL, "deep_x_scan": MODEL, "deep_reddit_scan": MODEL, "deep_fit": MODEL,
    "deep_take": MODEL, "ceo": MODEL, "strategy_lab": RULES, "strategy_researcher": MODEL,
}

LABEL = {
    "quant": "Quant model", "ensemble": "Blended call", "deep_compose": "Deep End composer", "factor_rank": "Factor rank",
    "ltf_analyst": "LTF analyst", "htf_analyst": "HTF analyst", "flow_analyst": "Flow analyst",
    "macro_analyst": "Macro analyst", "social_pulse": "Social Pulse (X)", "news_scout": "News Scout",
    "strategist": "Strategist", "strategist_critic": "Strategist's critic", "jev_critic": "Jev critic",
    "oracle": "Oracle", "reports": "Reports", "events_news": "Calendar from news", "oracle_calendar": "Calendar research",
    "jev_analyst": "Jev analyst", "jev_scout": "Jev trade scout", "deep_x_scan": "Deep End X Scout",
    "deep_reddit_scan": "Deep End Reddit scout", "deep_fit": "Deep End fit research", "deep_take": "The Albatross take",
    "ceo": "CEO", "strategy_lab": "Strategy lab", "strategy_researcher": "Strategy researcher",
}

READS: dict[str, list[str]] = {
    # first-hand: raw data only
    "quant": [], "ltf_analyst": [], "htf_analyst": [], "flow_analyst": [], "macro_analyst": [],
    "social_pulse": [], "news_scout": [], "events_news": [], "oracle_calendar": [], "jev_analyst": [],
    "deep_x_scan": [], "deep_reddit_scan": [], "factor_rank": [],
    # second-hand and beyond
    "ensemble": ["quant", "ltf_analyst", "htf_analyst", "flow_analyst", "social_pulse", "macro_analyst"],
    "strategist": ["ensemble", "news_scout", "social_pulse", "macro_analyst", "events_news", "oracle_calendar", "factor_rank"],
    "strategist_critic": ["strategist"],
    "jev_critic": ["strategist"],
    "oracle": ["ensemble", "strategist", "news_scout", "social_pulse"],
    "reports": ["strategist", "ensemble", "social_pulse", "news_scout", "macro_analyst", "factor_rank"],
    "jev_scout": ["strategist", "ensemble", "social_pulse", "news_scout", "events_news", "oracle_calendar", "factor_rank"],
    "deep_fit": ["deep_x_scan", "social_pulse"],
    "deep_compose": ["deep_x_scan", "deep_reddit_scan", "deep_fit"],
    "deep_take": ["deep_compose"],
    # the top: graded records, never conclusions. The paper books are built from the Strategist's ideas and the Deep
    # End's dossiers (the edges drawn); it also reads every agent's grade and cost, which nothing it writes feeds back.
    "ceo": ["strategist", "deep_compose"],
    # the lab tests strategies on the dossiers (memecoins) and on raw daily candles (factor rank, which it re-runs
    # itself); the researcher reads only the lab's outcomes
    "strategy_lab": ["deep_compose"],
    "strategy_researcher": ["strategy_lab"],
}

# Agents that also read their own earlier output on purpose (state, not evidence): open ideas, the paper book.
SELF = {"strategist": "its open ideas", "social_pulse": "when it last read a coin",
        "strategy_researcher": "how its earlier proposals scored (outcomes, never their wording)",
        "jev_scout": "its open paper trades, and its graded record to correct its odds (outcomes, never its reasoning)"}

ATTENTION: dict[str, list[str]] = {
    "social_pulse": ["strategist", "oracle", "news_scout"],          # reads the coins in open ideas and fresh news
    "deep_fit": ["deep_x_scan"],
    "strategy_lab": ["strategy_researcher"],                          # what to test; code decides what survives
}

# ─── the Agents page (albatross/api/agents.py): where each agent sits, what it does, where its output lands ───
LANE = {"deep_x_scan": "deep", "deep_reddit_scan": "deep", "deep_fit": "deep", "deep_compose": "deep",
        "deep_take": "deep"}   # the rest: "majors"
ROLE = {
    "factor_rank": "Ranks the liquid coins by relative strength, daily.",
    "quant": "Scores each coin and timeframe from chart features.",
    "ltf_analyst": "Reads 5m to 1h charts per coin and calls direction.",
    "htf_analyst": "Reads 4h to weekly charts and calls direction.",
    "flow_analyst": "Reads funding, open interest and flows.",
    "macro_analyst": "Reads rates, the dollar, liquidity and ETF flows.",
    "social_pulse": "Reads X on each coin and the market mood.",
    "news_scout": "Scores headlines for impact and sentiment.",
    "events_news": "Pulls dated events out of headlines.",
    "oracle_calendar": "Researches upcoming events on the web.",
    "jev_analyst": "Direction odds per coin on 1h, 4h and 1d. Kept out of the ensemble.",
    "ensemble": "Weights every source by its graded record into one bias per coin and timeframe.",
    "strategist": "Turns the evidence into bias calls and trade ideas, hourly.",
    "strategist_critic": "Reviews each idea: keep, downgrade or veto.",
    "jev_critic": "Jev's keep, downgrade and veto odds on each idea.",
    "oracle": "Daily thesis, coin notes and the occasional idea.",
    "reports": "Briefs every 4 hours and a daily report.",
    "jev_scout": "Scores a long and a short on every coin every 5 minutes and keeps a paper book.",
    "deep_x_scan": "Reads X for each hot memecoin.",
    "deep_reddit_scan": "Reads Reddit through Google search for the most-called memecoins and majors in the news.",
    "deep_fit": "Researches whether a coin's meme lands with the trenches right now.",
    "deep_compose": "Builds each token's dossier and its score from chain, market, X and fit; Reddit, FOMO, wallets "
                    "and GitHub show beside it, unscored until graded.",
    "deep_take": "Writes a short read on each dossier.",
    "ceo": "Reads every desk's graded record twice a day and recommends what to cut, keep or fund. Changes nothing itself.",
    "strategy_lab": "Tests about 250 new memecoin and factor strategies a day against a bar that rises with each one "
                    "tried, and paper-trades up to 20 survivors.",
    "strategy_researcher": "Proposes about 30 new strategies a day from the lab's results. Code tests them; it decides "
                           "nothing.",
}
# Top to bottom within a column: chart readers, then funding, macro, headlines, X and the web (as RAW lists them);
# ideas-bound agents above briefs-bound ones, so the wires cross as little as they can.
ORDER = ["factor_rank", "quant", "ltf_analyst", "htf_analyst", "jev_analyst", "flow_analyst", "macro_analyst",
         "news_scout", "events_news", "social_pulse", "oracle_calendar", "deep_x_scan", "deep_reddit_scan",
         "ensemble", "strategist", "deep_fit",
         "jev_scout", "jev_critic", "strategist_critic", "oracle", "reports", "deep_compose", "deep_take", "strategy_lab",
         "strategy_researcher", "ceo"]
# Where the output ends up: id -> (name, where on the site, the view to open).
DEST = {"matrix": ("Bias matrix", "Majors › All coins", "matrix"), "ideas": ("Ideas ledger", "Results › Ideas ledger", "ideas"),
        "briefs": ("Briefs & daily report", "Results › Reports", "reports"), "feed": ("Deep End feed & dossiers", "Memecoins › Feed", "deep"),
        "lab": ("Strategy lab", "Results › Strategy lab", "lab")}
LANDS = {"ensemble": ["matrix"], "strategist": ["matrix", "ideas"], "strategist_critic": ["ideas"], "jev_critic": ["ideas"],
         "oracle": ["ideas", "briefs"], "reports": ["briefs"], "jev_scout": ["ideas"],
         "deep_compose": ["feed"], "deep_take": ["feed"], "ceo": ["briefs"], "strategy_lab": ["lab"]}
RAW = {"majors": [("Charts", "Hyperliquid, OKX and CoinGecko candles, turned into features"),
                  ("Funding & open interest", "Hyperliquid, Coinalyze"),
                  ("Macro & ETF flows", "rates, the dollar, liquidity, ETF prints"),
                  ("Headlines", "crypto news sites"), ("X posts", "through Grok, CLI or xAI API"),
                  ("The web", "Perplexity search, through OpenRouter")],
       "deep": [("Group calls & bot trades", "Call feed"),
                ("Token data", "Birdeye, Jupiter, RugCheck, DexScreener, GeckoTerminal"),
                ("X posts", "through Grok, CLI or xAI API"), ("Reddit threads", "Google search, through Gemini"),
                ("Web search", "Perplexity"), ("FOMO traders", "fomoapi.io, unofficial"),
                ("Wallets", "Chainstack funders, RugCheck insider graphs"), ("GitHub", "public metadata, never the code")]}

# Loops that exist today and wait on a decision (named on the Output page); anything else fails the tests.
# (2026-09-30: the Oracle left the ensemble, which closed the only one: Oracle -> ensemble -> Strategist -> Oracle.)
KNOWN_LOOPS: dict[frozenset, str] = {}


def edges(graph: dict[str, list[str]] = READS) -> list[tuple[str, str]]:
    """(producer, consumer) pairs."""
    return [(src, dst) for dst, srcs in graph.items() for src in srcs]


def cycles(graph: dict[str, list[str]] = READS) -> list[list[str]]:
    """Every elementary cycle (as a list of agents, each reading the next's output), smallest first."""
    nodes = sorted(set(graph) | {s for v in graph.values() for s in v})
    readers: dict[str, list[str]] = {n: [] for n in nodes}
    for src, dst in edges(graph):
        readers[src].append(dst)
    found: set[tuple[str, ...]] = set()

    def walk(start: str, node: str, path: list[str]) -> None:
        for nxt in readers[node]:
            if nxt == start:
                i = path.index(min(path))                   # rotate so each cycle is found once
                found.add(tuple(path[i:] + path[:i]))
            elif nxt not in path and nxt > start:
                walk(start, nxt, path + [nxt])

    for n in nodes:
        walk(n, n, [n])
    return sorted((list(c) for c in found), key=lambda c: (len(c), c))


def depth(graph: dict[str, list[str]] = READS) -> dict[str, int]:
    """Model hops between each agent and raw data (0 = reads raw data only). A cycle counts once."""
    memo: dict[str, int] = {}

    def d(n: str, seen: frozenset) -> int:
        if n in memo:
            return memo[n]
        hops = [d(s, seen | {n}) + (KIND.get(s) == MODEL) for s in graph.get(n, []) if s not in seen]
        memo[n] = max(hops, default=0)
        return memo[n]

    return {n: d(n, frozenset()) for n in sorted(set(graph) | {s for v in graph.values() for s in v})}


def known(cycle: list[str]) -> str | None:
    """The explanation when a cycle is one of KNOWN_LOOPS (it contains a known pair)."""
    s = set(cycle)
    return next((why for pair, why in KNOWN_LOOPS.items() if pair <= s), None)


def summary() -> dict:
    """The map for the Output page."""
    dep = depth()
    loops = [{"agents": c, "known": known(c)} for c in cycles()]
    both = cycles({k: sorted(set(READS.get(k, [])) | set(ATTENTION.get(k, []))) for k in set(READS) | set(ATTENTION)})
    new = [c for c in both if c not in cycles()]
    # the smallest loops only: a loop through more agents that contains a smaller one adds nothing to read
    attention = [{"agents": c} for c in new if not any(set(o) < set(c) for o in new)]
    return {"nodes": [{"id": n, "label": LABEL.get(n, n), "kind": KIND.get(n, MODEL), "depth": dep[n],
                       "reads": READS.get(n, []), "attention": ATTENTION.get(n, []), "self": SELF.get(n)}
                      for n in sorted(dep, key=lambda n: (dep[n], n))],
            "loops": loops, "attention_loops": attention}

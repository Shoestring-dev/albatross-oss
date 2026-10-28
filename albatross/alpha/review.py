"""The review of 2026-10-09 on the reference install (four reviewers: memecoins, majors, measurement, alpha sources and
cost), as Alpha › Review shows it. Its numbers come from that install's data, not yours. Nothing listed here as
"abandon" was switched off there; the suggestions run beside the versions they would replace, on paper.

Edit `status` as things change: "running" (as it was), "on trial" (the suggestion runs beside it on an Alpha page),
"proposed" (not built), "done". `page` names the Alpha view where a trial can be seen.
"""

REVIEWED = "2026-10-09"

SUMMARY = ("Two real signals so far, both from cheap code: a coin's age and two red flags tell memecoin losers from the "
           "rest, and factor rank orders the liquid coins better than chance. Nothing makes money after costs yet. The "
           "AI layers that cost the most to run show no measurable edge, and the "
           "direction calls on the bias matrix grade worse than random. Intel (lead time, move size, losses avoided) "
           "isn't graded anywhere yet.")

BETS = [
    {"rank": 1, "name": "Memecoin loss avoidance", "kind": "intel",
     "evidence": "Calls on coins under 1 hour old: median −83% and 37% lost 90% or more. Coins 24 hours or older: "
                 "median −22%, 4% lost 90%. Skipping the dev-wallet and liquidity red flags was worth 22 to 43 points a "
                 "trade in both halves of the data.",
     "test": "Live tests it on every call; Loss avoidance holds two rules fixed in advance and reads them after "
             "about three weeks of calls.",
     "page": "live"},
    {"rank": 2, "name": "Factor rank", "kind": "profit",
     "evidence": "The ranking put the coins in the right order in 43 of 58 weeks, and the top fifth beat the bottom "
                 "fifth in 41, by 2.2% a week before costs. After costs it is thin: the long-short paper book made 0.58% a "
                 "week on its capital over that history.",
     "test": "The paper books need a few weeks before they read; the live record needs about 24 weeks before it means "
             "much.",
     "page": "edge"},
    {"rank": 3, "name": "Move-size forecasting", "kind": "intel",
     "evidence": "Which coins will move most next week is predictable from which moved most this week: the order "
                 "is usually right. Which direction they move is not.",
     "test": "The baseline every news, X and calendar feed must beat. Built with the intel grading.",
     "page": None},
    {"rank": 4, "name": "X shill share", "kind": "intel",
     "evidence": "Over 236 tokens, coins with few paid-sounding posts had a median of −17%; heavily shilled coins −82%. "
                 "The read lands a median 11 minutes after the first group call.",
     "test": "Grade it as a loss-avoidance flag; read faster.", "page": None},
    {"rank": 5, "name": "Unlock avoidance", "kind": "intel",
     "evidence": "The calendar research finds unlocks a median 14 days ahead. Published work finds price pressure "
                 "before most unlocks; ours has too few past unlocks to measure yet.",
     "test": "An unlock-impact table; upcoming unlocks on factor-book coins are listed on Factor edge now.",
     "page": "edge"},
]
WEAK = ("Funding carry (a yield of 2–4 points over T-bills, with a crash tail), listing notices (bots get there first), "
        "Polymarket odds (few events), copying FOMO traders or wallets, and news as an early signal (prices move as "
        "much before a headline as after).")

DECISIONS = [
    {"q": "Switch off the components marked abandon?",
     "why": "It frees most of the AI spend. On the reference install they were kept running beside their suggested "
            "replacements.", "status": None},
    {"q": "Age gate on a trading bot", "why": "Loss avoidance shows what it would have done, on paper only. Albatross "
                                              "never sends anything to a bot.", "status": None},
    {"q": "Can you trade perps where you are?",
     "why": "It decides whether the factor lane runs long and short or long hedged with BTC and ETH.", "status": None},
]

V_KEEP, V_ADJUST, V_ABANDON, V_FIXED = "keep", "adjust", "abandon", "fixed"
COMPONENTS = [
    # ─── memecoins ───
    {"lane": "memecoins", "name": "Dossier score", "verdict": V_ABANDON, "status": "on trial", "page": "avoid",
     "finding": "No link to returns over 2,641 calls, and on coins under 1 hour old it points the wrong way.",
     "change": "Replace it with the rebuilt checklist, which runs beside it on Loss avoidance."},
    {"lane": "memecoins", "name": "Red flags: dev wallet, liquidity", "verdict": V_KEEP, "status": "running",
     "finding": "Skipping them was worth 22 to 43 points a trade, in both halves of the data.", "change": ""},
    {"lane": "memecoins", "name": "Coin age", "verdict": V_ADJUST, "status": "on trial", "page": "avoid",
     "finding": "The strongest signal: under 1 hour old, median −83% and 37% lost 90% or more; 24 hours or older, "
                "median −22%.",
     "change": "Make age under 24 hours a red flag."},
    {"lane": "memecoins", "name": "Red flags: LP lock, insiders", "verdict": V_ADJUST, "status": "on trial",
     "page": "avoid", "finding": "LP points the wrong way; insiders shows nothing.",
     "change": "Show them as information, not red flags."},
    {"lane": "memecoins", "name": "Dex paid", "verdict": V_ADJUST, "status": "on trial", "page": "avoid",
     "finding": "Unpaid listings on coins older than 24 hours did far worse; under 24 hours it made no difference.",
     "change": "A red flag when still unpaid after 24 hours."},
    {"lane": "memecoins", "name": "Buy pressure and holder growth lines", "verdict": V_ADJUST, "status": "proposed",
     "finding": "Their \"good\" marks losers: by the time a group calls the coin it has usually already run.",
     "change": "Read them as warnings."},
    {"lane": "memecoins", "name": "Callers line", "verdict": V_ADJUST, "status": "proposed",
     "finding": "It never fires. Caller quality does persist: the callers who did best in the first half of the "
                "data mostly did best in the second.",
     "change": "Rate each caller rather than each group."},
    {"lane": "memecoins", "name": "X Scout", "verdict": V_ADJUST, "status": "proposed",
     "finding": "Shill share is the one AI-read number tied to outcomes (median −17% low against −82% high, 236 "
                "tokens). The read lands a median 11 minutes after the first call.",
     "change": "Score shill share and read faster."},
    {"lane": "memecoins", "name": "Fit research", "verdict": V_ABANDON, "status": "running",
     "finding": "No link to returns over 691 tokens, and it was the largest single AI cost.",
     "change": "Stop it, or run it on a small sample."},
    {"lane": "memecoins", "name": "The Albatross take", "verdict": V_ABANDON, "status": "running",
     "finding": "Restates the checklist; nothing to grade.", "change": "Pause until the checklist is rebuilt."},
    {"lane": "memecoins", "name": "Reddit scan (memecoins)", "verdict": V_ABANDON, "status": "running",
     "finding": "3 verified memecoin reads in 9 days; never on a graded call.", "change": "Keep it for majors only."},
    {"lane": "memecoins", "name": "Wallet clusters", "verdict": V_ABANDON, "status": "running",
     "finding": "The line fired on 2 calls, for a steady stream of Solana RPC calls.", "change": "Stop."},
    {"lane": "memecoins", "name": "GitHub metadata", "verdict": V_ABANDON, "status": "running",
     "finding": "A line on 10 calls.", "change": "Stop for memecoins."},
    {"lane": "memecoins", "name": "CryptoPanic", "verdict": V_ABANDON, "status": "running",
     "finding": "No items, ever.", "change": "Remove."},
    {"lane": "memecoins", "name": "FOMO traders", "verdict": V_KEEP, "status": "running",
     "finding": "Its book lost money over 370 trades (median −12.9% a trade): the mildest loser, still a loser.",
     "change": "Decide after another month of data."},
    {"lane": "memecoins", "name": "Discovery", "verdict": V_KEEP, "status": "running",
     "finding": "Finds coins a median 36 minutes before the call, but those calls did no better.",
     "change": "Keep as a sensor."},
    {"lane": "memecoins", "name": "Price pipeline and call ledger", "verdict": V_KEEP, "status": "running",
     "finding": "Accurate: prices from different sources agree to 0.2–0.4%, and 89% of calls get graded.", "change": ""},
    {"lane": "memecoins", "name": "Trading bot (from the call feed)", "verdict": V_ADJUST, "status": "paper only", "page": "avoid",
     "finding": "Many of its buys are coins under 1 hour old, the group that loses most.",
     "change": "An age gate, tested on paper."},
    # ─── majors ───
    {"lane": "majors", "name": "Factor rank", "verdict": V_KEEP, "status": "running", "page": "edge",
     "finding": "The one real ranking signal: the right order in 43 of 58 weeks, and better than chance in every quarter.",
     "change": ""},
    {"lane": "majors", "name": "Factor paper books", "verdict": V_ADJUST, "status": "on trial", "page": "edge",
     "finding": "Long-short was up over its first 9 days; long-only beat a falling market by 1.3 points. Most of the short leg is "
                "small coins only Hyperliquid lists.",
     "change": "Add the version hedged with BTC and ETH (in the lab now) and funding in the history."},
    {"lane": "majors", "name": "Quant model as a caller", "verdict": V_ABANDON, "status": "running",
     "finding": "Grades worse than random, and betting against it loses after costs.",
     "change": "Stop it calling; keep the features it computes."},
    {"lane": "majors", "name": "LTF and Flow analysts", "verdict": V_ABANDON, "status": "running",
     "finding": "No skill; already paused to a tenth on the ladder.", "change": "Retire."},
    {"lane": "majors", "name": "HTF analyst", "verdict": V_ADJUST, "status": "running",
     "finding": "Slightly worse than random over 716 calls, and nothing at all on the 4-hour charts.",
     "change": "Daily and weekly only."},
    {"lane": "majors", "name": "Blended call (ensemble)", "verdict": V_ADJUST, "status": "running",
     "finding": "Worse than random calls with the same mix of longs and shorts: 4 points a call worse, too large "
                "to be luck.",
     "change": "Abstain when no input has shown skill, and grade it like any source."},
    {"lane": "majors", "name": "Strategist", "verdict": V_ADJUST, "status": "running",
     "finding": "Its direction calls do worse than random, and its 145 trade ideas lost about a sixth of their "
                "risk each.",
     "change": "Flag the factor book's exceptions instead: an unlock, delisting or major news inside the hold."},
    {"lane": "majors", "name": "Sol shadows (four)", "verdict": V_ABANDON, "status": "running",
     "finding": "A large share of the AI spend, testing whether a stronger model calls direction better.",
     "change": "Keep one at most, with an end date."},
    {"lane": "majors", "name": "Strategist's critic", "verdict": V_ABANDON, "status": "running",
     "finding": "The ideas it vetoed did no worse than the ones it kept.", "change": "Let it lapse with the ideas."},
    {"lane": "majors", "name": "Jev analyst, scout and critic", "verdict": V_ABANDON, "status": "running",
     "finding": "Retired, harmful and no skill; scout and critic paused.",
     "change": "Retire."},
    {"lane": "majors", "name": "Oracle", "verdict": V_ADJUST, "status": "running",
     "finding": "42 of 63 escalations followed a move that had already happened.",
     "change": "Keep the daily thesis and weekly review; drop its trade ideas."},
    {"lane": "majors", "name": "Calendar research", "verdict": V_KEEP, "status": "running", "page": "edge",
     "finding": "Finds unlocks a median 14 days ahead.", "change": "Measure what unlocks do to price."},
    {"lane": "majors", "name": "Event feeds (listings, macro, Polymarket, news dates)", "verdict": V_KEEP,
     "status": "running", "finding": "Free, and they feed the calendar.", "change": ""},
    {"lane": "majors", "name": "News Scout", "verdict": V_ADJUST, "status": "running",
     "finding": "Its impact score doesn't predict the size of the next move, and prices move as much before a "
                "headline as after.",
     "change": "Keep the tagging; stop using impact as a trigger until it beats trailing volatility."},
    {"lane": "majors", "name": "Social Pulse", "verdict": V_ADJUST, "status": "running",
     "finding": "Its mood reading has no link to the next day's returns, and it is the largest X reader.",
     "change": "Read only the factor book's top and bottom fifths plus BTC and ETH; give the rest to the X Scout."},
    {"lane": "majors", "name": "Funding, open interest, macro, MVRV", "verdict": V_KEEP, "status": "running",
     "finding": "Context; funding and open-interest extremes not tested yet.", "change": "Test the extremes."},
    {"lane": "majors", "name": "Reports and the bias matrix", "verdict": V_ADJUST, "status": "running",
     "finding": "They lead with direction calls that grade worse than random.",
     "change": "Lead with factor rank and loss avoidance; label unproven calls."},
    # ─── measuring ───
    {"lane": "measuring", "name": "Strategy lab", "verdict": V_FIXED, "status": "done",
     "finding": "Its first version tested factor strategies on a grid that moved a day every day, kept each "
                "strategy's bar from the day it was tested, compared long-only with long-short, and counted memecoin "
                "trades as independent when whole days move together.",
     "change": "Fixed; every strategy was tested again."},
    {"lane": "measuring", "name": "Direction grading clock", "verdict": V_ADJUST, "status": "proposed",
     "finding": "Some calls were graded from a price before the call existed (the LTF analyst's 1h calls by 33 to 47 "
                "minutes).",
     "change": "Grade from the first price after each call, and regrade 30 days."},
    {"lane": "measuring", "name": "Intel grading", "verdict": V_ADJUST, "status": "proposed",
     "finding": "Nothing grades headlines, flag changes or X reads for lead time, move size or losses avoided.",
     "change": "A ledger of checkable claims, graded against a move-size baseline. Next build."},
    {"lane": "measuring", "name": "Culling ladder", "verdict": V_ADJUST, "status": "running",
     "finding": "It works: pausing LTF and Flow cut their cost by about 90%. The ensemble is exempt.",
     "change": "Grade the ensemble; count each period once in the bands."},
    {"lane": "measuring", "name": "CEO memo", "verdict": V_ADJUST, "status": "running",
     "finding": "It caught the Jev failure loop, then repeated the same 4 or 5 actions every 12 hours.",
     "change": "Give it this review's open decisions and the lab."},
    {"lane": "measuring", "name": "Home page", "verdict": V_ADJUST, "status": "proposed",
     "finding": "It says \"no verdict yet\" while the every-call book lost money on 9 of 10 days.",
     "change": "Say plainly what loses money, and show the loss-avoidance result."},
]


def payload() -> dict:
    return {"reviewed": REVIEWED, "summary": SUMMARY, "bets": BETS, "weak": WEAK, "decisions": DECISIONS,
            "components": COMPONENTS}

"""The structured event feeds (albatross/events/macro, listings, polymarket) on payloads recorded 2026-09-30 from the
live endpoints (tests/fixtures/events/), plus the calendar API's shaping. The FRED rows are hand-written in the shape
FRED documents for fred/releases/dates: no FRED key on the machine that recorded the rest."""
import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from albatross.api.calendar import shape
from albatross.events import listings, macro, polymarket
from albatross.events.store import make_key

FX = Path(__file__).parent / "fixtures" / "events"
NOW = datetime(2026, 9, 30, 1, 0, tzinfo=UTC)
TIERS = {"BTC": 1, "SOL": 1, "INJ": 2, "PAXG": 2, "VVV": 3, "CASHCAT": 3, "REZ": 3, "AEON": 3, "RVN": 3}


def _key(e):
    return make_key(e.kind, e.subject, e.starts_at, e.venue)


def _text(name):
    return (FX / name).read_text()


# ─── macro ──────────────────────────────────────────────────────────────────
def test_fomc_meetings_this_year_and_next_on_their_decision_day():
    evs = macro.fomc(_text("fomc_calendar.html"), NOW.date())
    assert [_key(e) for e in evs[:2]] == ["macro:fomc:fed:2026-10-28", "macro:fomc:fed:2026-12-09"]
    assert len(evs) == 10                       # Oct + Dec 2026, eight in 2027; 2025 and the past of 2026 left out
    assert evs[1].title.endswith("with new projections (dot plot)") and "projections" not in evs[0].title
    assert {e.importance for e in evs} == {5} and {e.status for e in evs} == {"scheduled"}
    assert {e.precision for e in evs} == {"day"} and evs[0].starts_at == datetime(2026, 10, 28, tzinfo=UTC)
    # A meeting from two days ago is still on the calendar, as happened.
    recent = macro.fomc(_text("fomc_calendar.html"), date(2026, 9, 17))
    assert (_key(recent[0]), recent[0].status) == ("macro:fomc:fed:2026-09-16", "happened")


def test_fomc_cross_month_meetings_and_notation_votes():
    page = ('<h4><a id="1">2026 FOMC Meetings</a></h4>'
            '<div class="fomc-meeting__month col"><strong>Oct/Nov</strong></div>\n'
            '<div class="fomc-meeting__date col">31-1*</div>'
            '<div class="fomc-meeting__month col"><strong>August</strong></div>\n'
            '<div class="fomc-meeting__date col">22 (notation vote)</div>')
    assert [_key(e) for e in macro.fomc(page, date(2026, 8, 1))] == ["macro:fomc:fed:2026-11-01"]


FRED_ROWS = [
    {"release_id": 10, "release_name": "Consumer Price Index", "date": "2026-10-15"},
    {"release_id": 46, "release_name": "Producer Price Index", "date": "2026-10-16"},
    {"release_id": 50, "release_name": "Employment Situation", "date": "2026-10-02"},
    {"release_id": 54, "release_name": "Personal Income and Outlays", "date": "2026-10-30"},
    {"release_id": 53, "release_name": "Gross Domestic Product", "date": "2026-09-29"},
    {"release_id": 10, "release_name": "Consumer Price Index", "date": "2026-12-10"},     # beyond 60 days
]


def test_fred_release_dates():
    evs = {_key(e): e for e in macro.releases(FRED_ROWS, NOW.date())}
    assert set(evs) == {"macro:cpi:bls:2026-10-15", "macro:jobs-report:bls:2026-10-02", "macro:pce:bea:2026-10-30",
                        "macro:gdp:bea:2026-09-29"}
    assert [evs[k].importance for k in sorted(evs)] == [5, 3, 4, 4]      # cpi, gdp, jobs, pce
    assert evs["macro:gdp:bea:2026-09-29"].status == "happened"
    assert evs["macro:cpi:bls:2026-10-15"].status == "scheduled"


def test_a_renumbered_fred_release_fails_loudly():
    with pytest.raises(RuntimeError, match="expected 'consumer price index'"):
        macro.releases([{"release_id": 10, "release_name": "Retail Sales", "date": "2026-10-15"}], NOW.date())


# ─── OKX ────────────────────────────────────────────────────────────────────
def _okx(listing_file, i):
    return json.loads(_text(listing_file))["data"][0]["details"][i]


def test_okx_listing_takes_the_trading_open_time_from_the_article():
    [e] = listings.okx_events(_okx("okx_new_listings.json", 0), _text("okx_article_vvv.txt"), TIERS, NOW)
    assert _key(e) == "listing:vvv:okx:2026-09-15"
    assert (e.starts_at, e.precision) == (datetime(2026, 9, 15, 14, tzinfo=UTC), "exact")
    assert e.title == "OKX lists VVV (Venice) for spot trading" and e.assets == ["VVV"] and e.importance == 3
    assert e.status == "happened" and "spot trading will open at Sep 15, 2026 14:00 UTC" in e.summary


def test_okx_time_formats():
    re_ann = {"title": "OKX to list RE (Re) for spot trading and convert pre-market futures to standard perpetual "
                       "futures", "url": "u", "pTime": "1781748010269"}
    [e] = listings.okx_events(re_ann, _text("okx_article_re.txt"), TIERS, NOW)
    assert (e.subject, e.starts_at, e.importance) == ("RE", datetime(2026, 6, 18, 14, tzinfo=UTC), 2)
    grvt = {"title": "OKX will launch GRVT/USD for spot trading", "url": "u", "pTime": "1785380409238"}
    [e] = listings.okx_events(grvt, _text("okx_article_grvt_usd.txt"), TIERS, NOW)
    assert (e.subject, e.starts_at) == ("GRVT", datetime(2026, 7, 30, 15, tzinfo=UTC))


def test_okx_delisting_ends_with_the_last_pair():
    evs = listings.okx_events(_okx("okx_delistings.json", 0), _text("okx_article_delist_dora.txt"), TIERS, NOW)
    assert [e.subject for e in evs] == ["DORA", "ICX", "STORJ", "ZEUS", "ELF"]
    icx = evs[1]
    # ICX/USDC and ICX/EUR go on Sep 30, ICX/USDT on Oct 3: ICX leaves OKX on Oct 3.
    assert (_key(icx), icx.starts_at, icx.status) == ("delisting:icx:okx:2026-10-03",
                                                      datetime(2026, 10, 3, 8, tzinfo=UTC), "scheduled")
    assert icx.title == "OKX delists ICX (spot trading)" and icx.importance == 2


def test_okx_single_pair_changes_are_minor():
    [e] = listings.okx_events(_okx("okx_delistings.json", 1), _text("okx_article_delist_aeon_usd.txt"), TIERS, NOW)
    assert (e.title, e.importance, e.starts_at) == ("OKX drops the AEON/USD pair", 1,
                                                     datetime(2026, 9, 30, 8, tzinfo=UTC))


def test_okx_housekeeping_is_skipped_and_titles_parse():
    assert listings.okx_events({"title": "OKX completed TON token migration", "url": "u", "pTime": "1"}, "", TIERS,
                               NOW) == []
    assert listings.okx_tickers("OKX to delist GODS, PRCL and DUCK spot trading pairs") == ["GODS", "PRCL", "DUCK"]
    assert listings.okx_tickers("OKX to list VVV/USDT (Venice) for spot trading") == ["VVV"]


def test_okx_notice_without_a_time_falls_back_to_its_day():
    [e] = listings.okx_events(_okx("okx_new_listings.json", 0), "no timeline here", TIERS, NOW)
    assert (e.precision, e.starts_at) == ("day", datetime(2026, 9, 15, tzinfo=UTC))


# ─── Upbit ──────────────────────────────────────────────────────────────────
NOTICES = {n["id"]: n for n in json.loads(_text("upbit_notices.json"))["data"]["notices"]}


def _upbit(nid):
    return listings.upbit_events(NOTICES[nid], _text(f"upbit_{nid}.txt"), TIERS, NOW)


def test_upbit_notice_kinds():
    kinds = {nid: listings.upbit_kind(n["title"]) for nid, n in NOTICES.items()}
    assert kinds[6618] == kinds[6561] == "listing" and kinds[6591] == "delisting" and kinds[6604] == "warning"
    assert kinds[6605] is None and kinds[6589] is None       # a warning lifted; a warning extended


def test_upbit_krw_listing_time_in_kst():
    [e] = _upbit(6618)
    assert _key(e) == "listing:cashcat:upbit:2026-09-28"
    assert (e.starts_at, e.precision) == (datetime(2026, 9, 28, 8, tzinfo=UTC), "exact")    # 17:00 KST
    assert e.title == "Upbit lists CASHCAT (KRW, BTC, USDT markets)" and e.importance == 4   # tier 3 on KRW
    assert e.sources[0].url == "https://upbit.com/service_center/notice?id=1512841039"


def test_upbit_postponed_listing_takes_the_latest_time_per_ticker():
    [rez] = _upbit(6560)                                     # 15:00 -> 18:00 -> 21:00 KST
    assert rez.starts_at == datetime(2026, 9, 10, 12, tzinfo=UTC) and rez.importance == 3   # USDT market only
    pyusd, jpyc = _upbit(6580)                               # only JPYC moved (12:00 -> 15:00 -> 18:00 KST)
    assert (pyusd.starts_at, jpyc.starts_at) == (datetime(2026, 9, 17, 3, tzinfo=UTC),
                                                 datetime(2026, 9, 17, 9, tzinfo=UTC))
    assert pyusd.importance == 3                             # untracked, but a KRW listing


def test_upbit_cancelled_listing():
    hemi, useless = _upbit(6558)
    assert (hemi.subject, hemi.status) == ("HEMI", "cancelled")
    assert (useless.status, useless.starts_at) == ("happened", datetime(2026, 9, 8, 12, 30, tzinfo=UTC))


def test_upbit_market_addition_delisting_and_warning():
    [bfc] = _upbit(6561)
    assert bfc.title == "Upbit lists BFC (KRW, USDT markets)" and bfc.starts_at == datetime(2026, 9, 10, 4, 45,
                                                                                             tzinfo=UTC)
    [icx] = _upbit(6591)
    assert (_key(icx), icx.starts_at, icx.status, icx.importance) == (
        "delisting:icx:upbit:2026-10-19", datetime(2026, 10, 19, 6, tzinfo=UTC), "scheduled", 2)
    [inj] = listings.upbit_events(NOTICES[6540], "", TIERS, NOW)
    assert (inj.kind, inj.title, inj.importance, inj.status) == (
        "other", "Upbit flags INJ with an investment warning", 3, "happened")
    assert inj.starts_at == datetime(2026, 9, 1, 8, 2, 57, tzinfo=UTC)


# ─── Coinbase / Hyperliquid diffs ───────────────────────────────────────────
def test_diff_feeds_record_a_baseline_first_and_announce_only_new_items():
    assert listings.announce(set(), {"BTC", "ETH"}) == set()                       # first run: baseline only
    assert listings.remember(set(), {"BTC", "ETH"}) == {"BTC", "ETH", listings.BASELINE}
    seen = {"BTC", "ETH", listings.BASELINE}
    assert listings.announce(seen, {"BTC", "ETH", "VVV"}) == {"VVV"}
    assert listings.remember(seen, {"BTC", "VVV"}) == {"VVV"}                      # a vanished item stays seen
    assert listings.announce({listings.BASELINE}, {"HYPE"}) == {"HYPE"}           # an empty baseline still counts


def test_seen_events():
    e = listings.seen_event("coinbase", "listing", "SOL", "for spot trading (USD)", "u", 5, TIERS, NOW)
    assert (_key(e), e.status, e.assets, e.precision) == ("listing:sol:coinbase:2026-09-30", "happened", ["SOL"],
                                                          "exact")


# ─── Polymarket ─────────────────────────────────────────────────────────────
PM = json.loads(_text("polymarket_events.json"))


def test_polymarket_one_row_per_project_at_the_first_likely_date():
    evs = {e.subject: e for e in polymarket.events(PM, NOW.date())}
    assert set(evs) == {"MetaMask", "Base", "Opensea", "Concrete", "Pump.fun"}   # OpenAI < $50k, stablecoin: not a TGE
    base = evs["Base"]                                       # 7% / 38% / 57%: the first date at 50% or more
    assert (_key(base), base.probability, base.status, base.importance) == ("tge:base:2027-12-31", 0.575, "rumored", 4)
    assert "Dec 31, 2026 7.0%; Jun 30, 2027 38%; Dec 31, 2027 57%" in base.summary
    assert base.sources[0].url == "https://polymarket.com/event/will-base-launch-a-token-in-2025-341"
    assert evs["MetaMask"].starts_at == datetime(2026, 12, 31, tzinfo=UTC)          # none at 50%: the latest date
    assert evs["Pump.fun"].kind == "airdrop"


def test_polymarket_ignores_empty_and_wide_books():
    # Concrete: Sep 30 shows 50% on an empty book (0.01/0.99) and the 2027 dates sit on 50-point spreads.
    c = next(e for e in polymarket.events(PM, NOW.date()) if e.subject == "Concrete")
    assert (_key(c), c.probability, c.importance) == ("tge:concrete:2026-12-31", 0.986, 2)
    assert "Sep 30" not in c.summary and "Mar 31, 2027" not in c.summary


def test_polymarket_fdv_ladder_joins_the_launch_row():
    o = next(e for e in polymarket.events(PM, NOW.date()) if e.subject == "Opensea")
    assert "FDV a day after launch: >$100M 16%, >$300M 18%" in o.summary          # the repeated $100M rung once


def test_polymarket_skips_a_project_that_already_launched():
    raw = json.loads(json.dumps(PM))
    concrete = next(e for e in raw if e["slug"] == "will-concrete-launch-a-token-by")
    resolved = next(m for m in concrete["markets"] if m["closed"])
    resolved["outcomePrices"] = '["1", "0"]'
    assert "Concrete" not in {e.subject for e in polymarket.events(raw, NOW.date())}


def test_polymarket_market_dates():
    md = polymarket.market_date
    assert md({"groupItemTitle": "December 31, 2026", "endDate": "2027-01-01T04:59:00Z"}) == date(2026, 12, 31)
    assert md({"groupItemTitle": "December 31", "endDate": "2026-01-01T04:59:00Z"}) == date(2025, 12, 31)
    assert md({"groupItemTitle": "June 30", "endDate": "2026-06-30T00:00:00Z"}) == date(2026, 6, 30)
    assert md({"groupItemTitle": "", "endDate": "2027-01-01T05:00:00Z"}) == date(2026, 12, 31)


# ─── API ────────────────────────────────────────────────────────────────────
def test_calendar_counts_every_kind_and_filters_the_list():
    rows = [{"kind": "listing", "id": 1}, {"kind": "macro", "id": 2}, {"kind": "listing", "id": 3}]
    out = shape(rows, {"macro"})
    assert out["counts"] == {"listing": 2, "macro": 1} and [r["id"] for r in out["events"]] == [2]
    assert len(shape(rows, set())["events"]) == 3


def test_a_failing_listing_source_does_not_stop_the_others(monkeypatch):
    import asyncio
    import httpx
    from albatross.events import listings

    async def boom(*a):
        raise httpx.HTTPStatusError("403", request=httpx.Request("GET", "https://x"), response=httpx.Response(403))

    async def fine(*a):
        return ["ok"]

    assert asyncio.run(listings._each("upbit", boom)) == []
    assert asyncio.run(listings._each("okx", fine)) == ["ok"]

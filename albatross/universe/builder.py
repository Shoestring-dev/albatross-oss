"""Builds the tradable universe: every perp-listed asset (plus liquid spot-only names),
tagged with sectors and ranked into coverage tiers. Re-run weekly."""

import logging
from collections import defaultdict

from albatross import db
from albatross.universe.taxonomy import (
    CATEGORIES, MAJORS, SPOT_MIN_VOLUME_USD, TIER1, TIER2_SIZE,
)
from albatross.venues import coinbase, coingecko, hyperliquid, okx

log = logging.getLogger(__name__)


async def build() -> dict:
    hl = [(p, ctx) for p, ctx in await hyperliquid.perps_with_contexts() if not p.delisted]
    okx_swaps = await okx.usdt_swaps()
    cb_products = await coinbase.usd_products()
    log.info("venues: hyperliquid=%d okx=%d coinbase=%d", len(hl), len(okx_swaps), len(cb_products))

    # symbol -> highest-market-cap CoinGecko coin (resolves ticker collisions)
    by_symbol: dict[str, dict] = {}
    for coin in await coingecko.markets(pages=8):
        sym = coin["symbol"].upper()
        if sym not in by_symbol or (coin.get("market_cap") or 0) > (by_symbol[sym].get("market_cap") or 0):
            by_symbol[sym] = coin
    stable_ids = {c["id"] for c in await coingecko.markets(category="stablecoins")}

    hl_volume = {p.symbol: float(ctx.get("dayNtlVlm") or 0) for p, ctx in hl}
    perp_symbols = {p.symbol for p, _ in hl} | {s["symbol"] for s in okx_swaps}
    spot_symbols = {
        p["symbol"] for p in cb_products
        if (by_symbol.get(p["symbol"], {}).get("total_volume") or 0) >= SPOT_MIN_VOLUME_USD
    }
    universe = {
        s for s in perp_symbols | spot_symbols
        if by_symbol.get(s, {}).get("id") not in stable_ids
    }

    def volume(sym: str) -> float:
        return max(hl_volume.get(sym, 0.0), by_symbol.get(sym, {}).get("total_volume") or 0.0)

    ranked = sorted((s for s in universe if s not in TIER1), key=volume, reverse=True)
    tiers = {s: 1 for s in TIER1 if s in universe}
    tiers.update({s: 2 for s in ranked[:TIER2_SIZE]})
    tiers.update({s: 3 for s in ranked[TIER2_SIZE:]})

    # Sector membership via CoinGecko category listings, matched on coingecko id.
    id_to_symbol = {by_symbol[s]["id"]: s for s in universe if s in by_symbol}
    members: dict[str, set[str]] = defaultdict(set)
    for s in MAJORS & universe:
        members[s].add("majors")
    for cat in CATEGORIES:
        for cg_id in cat.cg_ids:
            for coin in await coingecko.markets(category=cg_id, pages=2):
                if coin["id"] in id_to_symbol:
                    members[id_to_symbol[coin["id"]]].add(cat.slug)
    priority = {c.slug: c.priority for c in CATEGORIES}
    for s in universe:
        if not members[s]:
            members[s].add("other")
    primary = {s: min(members[s], key=priority.__getitem__) for s in universe}

    await _write(universe, tiers, primary, members, by_symbol, volume, hl, okx_swaps, cb_products)
    summary = {
        "assets": len(universe),
        "tier1": sum(1 for t in tiers.values() if t == 1),
        "tier2": sum(1 for t in tiers.values() if t == 2),
        "tier3": sum(1 for t in tiers.values() if t == 3),
        "by_primary_category": dict(sorted(
            ((c, sum(1 for v in primary.values() if v == c)) for c in set(primary.values())),
            key=lambda kv: -kv[1],
        )),
    }
    log.info("universe built: %s", summary)
    return summary


async def _write(universe, tiers, primary, members, by_symbol, volume, hl, okx_swaps, cb_products):
    await db.upsert(
        "categories", ["slug", "name", "directional", "priority"],
        [(c.slug, c.name, c.directional, c.priority) for c in CATEGORIES], ["slug"],
    )
    await db.upsert(
        "assets",
        ["symbol", "name", "coingecko_id", "market_cap_usd", "volume_24h_usd", "tier",
         "primary_category", "active"],
        [
            (s, by_symbol.get(s, {}).get("name"), by_symbol.get(s, {}).get("id"),
             by_symbol.get(s, {}).get("market_cap"), volume(s), tiers[s], primary[s], True)
            for s in universe
        ],
        ["symbol"],
    )
    pool = await db.pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("UPDATE assets SET active = FALSE WHERE symbol <> ALL($1::text[])", list(universe))
        await conn.execute("UPDATE assets SET updated_at = now() WHERE symbol = ANY($1::text[])", list(universe))
        ids = {r["symbol"]: r["id"] for r in await conn.fetch("SELECT id, symbol FROM assets")}
        await conn.execute("DELETE FROM asset_categories WHERE asset_id = ANY($1::int[])",
                           [ids[s] for s in universe])
        await conn.executemany(
            "INSERT INTO asset_categories (asset_id, category_slug) VALUES ($1, $2)",
            [(ids[s], c) for s in universe for c in members[s]],
        )
        market_rows = (
            [(ids[p.symbol], "hyperliquid", "perp", p.venue_symbol, p.multiplier)
             for p, _ in hl if p.symbol in universe]
            + [(ids[s["symbol"]], "okx", "perp", s["instId"], 1.0)
               for s in okx_swaps if s["symbol"] in universe]
            + [(ids[p["symbol"]], "coinbase", "spot", p["id"], 1.0)
               for p in cb_products if p["symbol"] in universe]
        )
        # Same transaction, so candle syncs never observe a moment with zero active markets.
        await conn.execute("UPDATE markets SET active = FALSE")
        await conn.executemany(
            """INSERT INTO markets (asset_id, venue, market_type, venue_symbol, price_multiplier, active)
               VALUES ($1, $2, $3, $4, $5, TRUE)
               ON CONFLICT (venue, market_type, venue_symbol) DO UPDATE SET
                 asset_id = EXCLUDED.asset_id, price_multiplier = EXCLUDED.price_multiplier,
                 active = TRUE, updated_at = now()""",
            market_rows,
        )

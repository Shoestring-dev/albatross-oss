"""Heat: which tokens every Deep End collector refreshes, and how often (docs/SPEC-DEEP-END.md section 2).

hot  = first call under 2 h ago, or >= 2 calls in the last hour, or the bot bought it in the last 2 h (an older bag is
       warm), or trending now with a market
       cap under DEEP_HOT_TRENDING_MAX_MCAP ($50M: the trenches, not BONK or WIF), or a trader opened its dossier in
       the last 10 minutes;
warm = any call or discovery in the last 24 h, or opened in the last day;
cold = everything else (refreshed only when opened).
"""

from albatross import db
from albatross.deepend.cfg import env_float

HEAT_SQL = """
WITH calls AS (
  SELECT coalesce(t.resolved_chain, t.chain) AS chain, c.address, c.called_at
  FROM micro_calls c JOIN micro_tokens t ON t.chain = c.chain AND t.address = c.address
  WHERE c.called_at > now() - interval '24 hours'),
c AS (SELECT chain, address, min(called_at) AS first_call, count(*) AS calls_24h,
             count(*) FILTER (WHERE called_at > now() - interval '1 hour') AS calls_1h
      FROM calls GROUP BY 1, 2),
first_ever AS (SELECT coalesce(t.resolved_chain, t.chain) AS chain, c2.address, min(c2.called_at) AS first_call
               FROM micro_calls c2 JOIN micro_tokens t ON t.chain = c2.chain AND t.address = c2.address
               WHERE c2.address IN (SELECT address FROM c) GROUP BY 1, 2),
held AS (SELECT coalesce(t.resolved_chain, t.chain) AS chain, tr.address, max(tr.entry_at) AS last_entry
         FROM micro_trades tr JOIN micro_tokens t ON t.chain = tr.chain AND t.address = tr.address
         WHERE tr.status = 'open' AND NOT coalesce(tr.dry, false) GROUP BY 1, 2),
seen AS (SELECT chain, address, viewed_at FROM de_interest WHERE viewed_at > now() - interval '24 hours'),
disc AS ({discovered})
SELECT k.chain, k.address, s.symbol,
       f.first_call, coalesce(c.calls_24h, 0) AS calls_24h, coalesce(c.calls_1h, 0) AS calls_1h,
       h.address IS NOT NULL AS held, h.last_entry, v.viewed_at, d.trending_at, d.first_seen AS discovered_at, d.mcap AS discovered_mcap
FROM (SELECT chain, address FROM c UNION SELECT chain, address FROM held UNION SELECT chain, address FROM seen
      UNION SELECT chain, address FROM disc) k
LEFT JOIN c ON c.chain = k.chain AND c.address = k.address
LEFT JOIN first_ever f ON f.chain = k.chain AND f.address = k.address
LEFT JOIN held h ON h.chain = k.chain AND h.address = k.address
LEFT JOIN seen v ON v.chain = k.chain AND v.address = k.address
LEFT JOIN disc d ON d.chain = k.chain AND d.address = k.address
LEFT JOIN micro_state s ON s.chain = k.chain AND s.token = k.address
"""
NO_DISCOVERY = ("SELECT NULL::text AS chain, NULL::text AS address, NULL::timestamptz AS trending_at, "
                "NULL::timestamptz AS first_seen, NULL::float AS mcap WHERE false")
DISCOVERY = """SELECT chain, address, trending_at, first_seen, (meta->'pair'->>'mcap')::float AS mcap FROM de_watch
               WHERE first_seen > now() - interval '24 hours' OR trending_at > now() - interval '24 hours'"""


def heat(r: dict, now) -> tuple[str, list[str]]:
    """-> (hot | warm | cold, why)."""
    from datetime import timedelta
    why = []
    if r.get("first_call") and now - r["first_call"] < timedelta(hours=2):
        why.append("new call")
    if (r.get("calls_1h") or 0) >= 2:
        why.append("calls this hour")
    if r.get("held") and r.get("last_entry") and now - r["last_entry"] < timedelta(hours=2):
        why.append("bot just bought it")
    if (r.get("trending_at") and now - r["trending_at"] < timedelta(minutes=30)
            and (r.get("discovered_mcap") or 0) < env_float("DEEP_HOT_TRENDING_MAX_MCAP", 50e6)):
        why.append("trending")
    if r.get("viewed_at") and now - r["viewed_at"] < timedelta(minutes=10):
        why.append("opened")
    if why:
        return "hot", why
    if (r.get("calls_24h") or 0) or r.get("discovered_at") or r.get("viewed_at") or r.get("held"):
        return "warm", ["last 24 h"]
    return "cold", []


async def tokens(tier: str | None = None) -> list[dict]:
    """Tokens with any heat (hot and warm; cold ones never appear), each with tier and why; filter by tier."""
    from datetime import UTC, datetime
    pool = await db.pool()
    has_watch = await pool.fetchval("SELECT to_regclass('de_watch') IS NOT NULL")
    rows = await pool.fetch(HEAT_SQL.format(discovered=DISCOVERY if has_watch else NO_DISCOVERY))
    now, out = datetime.now(UTC), []
    for r in rows:
        d = dict(r)
        d["tier"], d["why"] = heat(d, now)
        if tier is None or d["tier"] == tier:
            out.append(d)
    return out


async def touch(chain: str, address: str) -> None:
    """A trader opened this token's dossier: hot for 10 minutes."""
    pool = await db.pool()
    await pool.execute(
        """INSERT INTO de_interest (chain, address, viewed_at) VALUES ($1, $2, now())
           ON CONFLICT (chain, address) DO UPDATE SET viewed_at = now()""", chain, address)

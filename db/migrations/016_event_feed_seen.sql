-- What the diff-based event feeds (albatross/events/listings.py) have already seen: Coinbase USD bases, Hyperliquid
-- perps and delisted perps. A feed's first run stores its baseline (plus a '' marker row) and announces nothing;
-- later runs turn each item not in here into a listing/delisting event, then add it.
CREATE TABLE event_feed_seen (
    feed        TEXT NOT NULL,                  -- coinbase_usd | hyperliquid_perps | hyperliquid_delisted
    item        TEXT NOT NULL,                  -- base symbol (VVV) or perp name as Hyperliquid spells it (kPEPE)
    first_seen  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (feed, item)
);

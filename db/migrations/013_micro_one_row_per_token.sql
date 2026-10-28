-- Micro-cap lane: one token = one micro_tokens row, keyed by the chain it trades on (docs/SPEC-MICROCAPS.md §10).
-- A Telegram call names its chain only as a hint: none at all ('evm'), or whatever chain word or link appears
-- anywhere in the message (the feed's chain hint), so an 0x address can arrive as 'solana', or as
-- 'ethereum' while it trades on Robinhood. The same address then also arrived with its real chain (another call,
-- or a bot trade) and the token had two rows. From here on:
--   micro_tokens.chain   where the token trades once known ('evm' only while no chain is found); resolved_chain
--                        = chain, or NULL on an 'evm' row still unresolved
--   micro_calls.chain    follows its token (the raw hint stays with the feed)
--   micro_trades.chain   never changes: a bot trade happened on that chain, so it is where the token trades
-- micro_fold_tokens() below does the folding; the ingest endpoint runs it after every batch and the micro_pools job
-- after every discovery run, and this migration runs it once over the rows already stored.
-- trade_calls is untouched: a DEX call is logged only once its token is priced, i.e. under its trading chain.

-- Fold micro_tokens row (src, addr) into (dst, addr): merged into dst's row when there is one (earliest first_seen,
-- dst's status), else re-keyed. The token's calls follow it. Its micro_state row describes a chain it does not
-- trade on ('evm' is never priced; a wrong hint found no pool there), so it goes; a folded row never has pools,
-- switches or candles, and its 'no_quote' audit rows in micro_snapshots are left alone.
CREATE OR REPLACE FUNCTION micro_merge_token(src TEXT, addr TEXT, dst TEXT) RETURNS VOID LANGUAGE plpgsql AS $$
DECLARE
    s micro_tokens%ROWTYPE;
BEGIN
    SELECT * INTO s FROM micro_tokens WHERE chain = src AND address = addr FOR UPDATE;
    IF NOT FOUND OR src = dst THEN
        RETURN;
    END IF;
    PERFORM 1 FROM micro_tokens WHERE chain = dst AND address = addr FOR UPDATE;
    IF FOUND THEN
        UPDATE micro_tokens SET first_seen = least(first_seen, s.first_seen),
               first_seen_via = CASE WHEN s.first_seen < first_seen THEN s.first_seen_via ELSE first_seen_via END,
               symbol = coalesce(symbol, s.symbol), resolved_chain = coalesce(nullif(dst, 'evm'), resolved_chain)
        WHERE chain = dst AND address = addr;
        DELETE FROM micro_tokens WHERE chain = src AND address = addr;
    ELSE
        UPDATE micro_tokens SET chain = dst, resolved_chain = nullif(dst, 'evm') WHERE chain = src AND address = addr;
    END IF;
    UPDATE micro_calls SET chain = dst WHERE chain = src AND address = addr;
    DELETE FROM micro_state WHERE chain = src AND token = addr;
END $$;

-- Fold every 0x row that is only a guess into the row where the token is known to trade (a bot trade there, or a
-- pricing pool found there), when exactly one such chain exists:
--   'evm'                 -> that chain, else the chain the resolver found (micro_pools job)
--   'solana' (an 0x address is never a Solana mint) -> that chain, else 'evm' for the resolver to find
--   a named EVM chain where discovery found no pool at all, and no bot trade -> that chain
-- Returns the number of rows folded. Serialised by an advisory lock (the ingest endpoint takes it too), and repeated
-- until nothing moves, since one fold can verify the target of another.
CREATE OR REPLACE FUNCTION micro_fold_tokens() RETURNS INTEGER LANGUAGE plpgsql AS $$
DECLARE
    r RECORD;
    n INTEGER := 0;
    moved INTEGER;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('micro_tokens'));
    FOR i IN 1..3 LOOP
        moved := 0;
        FOR r IN
            WITH tok AS (
                SELECT t.chain, t.address, t.resolved_chain, s.pool IS NOT NULL AS priced,
                       EXISTS (SELECT 1 FROM micro_trades m WHERE m.chain = t.chain AND m.address = t.address) AS traded,
                       s.pools_at IS NOT NULL AND s.pool IS NULL AND NOT EXISTS (
                           SELECT 1 FROM micro_pools p WHERE p.chain = t.chain AND p.token = t.address) AS nothing_there
                FROM micro_tokens t LEFT JOIN micro_state s ON s.chain = t.chain AND s.token = t.address
                WHERE t.address LIKE '0x%'),
            known AS (
                SELECT address, min(chain) AS chain FROM tok WHERE priced OR traded GROUP BY 1 HAVING count(*) = 1)
            SELECT t.chain AS src, t.address, CASE t.chain
                       WHEN 'evm' THEN coalesce(k.chain, t.resolved_chain)
                       WHEN 'solana' THEN coalesce(k.chain, 'evm')
                       ELSE CASE WHEN t.nothing_there THEN k.chain END END AS dst
            FROM tok t LEFT JOIN known k USING (address)
            WHERE NOT t.traded
            ORDER BY t.address, t.chain
        LOOP
            CONTINUE WHEN r.dst IS NULL OR r.dst = r.src;
            PERFORM micro_merge_token(r.src, r.address, r.dst);
            moved := moved + 1;
        END LOOP;
        n := n + moved;
        EXIT WHEN moved = 0;
    END LOOP;
    RETURN n;
END $$;

SELECT micro_fold_tokens();

# Notices

## Third-party software

- **TradingView Lightweight Charts** (Apache-2.0), loaded from unpkg on memecoin charts. Each chart links to
  https://www.tradingview.com/lightweight-charts/.
- **Fonts:** Instrument Serif, Inter and JetBrains Mono (SIL Open Font License), loaded from Google Fonts.
- **Python dependencies** are listed in `pyproject.toml` and `uv.lock`; all use permissive licenses (Telethon, the
  Telegram client, is MIT).
- **The optional plans container** (`deploy/subs/`) installs the Codex CLI and Claude Code from npm when you build it.
  Neither is part of this repository; each comes under its own license.

## Artwork

The painted artwork under `albatross/api/static/art/` was generated for this project with an image model.

## Data sources

Albatross calls these services from your server, with your keys and under your accounts. Most allow personal use
freely. Showing their data to other people, or charging for it, often needs a paid plan, an attribution line or
written permission. Read each provider's current terms before you do either. This summary was checked in October
2026 and is not legal advice.

| Source | Used for | Watch out for |
|---|---|---|
| Hyperliquid | perps candles, funding, open interest | rate limits |
| OKX, Coinbase, Binance, Upbit | candles, listings notices | exchange terms restrict showing their market data to third parties without a licence |
| CoinGecko | universe, categories | free Demo plan is for personal use; paid plans include a display licence with the credit "Data provided by CoinGecko" |
| Coinalyze | Binance/Bybit/OKX funding and open interest | no published terms; ask before commercial use |
| Coin Metrics Community | market cap, MVRV | CC BY-NC 4.0: non-commercial only |
| DefiLlama | stablecoin supply | terms licence personal, non-commercial use |
| FRED | US macro series | show "This product uses the FRED® API but is not endorsed or certified by the Federal Reserve Bank of St. Louis" |
| alternative.me | Fear & Greed index | attribution next to the data |
| SoSoValue | spot ETF flows | key granted by application; ask about your use |
| Polymarket | event odds | rate limits |
| CryptoPanic | news | API plans are paid |
| DexScreener, GeckoTerminal | memecoin pools and prices | DexScreener bans re-exposing its API; GeckoTerminal data is now under CoinGecko's API terms |
| Jupiter | Solana prices | show "Powered by Jupiter"; don't sell the content itself |
| Birdeye | memecoin candles, security, holders | terms bar giving the data to third parties without written consent |
| RugCheck, honeypot.is | token safety checks | honeypot.is bars making its API available to third parties |
| Chainstack or any Solana RPC | wallet and holder reads | plan limits |
| GitHub API | public repo metadata for dev-led tokens | metadata only; Albatross never downloads repo content |
| Dune | on-chain queries | free accounts are view-only; commercial use needs permission |
| OpenRouter | every LLM agent | follow each model provider's terms |
| TypeSafe AI | the Jev decision model | |
| fomoapi.io | FOMO app traders | an unofficial relay; fomo.family's terms ban third-party collection of its data |
| xAI API | X search for Social Pulse and the X Scout | paid per call; X's terms govern showing posts |
| X, via the Grok CLI | Social Pulse, the X Scout | a consumer xAI subscription is for personal use; X's terms govern showing posts |
| Reddit, via Gemini search grounding | Reddit reads | Google's grounding terms allow results only for the user who asked |
| Your ChatGPT plan, via the Codex CLI | optional model lane | a consumer subscription is for personal use |
| Your Claude plan, via Claude Code | optional model lane | a consumer subscription is for personal use |
| Telegram groups, via your own account or the call feed | memecoin calls | Telegram's terms and each group's own rules govern collecting and showing their calls |

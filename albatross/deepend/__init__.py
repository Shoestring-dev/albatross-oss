"""The Deep End: the evidence a memecoin trader gathers from a dozen tools, in one place (docs/SPEC-DEEP-END.md).

Shared plumbing lives here (cfg, birdeye, watch, registry); each builder owns one module:
  chain.py      holders, security, dev history, insiders, top traders (Jupiter, RugCheck, Birdeye, honeypot.is)
  market.py     Birdeye candles and trade stats, dex-paid and boosts
  xintel.py     per-token X reads through Grok's X search (the CLI on the host worker, or the xAI API)
  fit.py        culture-fit research
  discovery.py  trending and new listings, CryptoPanic news
  dossier.py    the evidence checklist, score and take; the feed
"""

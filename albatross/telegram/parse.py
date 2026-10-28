"""Token addresses in a Telegram message, for the listener to log as calls. Pure functions.

What counts:
- a Solana mint: 32 to 44 base58 characters that decode to exactly 32 bytes;
- an EVM contract: 0x and 40 hex digits;
- either inside a link (dexscreener, pump.fun, birdeye, gmgn, photon, bullx, jup.ag, a bot's start link...), where the
  link can also say the chain, or that the address is a wallet or a pool rather than a token.
What doesn't: wrapped SOL, ETH and BNB, the big stablecoins, burn and system addresses, an address a link calls a wallet
or a pool, and a message that names more than MAX_PER_MESSAGE tokens (a digest such as a trending list, not a call).

DexScreener links (and an explorer's address page) usually name the pool, not the token, so an address seen only there
counts only when the message names no token any other way.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

MAX_PER_MESSAGE = 3
EVM_CHAINS = {"ethereum", "base", "bsc", "arbitrum", "robinhood"}
OTHER = "other"         # a chain Albatross doesn't track (tron, polygon, sui...): the address is left out

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58 = {c: i for i, c in enumerate(B58)}
# A boundary is anything that isn't a letter or digit, so an address glued to a referral code by "_" or "-" is found
# and a run inside a longer hash isn't.
SOL_RE = re.compile(r"(?<![A-Za-z0-9])[1-9A-HJ-NP-Za-km-z]{32,44}(?![A-Za-z0-9])")
EVM_RE = re.compile(r"(?<![A-Za-z0-9])0x[0-9a-fA-F]{40}(?![A-Za-z0-9])")
URL_RE = re.compile(r"(?i)\b(?:https?://)?(?:[a-z0-9-]+\.)+[a-z]{2,}(?:[/?][^\s<>\"'`]*)?")

# Chain names as the sites write them.
CHAIN = {"solana": "solana", "sol": "solana", "ethereum": "ethereum", "eth": "ethereum", "ether": "ethereum",
         "base": "base", "bsc": "bsc", "bnb": "bsc", "binance": "bsc", "arbitrum": "arbitrum", "arb": "arbitrum",
         "robinhood": "robinhood"}
# Chains the sites name that Albatross doesn't track.
OTHER_CHAINS = {"tron", "ton", "sui", "aptos", "polygon", "polygon_pos", "matic", "avalanche", "avax", "optimism",
                "blast", "linea", "zksync", "fantom", "sonic", "cronos", "mantle", "scroll", "abstract", "unichain",
                "berachain", "hyperevm", "hyperliquid", "sei", "near", "injective", "osmosis", "pulsechain", "celo",
                "apechain", "zora", "monad", "bitcoin", "cardano", "xrpl", "starknet", "algorand", "hedera", "stellar"}
BULLX_CHAIN = {"1399811149": "solana", "1": "ethereum", "8453": "base", "56": "bsc", "42161": "arbitrum"}
PHOTON_CHAIN = {"photon-sol": "solana", "photon-base": "base", "photon-bnb": "bsc", "photon-tron": OTHER}
EXPLORER_CHAIN = {"etherscan.io": "ethereum", "basescan.org": "base", "bscscan.com": "bsc", "arbiscan.io": "arbitrum"}

NOT_TOKENS = {
    # Solana: wrapped SOL, USDC, USDT, the system, token and common program ids, the incinerator
    "So11111111111111111111111111111111111111112", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB", "11111111111111111111111111111111",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL", "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M4uBEwF6P", "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
    "1nc1nerator11111111111111111111111111111111",
    # EVM (lowercase): the burn and native placeholders, WETH (Ethereum, Base, Arbitrum), WBNB, USDC, USDT, DAI
    "0x000000000000000000000000000000000000dead", "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2", "0x4200000000000000000000000000000000000006",
    "0x82af49447d8a07e3bd95bd0d56f35241523fbab1", "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", "0xdac17f958d2ee523a2206206994597c13d831ec7",
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", "0xaf88d065e77c8cc2239327c5edb3a432268e5831",
    "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9", "0x55d398326f99059ff775485246999027b3197955",
    "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d", "0x6b175474e89094c44da98b954eedeac495271d0f",
}


@dataclass(frozen=True)
class Found:
    chain: str          # solana | ethereum | base | bsc | arbitrum | robinhood | evm (an 0x address no link placed)
    address: str        # EVM lowercase, Solana as written


def solana_mint(s: str) -> bool:
    """Base58 that decodes to exactly 32 bytes (a leading "1" is a zero byte)."""
    if not 32 <= len(s) <= 44 or any(c not in _B58 for c in s):
        return False
    n = 0
    for c in s:
        n = n * 58 + _B58[c]
    return len(s) - len(s.lstrip("1")) + (n.bit_length() + 7) // 8 == 32


def _candidates(s: str) -> list[tuple[int, str]]:
    """(position, address) for every address-shaped string in s."""
    out = [(m.start(), m.group().lower()) for m in EVM_RE.finditer(s)]
    out += [(m.start(), m.group()) for m in SOL_RE.finditer(s) if solana_mint(m.group())]
    return sorted(out)


def _system(a: str) -> bool:
    """Burn, system and placeholder addresses: a run of ten 1s ends Solana's program and sysvar ids, and an EVM
    address below 2^32 is a precompile or a chain's system contract."""
    if a.startswith("0x"):
        return int(a, 16) < 2 ** 32
    return a.endswith("1" * 10)


def _host(host: str, *domains: str) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def link(url: str) -> list[tuple[str, str | None, str]] | None:
    """-> (address, chain or None, kind) for each address in a link to a site we know, kind being token, maybe (the page
    may be the token's pool), pool or wallet; None for any other site, whose addresses count as plain text."""
    parts = urlsplit(url if "://" in url else "https://" + url)
    host = (parts.hostname or "").removeprefix("www.")
    segs = [s.lower() for s in parts.path.split("/") if s]
    q = {k.lower(): v for k, v in parse_qs(parts.query).items()}
    rest = parts.path + "?" + parts.query
    first = segs[0] if segs else ""

    def each(kind: str, chain: str | None, where: str = rest) -> list[tuple[str, str | None, str]]:
        return [(a, chain, kind) for _, a in _candidates(where)]

    def named(slug: str | None, always: bool = False) -> str | None:
        """always: the path's first segment is always a chain on this site."""
        if not slug:
            return None
        slug = slug.lower()
        return CHAIN.get(slug) or (OTHER if always or slug in OTHER_CHAINS else None)

    if _host(host, "dexscreener.com"):
        return each("maybe", named(first, always=len(segs) > 1))
    if _host(host, "pump.fun"):
        return each("wallet" if first == "profile" else "token", "solana")
    if _host(host, "birdeye.so"):
        chain = named((q.get("chain") or [None])[0] or (first if "token" in segs[1:2] else None))
        kind = "wallet" if "profile" in segs else "token" if "token" in segs else "maybe"
        return each(kind, chain)
    if _host(host, "gmgn.ai"):
        kind = "wallet" if "address" in segs else "token" if "token" in segs else "maybe"
        return each(kind, named(first) if len(segs) > 1 else None)
    if _host(host, "tinyastro.io"):                           # Photon
        return each("pool" if "lp" in segs else "token", PHOTON_CHAIN.get(host.split(".")[0]))
    if _host(host, "bullx.io", "bull-x.io"):
        cid = (q.get("chainid") or [None])[0]
        return each("token", BULLX_CHAIN.get(cid, OTHER) if cid else None)
    if _host(host, "dextools.io"):
        if "pair-explorer" in segs:
            i = segs.index("pair-explorer")
            return each("pool", named(segs[i - 1]) if i else None)
        return each("maybe", None)
    if _host(host, "geckoterminal.com"):
        kind = "pool" if "pools" in segs else "token" if "tokens" in segs else "maybe"
        return each(kind, named(first) if len(segs) > 1 else None)
    if _host(host, "solscan.io", "solana.fm"):
        kind = "token" if "token" in segs else "wallet" if "account" in segs else "maybe"
        return each(kind, "solana")
    for domain, chain in EXPLORER_CHAIN.items():
        if _host(host, domain):
            return each("token" if "token" in segs else "maybe", chain)
    if _host(host, "axiom.trade"):
        return each("maybe", None)
    return None


def addresses(text: str | None, urls: Iterable[str] = ()) -> list[Found]:
    """The tokens a message calls, in the order it names them, at most MAX_PER_MESSAGE. urls: links the text doesn't
    show (a word linked to a URL, a button under the message)."""
    text = text or ""
    found: list[tuple[int, str, str | None, str]] = []      # (position, address, chain hint, kind)
    plain = list(text)
    for m in URL_RE.finditer(text):
        got = link(m.group())
        if got is not None:
            found += [(m.start(), a, chain, kind) for a, chain, kind in got]
            plain[m.start():m.end()] = " " * (m.end() - m.start())     # its addresses are read once, as the link's
    found += [(i, a, None, "text") for i, a in _candidates("".join(plain))]
    for n, url in enumerate(urls):
        got = link(url)
        found += [(len(text) + n, a, chain, kind) for a, chain, kind in
                  (got if got is not None else [(a, None, "text") for _, a in _candidates(url)])]
    found.sort(key=lambda f: f[0])

    hint: dict[str, str] = {}
    refused = set()
    for _, a, chain, kind in found:
        if kind in ("wallet", "pool") or chain == OTHER:
            refused.add(a)
        elif chain and a not in hint:
            hint[a] = chain
    ok = [f for f in found if f[1] not in refused and f[1] not in NOT_TOKENS and not _system(f[1])]
    sure = list(dict.fromkeys(a for _, a, _, kind in ok if kind in ("text", "token")))
    picked = sure or list(dict.fromkeys(a for _, a, _, kind in ok if kind == "maybe"))
    if len(picked) > MAX_PER_MESSAGE:
        return []
    return [Found(_chain(a, hint.get(a)), a) for a in picked]


def _chain(address: str, hint: str | None) -> str:
    if address.startswith("0x"):
        return hint if hint in EVM_CHAINS else "evm"
    return "solana"             # base58 is only a Solana mint here, whatever a link's path says

"""Our sector taxonomy, mapped onto CoinGecko categories.

Priority decides an asset's primary_category when it belongs to several (lower wins):
specific narratives beat broad buckets, so PEPE is 'memes' rather than 'l1-ecosystem'.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Category:
    slug: str
    name: str
    cg_ids: tuple[str, ...]
    priority: int
    directional: bool = True


CATEGORIES: list[Category] = [
    Category("majors", "Majors", (), 0),
    Category("memes", "Memes", ("meme-token", "solana-meme-coins"), 10),
    Category("ai_agents", "AI Agents", ("ai-agents",), 11),
    Category("ai", "AI", ("artificial-intelligence",), 12),
    Category("perp_dex", "Perp DEX", ("decentralized-perpetuals",), 13),
    Category("lst_restaking", "Liquid Staking / Restaking",
             ("liquid-staking-governance-tokens", "restaking", "liquid-restaking-governance-token"), 14),
    Category("rwa", "RWA", ("real-world-assets-rwa",), 15),
    Category("depin", "DePIN", ("depin",), 16),
    Category("oracles", "Oracles", ("oracle",), 17),
    Category("gaming", "Gaming", ("gaming",), 18),
    Category("exchange", "Exchange Tokens", ("exchange-based-tokens",), 19),
    Category("privacy", "Privacy", ("privacy-coins",), 20),
    Category("socialfi", "SocialFi", ("socialfi",), 21),
    Category("btc_eco", "Bitcoin Ecosystem", ("bitcoin-ecosystem",), 22),
    Category("l2", "Layer 2", ("layer-2",), 23),
    Category("zk", "Zero Knowledge", ("zero-knowledge-zk",), 24),
    Category("modular", "Modular", ("modular-blockchain",), 25),
    Category("dex", "DEX", ("decentralized-exchange",), 26),
    Category("lending", "Lending", ("lending-borrowing",), 27),
    Category("defi", "DeFi", ("decentralized-finance-defi",), 28),
    Category("l1", "Layer 1", ("layer-1", "smart-contract-platform"), 29),
    Category("interop", "Interop / Layer 0", ("layer-0-l0", "interoperability", "cross-chain-communication"), 30),
    Category("payments_pow", "Payments / PoW", ("payment-solutions", "proof-of-work-pow"), 31),
    Category("infra", "Infrastructure", ("infrastructure", "identity", "name-service", "storage"), 32),
    Category("launchpad", "Launchpads", ("launchpad",), 33),
    Category("other", "Uncategorised", (), 99),
]

MAJORS = {"BTC", "ETH"}

# Tier 1: deepest coverage. Everything else is ranked into T2/T3 by volume.
TIER1 = ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "HYPE", "SUI", "LINK", "AVAX"]
TIER2_SIZE = 60

# Spot-only assets must clear this to enter the universe (perp-listed always qualify).
SPOT_MIN_VOLUME_USD = 5_000_000

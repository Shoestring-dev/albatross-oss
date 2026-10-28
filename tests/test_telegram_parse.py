"""Token addresses in Telegram messages (albatross/telegram/parse.py). Every address here is made up: base58 strings
built from hashes of test labels, and 0x patterns, never real tokens, wallets or chats."""

import pytest

from albatross.telegram import parse
from albatross.telegram.parse import Found, addresses

M1 = "B6qN8yBtYfxfF9JoTywSr4CZDaQZnDhRf4FKKWHbpump"
M2 = "8SPTAnmrZ7pfuMm2Z7Bt7fB9BuCTNDd7nA4vYWnTpump"
M3 = "AtG3g38wn6oE1AfmyTcDteZVtAhjkRphXPQqjTsgpump"
M4 = "2vnpgstwiLpGbu6bnHLLLcMYxYhpyAYo5hCahNP7U2B4"
POOL = "41BHJaMN3gDGbSmV3MPAzvhBR4JwBb1JXFt7wFGStRoP"
WALLET = "CAudNyXakXAhAe7nPEPqCRVa5nTJ8r1Qpbe6mKX2QpKg"
E1 = "0x" + "a1" * 20
E2 = "0x" + "b2" * 20
SIG = "57hAub5FD4wNGQaSjujPvdvbgQRzHTDJ6iJRTVGPwf8yA8mWKtyZmEwpmzR24RjypwZoJKUGo7Q9naF1L5dGPFBj"
TRON = "TE7Ut7NwLifP2XPdtz761z57MkEWMDNhuk"
WSOL = "So11111111111111111111111111111111111111112"
USDC_SOL = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def sol(*mints):
    return [Found("solana", m) for m in mints]


# ─── the address formats ────────────────────────────────────────────────────
def test_solana_mints_decode_to_32_bytes():
    assert all(parse.solana_mint(m) for m in (M1, M2, M3, M4, POOL, WALLET, WSOL, "1" * 32))
    assert not parse.solana_mint("z" * 44)              # 33 bytes
    assert not parse.solana_mint("2" * 32)              # 24 bytes
    assert not parse.solana_mint(TRON)                  # 25 bytes: a Tron address
    assert not parse.solana_mint(SIG)                   # a transaction signature: 64 bytes
    assert not parse.solana_mint("0" + M1[1:])          # 0 isn't base58


@pytest.mark.parametrize("text", [
    f"{M1}",
    f"CA: {M1}",
    f"CA:{M1}",
    f"ca - {M1} 🚀🚀",
    f"`{M1}`",
    f"**{M1}**",
    f"New gem just launched!\n\n$PEPE2\n\n{M1}\n\nLP burned, mint revoked. DYOR",
    f"({M1})",
    f"{M1}.",
    f"Contract address👉{M1}👈",
    f"ref_{M1}",                                         # glued to a referral code by an underscore
])
def test_a_bare_solana_mint_in_common_formats(text):
    assert addresses(text) == sol(M1)


def test_a_bare_evm_address_has_no_chain_and_is_lowercased():
    assert addresses(f"CA {E1}") == [Found("evm", E1)]
    mixed = "0x" + "A1b2" * 10
    assert addresses(f"Contract: {mixed}") == [Found("evm", mixed.lower())]


@pytest.mark.parametrize("text", [
    "gm everyone, sending it today",
    "price 0.000123 mc 45k liq 12k",
    f"tx {SIG}",                                         # a Solana signature
    "0x" + "ab" * 32,                                    # an EVM transaction hash: 64 hex digits
    f"tron {TRON}",
    "https://x.com/someone/status/1234567890123456789",
    "QmYwAPJzv5CZsnA625s3Xf2nemtYgPpHdWEz79ojWnPbdG",    # an IPFS hash: 34 bytes
    f"{M1}0",                                            # a base58 run inside a longer word
    f"x{E1}",
    "z" * 44,
    "",
    None,
])
def test_no_address(text):
    assert addresses(text) == []


# ─── links ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("url, want", [
    (f"https://pump.fun/coin/{M1}", Found("solana", M1)),
    (f"pump.fun/{M1}", Found("solana", M1)),
    (f"https://www.pump.fun/advanced/coin/{M1}?include-nsfw=true", Found("solana", M1)),
    (f"https://birdeye.so/token/{M1}?chain=solana", Found("solana", M1)),
    (f"https://birdeye.so/token/{E1}?chain=Arbitrum", Found("arbitrum", E1)),
    (f"https://birdeye.so/base/token/{E1}", Found("base", E1)),
    (f"https://gmgn.ai/sol/token/{M1}", Found("solana", M1)),
    (f"https://gmgn.ai/sol/token/aBc12XyZ_{M1}", Found("solana", M1)),     # a referral link
    (f"https://gmgn.ai/eth/token/{E1}", Found("ethereum", E1)),
    (f"https://gmgn.ai/bsc/token/{E1}", Found("bsc", E1)),
    (f"https://photon-sol.tinyastro.io/en/r/@someone/{M1}", Found("solana", M1)),
    (f"https://photon-base.tinyastro.io/en/r/@someone/{E1}", Found("base", E1)),
    (f"https://neo.bullx.io/terminal?chainId=1399811149&address={M1}", Found("solana", M1)),
    (f"https://neo.bullx.io/terminal?chainId=56&address={E1}", Found("bsc", E1)),
    (f"https://neo.bullx.io/terminal?chainId=8453&address={E1}", Found("base", E1)),
    (f"https://jup.ag/swap/SOL-{M1}", Found("solana", M1)),
    (f"https://jup.ag/swap?sell={WSOL}&buy={M1}", Found("solana", M1)),
    (f"https://jup.ag/tokens/{M1}", Found("solana", M1)),
    (f"https://raydium.io/swap/?inputMint=sol&outputMint={M1}", Found("solana", M1)),
    (f"https://www.geckoterminal.com/eth/tokens/{E1}", Found("ethereum", E1)),
    (f"https://solscan.io/token/{M1}", Found("solana", M1)),
    (f"https://basescan.org/token/{E1}", Found("base", E1)),
    (f"https://etherscan.io/token/{E1}", Found("ethereum", E1)),
    (f"https://bscscan.com/address/{E1}", Found("bsc", E1)),                # an address page: alone, it counts
    (f"https://t.me/solana_trojanbot?start=r-someone-{M1}", Found("solana", M1)),
    (f"https://t.me/SomeTradingBot?start={E1}", Found("evm", E1)),
    (f"https://dexscreener.com/solana/{M1}", Found("solana", M1)),
    (f"https://dexscreener.com/base/{E1}", Found("base", E1)),
    (f"https://dexscreener.com/ethereum/{E1}", Found("ethereum", E1)),
    (f"https://dexscreener.com/arbitrum/{E1}", Found("arbitrum", E1)),
    (f"https://dexscreener.com/bsc/{E1}", Found("bsc", E1)),
    (f"https://some-new-terminal.example/t/{M1}", Found("solana", M1)),    # a site we don't know: plain text
])
def test_links(url, want):
    assert addresses(f"Chart: {url} 🔥") == [want]


def test_a_link_names_the_chain_of_the_same_bare_address():
    assert addresses(f"CA: {E1}\nhttps://dexscreener.com/base/{E1}") == [Found("base", E1)]
    assert addresses(f"{E1.upper().replace('0X', '0x')} gmgn.ai/eth/token/{E1}") == [Found("ethereum", E1)]


def test_dexscreener_usually_names_the_pool_so_a_token_named_elsewhere_wins():
    msg = f"🚀 $PEPE2\nCA: {M1}\nChart: https://dexscreener.com/solana/{POOL}\nBuy: https://jup.ag/swap/SOL-{M1}"
    assert addresses(msg) == sol(M1)
    assert addresses(f"{E1}\nhttps://dexscreener.com/base/{E2}") == [Found("evm", E1)]


@pytest.mark.parametrize("url", [
    f"https://photon-sol.tinyastro.io/en/lp/{POOL}?handle=someone",
    f"https://www.dextools.io/app/en/solana/pair-explorer/{POOL}",
    f"https://www.geckoterminal.com/solana/pools/{POOL}",
])
def test_a_pool_is_not_a_token(url):
    assert addresses(f"chart {url}") == []
    assert addresses(f"chart {url} and CA {M1}") == sol(M1)
    assert addresses(f"{POOL} {url}") == []             # the link says what the bare string is


@pytest.mark.parametrize("url", [
    f"https://gmgn.ai/sol/address/{WALLET}",
    f"https://solscan.io/account/{WALLET}",
    f"https://birdeye.so/profile/{WALLET}?chain=solana",
    f"https://pump.fun/profile/{WALLET}",
])
def test_a_wallet_is_not_a_token(url):
    assert addresses(f"dev wallet {url}") == []
    msg = f"Dev: {WALLET}\n{url}\nCA: {M1}"
    assert addresses(msg) == sol(M1)                     # the same address bare is still the wallet


def test_hidden_links_and_buttons():
    # "Chart" and "Buy" are words linked to URLs the text doesn't show; the pool behind "Chart" is not the token.
    assert addresses("$CAT is live. Chart | Buy", [f"https://dexscreener.com/solana/{POOL}",
                                                    f"https://jup.ag/swap/SOL-{M1}"]) == sol(M1)
    assert addresses("Buy now", [f"https://t.me/SomeBot?start=x_{M2}"]) == sol(M2)
    assert addresses(f"CA {M1}", [f"https://pump.fun/coin/{M2}"]) == sol(M1, M2)          # in message order
    assert addresses("chart", [f"https://dexscreener.com/base/{E1}"]) == [Found("base", E1)]


def test_one_entry_per_token():
    assert addresses(f"{M1}\nhttps://pump.fun/coin/{M1}\n{M1}") == sol(M1)
    assert addresses(f"{E1} {E1.upper().replace('0X', '0x')}") == [Found("evm", E1)]


def test_several_tokens_in_order_up_to_the_limit():
    assert addresses(f"{M1} and {E1} and {M2}") == [Found("solana", M1), Found("evm", E1), Found("solana", M2)]
    assert parse.MAX_PER_MESSAGE == 3


def test_a_list_of_more_tokens_is_a_digest_not_a_call():
    trending = "🔥 Trending\n" + "\n".join(f"{i}. https://dexscreener.com/solana/{m}" for i, m in
                                          enumerate((M1, M2, M3, M4), 1))
    assert addresses(trending) == []
    assert addresses(f"{M1} {M2} {M3} {M4}") == []
    # what isn't a token doesn't count toward the limit
    assert addresses(f"{M1} {M2} {M3} {WSOL} {USDC_SOL}") == sol(M1, M2, M3)


@pytest.mark.parametrize("address", [
    WSOL, USDC_SOL, "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB", "11111111111111111111111111111111",
    "1nc1nerator11111111111111111111111111111111", "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "Sysvar1111111111111111111111111111111111111", "ComputeBudget111111111111111111111111111111",
    "0x0000000000000000000000000000000000000000", "0x000000000000000000000000000000000000dEaD",
    "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE", "0x0000000000000000000000000000000000001010",
    "0x4200000000000000000000000000000000000006", "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    "0xdAC17F958D2ee523a2206206994597C13D831ec7", "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",
    "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
])
def test_majors_stables_burn_and_system_addresses_are_not_calls(address):
    assert addresses(f"swap {address}") == []
    assert addresses(f"https://jup.ag/swap/{address}-{M1}") == sol(M1)


@pytest.mark.parametrize("url", [
    f"https://dexscreener.com/polygon/{E1}",
    f"https://gmgn.ai/tron/token/{E1}",
    f"https://www.geckoterminal.com/polygon_pos/tokens/{E1}",
    f"https://neo.bullx.io/terminal?chainId=137&address={E1}",
])
def test_a_chain_albatross_does_not_track(url):
    assert addresses(url) == []
    assert addresses(f"CA {E1} {url}") == []             # that address lives there


def test_a_base58_address_is_solana_whatever_the_path_says():
    assert addresses(f"https://dexscreener.com/base/{M1}") == sol(M1)


def test_a_real_world_shaped_call():
    msg = (f"💊 NEW CALL 💊\n\nToken: Some Cat (SCAT)\nCA: {M1}\n\nMC: 48K | Liq: 15K\n"
           f"📈 https://dexscreener.com/solana/{POOL}\n"
           f"🤖 Trojan: https://t.me/solana_trojanbot?start=r-caller-{M1}\n"
           f"👨‍💻 Dev: https://solscan.io/account/{WALLET}\n"
           f"Not financial advice. https://x.com/somecaller")
    assert addresses(msg) == sol(M1)

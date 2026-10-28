"""The Setup page's API: which keys are set, and saving, testing and removing them (albatross/keys.py stores them).

Every key the code reads that a user would set is listed in GROUPS, with what it turns on and where to get one. A key
set in the environment (.env, data.keys) is shown as such and can't be changed here. A saved secret is never sent
back: the page sees "set" and, for a long key, its last four characters. Where a provider has a cheap way to check a
key, Test (and every save) asks it and shows the answer in one line. The routes that connect accounts (the ChatGPT
and Claude plans, Telegram, X) live beside this one and share its guard (api/guard.py).
"""

import asyncio
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, ValidationError

from albatross import keys
from albatross.alerts.bot import hash_password, shared_bots
from albatross.api.guard import read_guard, write_guard
from albatross.config import settings
from albatross.http import client

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/setup")

Check = Callable[[str], Awaitable[tuple[bool, str]]]
CHECK_TIMEOUT_S = 12
DONE = "SETUP_DONE"


@dataclass(frozen=True)
class Item:
    name: str
    label: str
    help: str
    kind: str = "secret"            # secret | text | url | number | bool | choice | password | token
    link: str | None = None
    choices: tuple[str, ...] = ()
    advanced: bool = False
    check: Check | None = field(default=None, compare=False)


# ─── provider checks: each returns (ok, one plain line) and never puts the key in the line ─────────────────────────
def _why(e: Exception) -> str:
    # Never str(e): an httpx error's text includes the URL, and some providers take the key in the URL.
    return f"HTTP {e.response.status_code}" if isinstance(e, httpx.HTTPStatusError) else type(e).__name__


def _refused(r: httpx.Response, who: str) -> tuple[bool, str] | None:
    if r.status_code in (400, 401, 403):
        return False, f"{who} rejected this key (HTTP {r.status_code})."
    r.raise_for_status()
    return None


async def check_openrouter(key: str) -> tuple[bool, str]:
    r = await client().get("https://openrouter.ai/api/v1/key", headers={"Authorization": f"Bearer {key}"},
                           timeout=CHECK_TIMEOUT_S)
    if bad := _refused(r, "OpenRouter"):
        return bad
    d = (r.json() or {}).get("data") or {}
    usage, limit = d.get("usage"), d.get("limit")
    if usage is None:
        return True, "Key works."
    return True, f"Key works. ${usage:,.2f} used" + (f" of its ${limit:,.2f} limit." if limit else ", no limit set.")


async def check_telegram_bot(token: str) -> tuple[bool, str]:
    r = await client().get(f"https://api.telegram.org/bot{token}/getMe", timeout=CHECK_TIMEOUT_S)
    if r.status_code in (401, 404):
        return False, "Telegram rejected this token. Copy it again from @BotFather."
    r.raise_for_status()
    name = ((r.json() or {}).get("result") or {}).get("username") or ""
    if name.lower() in shared_bots():
        return False, f"@{name} is listed in TELEGRAM_SHARED_BOTS, so the alerts bot won't use it. Make a new bot."
    return True, f"Token works: @{name}. Send it /start, then the alerts password."


async def check_coingecko(key: str) -> tuple[bool, str]:
    pro = settings.coingecko_plan == "pro"
    r = await client().get(("https://pro-api.coingecko.com" if pro else "https://api.coingecko.com") + "/api/v3/ping",
                           headers={"x-cg-pro-api-key" if pro else "x-cg-demo-api-key": key}, timeout=CHECK_TIMEOUT_S)
    if r.status_code in (400, 401, 403):
        other = "demo" if pro else "pro"
        return False, f"CoinGecko rejected this key as a {settings.coingecko_plan} key. If it's a {other} key, change the plan."
    r.raise_for_status()
    return True, f"Key works ({settings.coingecko_plan} plan)."


async def check_coinalyze(key: str) -> tuple[bool, str]:
    r = await client().get("https://api.coinalyze.net/v1/exchanges", params={"api_key": key}, timeout=CHECK_TIMEOUT_S)
    return _refused(r, "Coinalyze") or (True, "Key works.")


async def check_fred(key: str) -> tuple[bool, str]:
    r = await client().get("https://api.stlouisfed.org/fred/series",
                           params={"series_id": "DGS10", "api_key": key, "file_type": "json"}, timeout=CHECK_TIMEOUT_S)
    return _refused(r, "FRED") or (True, "Key works.")


async def check_cryptopanic(key: str) -> tuple[bool, str]:
    plan = keys.value("CRYPTOPANIC_PLAN") or "growth"
    r = await client().get(f"https://cryptopanic.com/api/{plan}/v2/posts/",
                           params={"auth_token": key, "public": "true", "kind": "news"}, timeout=CHECK_TIMEOUT_S)
    if r.status_code == 403:
        return False, ("CryptoPanic refused the request (HTTP 403). The key or plan may be wrong, or CryptoPanic may "
                       "block this server's address.")
    return _refused(r, "CryptoPanic") or (True, f"Key works ({plan} plan).")


async def check_birdeye(key: str) -> tuple[bool, str]:
    r = await client().get("https://public-api.birdeye.so/defi/networks",
                           headers={"X-API-KEY": key, "accept": "application/json"}, timeout=CHECK_TIMEOUT_S)
    return _refused(r, "Birdeye") or (True, "Key works.")


async def check_jupiter(key: str) -> tuple[bool, str]:
    r = await client().get("https://api.jup.ag/price/v3", params={"ids": "So11111111111111111111111111111111111111112"},
                           headers={"x-api-key": key}, timeout=CHECK_TIMEOUT_S)
    return _refused(r, "Jupiter") or (True, "Key works.")


async def check_rpc(url: str) -> tuple[bool, str]:
    r = await client().post(url, json={"jsonrpc": "2.0", "id": 1, "method": "getHealth"}, timeout=CHECK_TIMEOUT_S)
    if r.status_code in (401, 403):
        return False, f"The RPC refused the request (HTTP {r.status_code}). Check the key in the URL."
    r.raise_for_status()
    body = r.json() or {}
    if body.get("result") == "ok":
        return True, "The RPC answers and says it's healthy."
    return False, f"The RPC answered but isn't healthy: {str((body.get('error') or {}).get('message') or body)[:120]}"


async def check_xai(key: str) -> tuple[bool, str]:
    try:
        from albatross.llm import xai
    except ImportError:
        return True, "Saved. This install can't test xAI keys."
    return await xai.check_key(key)


# ─── the catalog ────────────────────────────────────────────────────────────────────────────────────────────────
GROUPS: list[tuple[str, str, str, list[Item]]] = [
    ("ai", "AI agents", "The analysts, the Strategist, News Scout, the reports, the memo and memecoin research all run on a "
     "model. Use an OpenRouter key, your ChatGPT plan or your Claude plan. Without any of them the agents stay off and "
     "the rest of Albatross still works.", [
        Item("OPENROUTER_API_KEY", "OpenRouter key", "Runs every agent on its own model. Each agent has a daily dollar cap.",
             link="https://openrouter.ai/settings/keys", check=check_openrouter),
        Item("AGENTS_ON", "Run the agents on", "OpenRouter uses each agent's own model. A plan runs every agent on that "
             "plan and falls back to OpenRouter when the plan is out of allowance, if you've added a key.",
             kind="choice", choices=("openrouter", "chatgpt", "claude")),
        Item("CODEX_ENABLED", "Use my ChatGPT plan", "Sends agents to your ChatGPT plan through the Codex CLI.", kind="bool"),
        Item("CODEX_RESERVE_PCT", "Keep free for me (%)", "Share of the plan's weekly allowance the agents leave for you.",
             kind="number", advanced=True),
        Item("CLAUDE_ENABLED", "Use my Claude plan", "Sends agents to your Claude plan through Claude Code.", kind="bool"),
        Item("CLAUDE_MODEL", "Claude model", "Haiku uses the least of your plan; Opus the most.", kind="choice",
             choices=("haiku", "sonnet", "opus")),
        Item("ALERT_DAILY_SPEND_USD", "Spend alert ($)", "Raise an alert when today's OpenRouter spend passes this.",
             kind="number"),
        Item("TYPESAFE_API_KEY", "TypeSafe key", "TypeSafe AI's Jev model. Two agents use it; without it they're skipped.",
             advanced=True),
        Item("LLM_MODEL_OVERRIDES", "Model overrides", "Swap an agent's model: agent=model, comma-separated. "
             "docs/MODELS.md lists the agents.", kind="text", advanced=True),
    ]),
    ("x", "X and Grok", "Social Pulse and the memecoin X Scout read X. Use an xAI API key, or the Grok CLI signed in to "
     "a SuperGrok plan on the host worker.", [
        Item("XAI_API_KEY", "xAI key", "X search through the xAI API, paid per call. Capped by the daily limit below.",
             link="https://console.x.ai", check=check_xai),
        Item("X_SEARCH_VIA", "Read X through", "auto uses the API when a key is set, otherwise the Grok CLI.",
             kind="choice", choices=("auto", "api", "cli")),
        Item("XAI_DAILY_USD", "xAI daily cap ($)", "The most the X agents may spend on the xAI API in a day.", kind="number"),
        Item("SOCIAL_WEEKLY_USD", "Social Pulse weekly share ($)", "Grok CLI only: Social Pulse's share of your weekly "
             "SuperGrok pool, in the pool's dollar equivalent.", kind="number", advanced=True),
        Item("DEEP_X_WEEKLY_USD", "X Scout weekly share ($)", "Grok CLI only: the memecoin X Scout's share of the pool.",
             kind="number", advanced=True),
    ]),
    ("telegram", "Telegram", "Alerts come from your own bot. Calls come from the groups you read with your own account, "
     "or from your own program through the call feed.", [
        Item("TELEGRAM_ALERT_BOT_TOKEN", "Alerts bot token", "Make a bot with @BotFather and paste its token.",
             link="https://t.me/BotFather", check=check_telegram_bot),
        Item("TELEGRAM_ALERT_PASSWORD_HASH", "Alerts password", "Anyone who sends this password to your bot gets the "
             "alerts. Only a hash is stored.", kind="password"),
        Item("TELEGRAM_ALERT_CHAT_ID", "Extra alert chat", "Optional: a chat id that always gets the alerts.", kind="text",
             advanced=True),
        Item("TELEGRAM_API_ID", "API id", "From my.telegram.org, under API development tools.", kind="text",
             link="https://my.telegram.org/apps"),
        Item("TELEGRAM_API_HASH", "API hash", "From the same page as the API id.", link="https://my.telegram.org/apps"),
        Item("CALL_FEED_TOKEN", "Call feed token", "Your own program sends calls to POST /api/ingest/calls with this "
             "token. deploy/call-feed/README.md has the format.", kind="token"),
        Item("TELEGRAM_SHARED_BOTS", "Bots used elsewhere", "Bot usernames another program polls, comma-separated. The "
             "alerts bot refuses their tokens.", kind="text", advanced=True),
    ]),
    ("market", "Market data", "Prices, funding, open interest, macro and news. Most sources need no key; these add more.", [
        Item("COINGECKO_API_KEY", "CoinGecko key", "The free Demo key builds the coin list in about 2 minutes instead of 14.",
             link="https://www.coingecko.com/en/developers/dashboard", check=check_coingecko),
        Item("COINGECKO_PLAN", "CoinGecko plan", "demo for the free key, pro for a paid one.", kind="choice",
             choices=("demo", "pro")),
        Item("COINALYZE_API_KEY", "Coinalyze key", "Funding and open interest from Binance, Bybit and OKX. Free.",
             link="https://coinalyze.net/account/api-key/", check=check_coinalyze),
        Item("FRED_API_KEY", "FRED key", "US macro series for the macro analyst. Free.",
             link="https://fredaccount.stlouisfed.org/apikeys", check=check_fred),
        Item("SOSOVALUE_API_KEY", "SoSoValue key", "Spot ETF flows.", link="https://sosovalue.com/developer"),
        Item("CRYPTOPANIC_API_KEY", "CryptoPanic key", "News for the memecoin feed. A paid plan.",
             link="https://cryptopanic.com/developers/api/", check=check_cryptopanic),
        Item("CRYPTOPANIC_PLAN", "CryptoPanic plan", "The plan name in your API URL, e.g. developer or growth.",
             kind="text", advanced=True),
    ]),
    ("memecoins", "Memecoins", "Candles, token safety, holders and wallet history for the memecoin dossiers.", [
        Item("BIRDEYE_API_KEY", "Birdeye key", "Candles, token security and holders. A paid plan works best.",
             link="https://bds.birdeye.so", check=check_birdeye),
        Item("JUPITER_API_KEY", "Jupiter key", "Solana prices. Without it the slower public endpoint is used.",
             link="https://portal.jup.ag", check=check_jupiter),
        Item("SOLANA_RPC", "Solana RPC URL", "Wallet funding and holder lookups. Any Solana RPC; the key is usually in "
             "the URL.", kind="url", check=check_rpc),
        Item("CHAINSTACK_URL", "Chainstack URL", "A Chainstack Solana endpoint, used ahead of the RPC above.", kind="url",
             link="https://console.chainstack.com", check=check_rpc, advanced=True),
        Item("GITHUB_TOKEN", "GitHub token", "Raises GitHub's limit for repo metadata on dev-led tokens from 60 to 5,000 "
             "requests an hour. Needs no scopes.", link="https://github.com/settings/tokens", advanced=True),
        Item("FOMOAPI_KEY", "fomoapi.io key", "FOMO app traders through an unofficial relay. Read NOTICE.md first.",
             advanced=True),
    ]),
]
CATALOG: dict[str, Item] = {i.name: i for _, _, _, items in GROUPS for i in items}
SECRET_KINDS = {"secret", "url", "password", "token"}
_tests: dict[str, dict] = {}            # NAME -> the last test's answer, for this dashboard process's life


def ai_ready() -> bool:
    return bool(settings.openrouter_api_key or settings.codex_enabled or settings.claude_enabled)


def first_run() -> bool:
    return not keys.value(DONE) and not ai_ready()


def _hint(item: Item, v: str) -> str:
    if item.kind == "url":
        return f"set ({urlsplit(v).hostname or 'URL'})"
    if item.kind in ("password",):
        return "set"
    return f"…{v[-4:]}" if len(v) >= 16 else "set"


def _current(item: Item) -> str | None:
    """A non-secret's value in force: the saved or environment value, else the setting's default."""
    if (v := keys.value(item.name)) is not None:
        return v
    f = keys.field(item.name)
    d = getattr(settings, f) if f else None
    return None if d is None else ("true" if d is True else "false" if d is False else str(d))


def view(item: Item) -> dict:
    src, secret = keys.source(item.name), item.kind in SECRET_KINDS
    v = keys.value(item.name)
    return {
        "name": item.name, "label": item.label, "help": item.help, "kind": item.kind, "link": item.link,
        "choices": list(item.choices), "advanced": item.advanced, "state": src,
        "hint": _hint(item, v) if secret and v else None,
        "value": None if secret else _current(item),
        "can_test": item.check is not None, "test": _tests.get(item.name),
    }


async def run_check(item: Item) -> dict | None:
    if item.check is None or not (v := keys.value(item.name)):
        return None
    try:
        ok, msg = await asyncio.wait_for(item.check(v), CHECK_TIMEOUT_S + 3)
    except (httpx.HTTPError, TimeoutError, ValueError) as e:
        ok, msg = False, f"Couldn't reach the provider to check it ({_why(e)}). It's saved; try Test again later."
    _tests[item.name] = out = {"ok": ok, "message": msg, "at": time.time()}
    return out


# ─── routes ─────────────────────────────────────────────────────────────────────────────────────────────────────
@router.get("", dependencies=[Depends(read_guard)])
async def state() -> dict:
    await keys.refresh()                        # another process (the Telegram login) may have saved since
    return {
        "first_run": first_run(), "ai_ready": ai_ready(), "done": bool(keys.value(DONE)),
        "groups": [{"id": gid, "title": title, "blurb": blurb, "items": [view(i) for i in items]}
                   for gid, title, blurb, items in GROUPS],
    }


class Put(BaseModel):
    value: str | None = Field(default=None, max_length=4096)
    generate: bool = False


def _item(name: str) -> Item:
    if (item := CATALOG.get(name)) is None:
        raise HTTPException(404, f"Setup has no key called {name}.")
    if keys.from_env(name):
        raise HTTPException(409, f"{name} is set in the environment (.env or data.keys). Change it there.")
    return item


def _clean(item: Item, raw: str) -> str:
    v = raw.strip()
    if not v:
        raise HTTPException(422, "Enter a value, or use Remove to clear it.")
    if any(ord(c) < 32 for c in v):
        raise HTTPException(422, "That has a line break or control character in it. Paste it again on one line.")
    if item.kind == "number":
        try:
            if float(v) < 0:
                raise ValueError
        except ValueError:
            raise HTTPException(422, "Enter a number, 0 or more.") from None
    elif item.kind == "bool":
        if v.lower() not in ("true", "false"):
            raise HTTPException(422, "Choose on or off.")
        v = v.lower()
    elif item.kind == "choice":
        if v not in item.choices:
            raise HTTPException(422, f"Choose one of: {', '.join(item.choices)}.")
    elif item.kind == "url":
        if urlsplit(v).scheme not in ("http", "https") or not urlsplit(v).hostname:
            raise HTTPException(422, "Enter a full URL starting with https://.")
    elif item.kind == "password":
        if len(v) < 8:
            raise HTTPException(422, "Use at least 8 characters.")
        v = hash_password(v)
    try:
        keys.coerce(item.name, v)
    except ValidationError:
        raise HTTPException(422, "That isn't a valid value here.") from None
    return v


@router.put("/keys/{name}", dependencies=[Depends(write_guard)])
async def put_key(name: str, body: Put) -> dict:
    item, token = _item(name), None
    if body.generate:
        if item.kind != "token":
            raise HTTPException(422, "Only a token can be generated.")
        value = token = secrets.token_urlsafe(32)
    elif body.value is None:
        raise HTTPException(422, "Enter a value.")
    else:
        value = _clean(item, body.value)
    await keys.save(name, value)
    _tests.pop(name, None)
    log.info("setup: %s saved", name)
    await run_check(item)
    # A generated token is shown this once, so it can be copied into the program that sends calls.
    return {"item": view(item), "token": token}


@router.delete("/keys/{name}", dependencies=[Depends(write_guard)])
async def delete_key(name: str) -> dict:
    item = _item(name)
    await keys.delete(name)
    _tests.pop(name, None)
    log.info("setup: %s removed", name)
    return {"item": view(item)}


@router.post("/keys/{name}/test", dependencies=[Depends(write_guard)])
async def test_key(name: str) -> dict:
    if (item := CATALOG.get(name)) is None:
        raise HTTPException(404, f"Setup has no key called {name}.")
    if item.check is None:
        raise HTTPException(422, "There's no test for this one.")
    if not keys.value(name):
        raise HTTPException(422, "Nothing to test yet. Save a value first.")
    return {"item": view(item), "test": await run_check(item)}


class Done(BaseModel):
    done: bool = True


@router.post("/done", dependencies=[Depends(write_guard)])
async def mark_done(body: Done) -> dict:
    """The first-run page was dismissed: open on Home from now on (Setup stays under Ops)."""
    await (keys.save(DONE, "1") if body.done else keys.delete(DONE))
    return {"done": body.done}

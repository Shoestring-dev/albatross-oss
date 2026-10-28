"""TypeSafe AI's Jev, called directly (not through OpenRouter).

Jev is a "System One" decision model: it takes a state (text or JSON) and named typed questions (choice,
score, noul = yes/no) and returns typed answers with probabilities, no free text. $0.042 per million input
tokens, output free, ~0.1-0.3 s a call, 40 requests/s. Every call is logged to agent_runs like the
OpenRouter ones (model, tokens, cost, latency, the answers), under a per-agent daily budget.
"""

import asyncio
import hashlib
import logging
import time

import httpx
import orjson

from albatross import controls, db
from albatross.config import settings
from albatross.http import client
from albatross.llm.openrouter import BudgetExceeded, spent_today

log = logging.getLogger(__name__)

URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
USD_PER_INPUT_TOKEN = 0.042e-6          # output tokens are free
RETRY_STATUS = {429, 529}               # rate limited / overloaded: back off and retry
NO_CREDIT_WAIT = 900.0                  # after "no available TypeSafe API credits" (402), no call for this long
_no_credit_until = 0.0                  # one answer stops the rest: the scout asks once per coin, ~63 every 5 minutes


class OutOfCredit(BudgetExceeded):
    """TypeSafe answered that the organisation has no credits: callers stop as for a spent budget."""


def choice(instructions: str, options: dict[str, str]) -> dict:
    return {"type": "choice", "instructions": instructions, "criteria": options}


def noul(instructions: str, yes: str, no: str) -> dict:
    return {"type": "noul", "instructions": instructions, "criteria": {"true": yes, "false": no}}


async def decide(agent: str, state, questions: dict[str, dict], *, daily_budget_usd: float,
                 timeout_s: float = 30) -> tuple[dict, int]:
    """-> (answers keyed like `questions`, agent_run_id). Raises RuntimeError on failure, BudgetExceeded over budget."""
    if not settings.typesafe_api_key:
        raise RuntimeError("TYPESAFE_API_KEY is not set")
    global _no_credit_until
    if (wait := _no_credit_until - time.monotonic()) > 0:
        raise OutOfCredit(f"TypeSafe has no credits (it said so at its last call); next try in {wait / 60:.0f} min")
    if await controls.paused(agent):
        raise BudgetExceeded(f"{agent} is paused on the Output page")
    daily_budget_usd *= await controls.scale(agent)
    if (spent := await spent_today(agent)) >= daily_budget_usd:
        raise BudgetExceeded(f"{agent} spent ${spent:.4f} of ${daily_budget_usd:.2f} today")
    body = {"model": MODEL, "state": state, "questions": questions}
    input_hash = hashlib.sha256(orjson.dumps(body)).hexdigest()[:16]      # the Output page counts repeated inputs
    started, resp, error = time.monotonic(), None, None
    for attempt in range(4):
        try:
            r = await client().post(URL, json=body, timeout=timeout_s,
                                    headers={"Authorization": f"Bearer {settings.typesafe_api_key}"})
            if r.status_code in RETRY_STATUS and attempt < 3:
                await asyncio.sleep(0.5 * 2 ** attempt)
                continue
            r.raise_for_status()
            resp = r.json()
            break
        except httpx.HTTPStatusError as e:
            error = f"HTTP {e.response.status_code}: {e.response.text[:300]}"     # never the key: it's a header
            break
        except (httpx.HTTPError, ValueError) as e:
            error = type(e).__name__
            if attempt == 3:
                break
            await asyncio.sleep(0.5 * 2 ** attempt)
    answers = (resp or {}).get("answers") or {}
    if resp is not None and set(answers) != set(questions):
        error = f"answers for {sorted(answers)} but asked {sorted(questions)}"
    usage = (resp or {}).get("usage") or {}
    pool = await db.pool()
    run_id = await pool.fetchval(
        """INSERT INTO agent_runs (agent, model, latency_ms, input_tokens, output_tokens, cost_usd, ok, error, output,
                                   input_hash)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) RETURNING id""",
        agent, f"typesafe/{(resp or {}).get('model') or MODEL}", int((time.monotonic() - started) * 1000),
        usage.get("input_tokens"), usage.get("output_tokens"),
        (usage.get("input_tokens") or 0) * USD_PER_INPUT_TOKEN if usage else None,
        error is None, error, orjson.dumps(answers).decode() if answers else None, input_hash)
    if error is not None and error.startswith("HTTP 402"):
        _no_credit_until = time.monotonic() + NO_CREDIT_WAIT
        raise OutOfCredit(f"{agent} Jev call refused, TypeSafe is out of credit: {error[:160]}")
    if error is not None:
        raise RuntimeError(f"{agent} Jev call failed: {error}")
    return answers, run_id

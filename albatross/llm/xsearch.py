"""X search for Social Pulse and the Deep End's X Scout, through whichever lane you picked on the Setup page.

- "cli": the Grok Build CLI logged into a SuperGrok subscription on the host worker (`python -m albatross host`,
  llm/cli.py). Spend is the pool's dollar equivalent, in agent_runs.output.subscription_equiv_cost_usd.
- "api": the xAI API with your key, from the ingest container (llm/xai.py). Spend is real dollars, in
  agent_runs.cost_usd, at most settings.xai_daily_usd a day.
settings.x_search_via picks "cli", "api" or "auto" (the API when a key is set, else the CLI when its binary is here,
else off). x_lane() answers for this process. Each X job is scheduled in both processes and runs only where its lane is
(only_in): the API lane in ingest, the CLI lane on the host worker; the other process skips it and records nothing.
lane_now() is the answer as the dashboard and the alerts see it, from containers with no grok binary: there the CLI
lane counts as on while the host worker has made a Grok call in the last SEEN_FOR.

The agents pace their spend in pool dollars (settings.social_weekly_usd and DEEP_X_WEEKLY_USD out of
GROK_POOL_WEEKLY_USD). On the API, week() turns each into the same share of xai_daily_usd x 7, and spend_sql() reads
the lane's own column, so a week that changed lanes counts only the current lane's spend.
"""

import functools
import os
from datetime import UTC, date, datetime, timedelta

from pydantic import BaseModel

from albatross import db
from albatross.config import settings
from albatross.deepend.cfg import env_float
from albatross.llm import cli, xai

X_JOBS = frozenset({"social_pulse", "deep_x_scan", "deep_x_ask"})     # Social Pulse and the X Scout's two jobs
X_AGENTS = ["social_pulse", "deep_x_scan"]                            # their agent_runs names
POOL_USD = 40.0                     # 95% of a ~$42 SuperGrok week, as measured on one plan
SEEN_FOR = timedelta(days=7)


def via() -> str:
    v = (settings.x_search_via or "").strip().lower()
    return v if v in ("api", "cli") else "auto"


def grok_installed() -> bool:
    return os.path.isfile(cli.GROK) and os.access(cli.GROK, os.X_OK)


def x_lane() -> str:
    """This process's X search lane: "api" | "cli" | "off"."""
    key = bool((settings.xai_api_key or "").strip())
    match via():
        case "api":
            return "api" if key else "off"
        case "cli":
            return "cli" if grok_installed() else "off"
        case _:
            return "api" if key else "cli" if grok_installed() else "off"


def only_in(lane: str, job):
    """An X job as one process schedules it: run only while this process's lane is `lane`, else return 0 and record
    nothing (the job's health is the owning process's)."""
    @functools.wraps(job)
    async def run(*args, **kwargs):
        if x_lane() != lane:
            return 0
        return await job(*args, **kwargs)
    return run


async def structured[T: BaseModel](
    agent: str, schema: type[T], prompt: str, *, system: str, max_calls_per_day: int,
    max_calls_per_7d: int | None = None, timeout: float = 420, grok_args: list[str] | None = None,
    effort: str | None = None, from_date: date | None = None,
) -> tuple[T, int]:
    """One X-searched answer on this process's lane -> (parsed, agent_run_id). grok_args go to the CLI (where `system`
    is already one of them); effort and from_date to the API. CallLimitExceeded when no lane is on."""
    match x_lane():
        case "api":
            return await xai.x_structured(agent, schema, prompt, system=system, max_calls_per_day=max_calls_per_day,
                                          max_calls_per_7d=max_calls_per_7d, effort=effort or xai.EFFORT,
                                          from_date=from_date, timeout=timeout)
        case "cli":
            return await cli.grok_structured(agent, schema, prompt, max_calls_per_day=max_calls_per_day,
                                             max_calls_per_7d=max_calls_per_7d, timeout=timeout, extra_args=grok_args)
        case _:
            raise cli.CallLimitExceeded(f"{agent}: X search is off (no xAI API key, and no Grok CLI on this machine)")


# ─── pacing ──────────────────────────────────────────────────────────────────
def pool_week() -> float:
    return env_float("GROK_POOL_WEEKLY_USD", POOL_USD)


def week(pool_dollars: float) -> float:
    """An X allowance set in SuperGrok pool dollars, in this lane's dollars: as set on the CLI; on the API, the same
    share of the xAI week (xai_daily_usd x 7)."""
    if x_lane() != "api":
        return pool_dollars
    pool = pool_week()
    return pool_dollars / pool * settings.xai_daily_usd * 7 if pool > 0 else 0.0


def spend_sql() -> str:
    """One agent_runs row's X spend on this lane, as SQL. Each lane fills only its own column."""
    return "cost_usd" if x_lane() == "api" else "(output->>'subscription_equiv_cost_usd')::float"


# ─── as the dashboard and the alerts see it ──────────────────────────────────
async def host_seen_at() -> datetime | None:
    """When the host worker last made a Grok CLI call: the only trace it leaves in the database."""
    pool = await db.pool()
    return await pool.fetchval(
        "SELECT max(started_at) FROM agent_runs WHERE agent = ANY($1::text[]) AND model LIKE 'grok:%'", X_AGENTS)


def seen_lane(here: str, seen: datetime | None, now: datetime) -> str:
    """The lane from a process with no grok binary of its own: its own answer, or the CLI while the CLI may be chosen
    and the host worker has made a Grok call within SEEN_FOR."""
    if here != "off" or via() == "api":
        return here
    return "cli" if seen is not None and now - seen <= SEEN_FOR else "off"


async def lane_now() -> str:
    here = x_lane()
    return here if here != "off" else seen_lane(here, await host_seen_at(), datetime.now(UTC))


async def idle_jobs() -> frozenset[str]:
    """The X jobs while no lane is on (they skip on purpose: never late or failing), else none."""
    return X_JOBS if await lane_now() == "off" else frozenset()

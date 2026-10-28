"""Collects every Deep End module's jobs, schedules and alert cadences, so __main__.py and alerts/rules.py wire
them in one place and builders never edit those shared files (docs/SPEC-DEEP-END.md section 3)."""

import importlib
import logging

log = logging.getLogger(__name__)

MODULES = ["chain", "market", "xintel", "fit", "discovery", "dossier", "github", "fomo", "reddit", "wallets", "fomo_verdict"]

JOBS: dict = {}            # ingest container jobs: name -> async fn() -> int
SCHEDULE: list = []        # [(name, cron kwargs)]
HOST_JOBS: dict = {}       # host worker jobs (Grok / Claude CLIs)
HOST_SCHEDULE: list = []
CADENCE: dict = {}         # name -> seconds between runs (alerts: stale after 3)

for _name in MODULES:
    try:
        _m = importlib.import_module(f"albatross.deepend.{_name}")
    except Exception:                                    # a broken module must not take the scheduler down
        log.exception("deepend.%s failed to import; its jobs are skipped", _name)
        continue
    JOBS.update(getattr(_m, "JOBS", {}))
    SCHEDULE.extend(getattr(_m, "SCHEDULE", []))
    HOST_JOBS.update(getattr(_m, "HOST_JOBS", {}))
    HOST_SCHEDULE.extend(getattr(_m, "HOST_SCHEDULE", []))
    CADENCE.update(getattr(_m, "CADENCE", {}))

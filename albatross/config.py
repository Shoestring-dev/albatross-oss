from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    """All config comes from environment variables (or the project secrets file locally).

    Never log or print these values.
    """

    # Two optional key files, both gitignored: `.env` and `data.keys`. Blank values are ignored, so a blank
    # template line in `.env` never masks a key in `data.keys` (docker-compose.yml loads data.keys last for the
    # same reason). Keys saved on the Setup page fill in whatever these leave unset (albatross/keys.py).
    model_config = SettingsConfigDict(env_file=(ROOT / "data.keys", ROOT / ".env"), extra="ignore",
                                      env_ignore_empty=True)

    postgres_host: str = "127.0.0.1"
    postgres_port: int = 5432
    postgres_user: str = "albatross"
    postgres_password: str = "albatross_local"
    postgres_db: str = "albatross"

    openrouter_api_key: str | None = None
    # Per-agent model switch without a deploy (data.keys): "agent=model,agent=model", e.g.
    # "events_news=typesafe/jev-router". Unset agents keep their code default (DeepSeek V4.1 Flash).
    llm_model_overrides: str | None = None
    # A shadow Strategist on another model: same inputs every hour, its calls stored as source
    # "strategist@<model slug>" and graded like any source, feeding nothing (docs/MODELS.md).
    strategist_shadow_model: str | None = None
    # More shadows, comma-separated models. A shadow runs only on the
    # ChatGPT plan (skipped, never sent to OpenRouter) unless its model costs pennies there (Luna, capped at $2 a day:
    # openrouter.shadow_terms); every shadow stops before the regular agents do (llm/codex.py EXPERIMENT_SHARE) and
    # shares 2 of the gateway's 6 slots. Budgets are per shadow at list price (plan credits at $0.04 plus dollars).
    strategist_shadow_models: str | None = None
    strategist_shadow_budget_usd: float = 20.0
    # Analyst shadows: "agent=model,agent=model", e.g. "htf_analyst=openai/gpt-6.1-sol"; same inputs as the analyst's
    # own run (the 10% sample while it is paused), stored as source "<agent>@<model slug>", graded, feeding nothing.
    analyst_shadow_models: str | None = None
    analyst_shadow_budget_usd: float = 10.0
    # A ChatGPT subscription (albatross/llm/codex.py, deploy/codex_gateway.py): agents whose model is an OpenAI
    # model Codex serves are called through it first and fall back to OpenRouter. The pacer keeps codex_reserve_pct
    # of the weekly allowance free for the account holder (5% by default) and spreads the rest evenly over the week.
    codex_enabled: bool = False     # opt in with CODEX_ENABLED=true once deploy/codex_gateway.py is installed
    codex_gateway_sock: str = "/run/albatross-codex/gateway.sock"
    codex_reserve_pct: float = 5.0
    codex_burst_pct: float = 5.0
    # A Claude subscription through the Claude Code CLI, in the same gateway (deploy/subs, albatross/llm/claude.py).
    # The login stays inside the gateway container; this only turns the lane on and picks the model alias.
    claude_enabled: bool = False
    claude_model: str = "sonnet"        # haiku | sonnet | opus
    # Which lane runs the agents: "openrouter" (each agent's own model; OpenAI ones go through the ChatGPT plan first
    # when codex_enabled), "chatgpt" (every agent on the ChatGPT plan) or "claude" (every agent on the Claude plan).
    # The plans fall back to OpenRouter when they're out of allowance and an OpenRouter key is set.
    agents_on: str = "openrouter"
    # TypeSafe AI's Jev decision model, called directly (albatross/llm/typesafe.py; data.keys TYPESAFE_API_KEY).
    typesafe_api_key: str | None = None
    # Jev trade scout's paper book (albatross/agents/jev_scout.py): a candidate is taken when Jev's
    # probability, corrected against its own graded record (jev_calibration), and its expected R both clear
    # these, at most max_new a run and max_open at once. A 2R target breaks even at P 1/3 before costs, so
    # min_p is a floor just over break-even and min_ev is what binds (+0.2R needs P 0.40 even at zero cost).
    # On 2026-09-30 the corrected odds topped out near 0.22 (15m and 1h): honestly, nothing qualifies yet.
    # Social Pulse (Grok X search on a SuperGrok subscription pool): the week's spend in the pool's own dollar
    # equivalent. One plan's week measured about $42; measure your own pool before raising this.
    social_weekly_usd: float = 25.0     # with the Deep End's X Scout (DEEP_X_WEEKLY_USD, $15) the two stay inside ~95% of the pool
    jev_scout_min_p: float = 0.35
    jev_scout_min_ev: float = 0.2
    jev_scout_max_new: int = 3
    jev_scout_max_open: int = 20
    xai_api_key: str | None = None
    # X search for Social Pulse and the memecoin X Scout: "cli" (the Grok CLI on the host worker, a SuperGrok
    # subscription), "api" (the xAI API with xai_api_key, from the ingest container) or "auto" (the API when a key is
    # set, else the CLI).
    x_search_via: str = "auto"
    xai_model: str | None = None        # None: the default in albatross/llm/xai.py
    xai_daily_usd: float = 3.0          # the xAI API's daily dollar cap across the X agents
    coingecko_api_key: str | None = None
    coingecko_plan: str = "demo"  # demo | pro
    cryptopanic_api_key: str | None = None
    fred_api_key: str | None = None
    coinalyze_api_key: str | None = None
    sosovalue_api_key: str | None = None
    # Micro-cap / new-token lane (keys may be shared with a trading bot: stay well under their rate limits).
    jupiter_api_key: str | None = None
    solana_rpc: str | None = None
    chainstack_url: str | None = None
    # Bearer token an external call feed (a Telegram listener, a trading bot's ledger) presents to /api/ingest/calls.
    call_feed_token: str | None = None
    # Failure alerts (albatross/alerts) go through Albatross's own Telegram bot to every chat that sent its
    # password (bot.py); never a token another program uses, see the HARD RULE in albatross/alerts/notify.py.
    telegram_alert_bot_token: str | None = None
    # Website login (albatross/api/gate.py): the password as bot.hash_password() output and the key that
    # signs the 30-day device cookie; rotating the key signs every device out. Both live in data.keys.
    site_user: str = "admin"
    site_password_hash: str | None = None
    site_session_secret: str | None = None
    # With no site password, the Setup page only answers on these host names, so another website can't reach it
    # through your browser (DNS rebinding). Add a LAN name here, comma-separated, to use Setup from another device.
    allowed_hosts: str = "localhost,127.0.0.1,::1"
    telegram_alert_password_hash: str | None = None   # bot.hash_password() output; the password itself isn't stored
    telegram_alert_chat_id: str | None = None         # optional extra recipient
    # Bot usernames another program long-polls (comma-separated): the alerts bot refuses to poll these tokens.
    telegram_shared_bots: str = ""
    # The built-in Telegram listener (albatross/telegram): your own Telegram account reads the groups you pick and
    # logs each token address posted there as a call. api_id and api_hash come from my.telegram.org; the session is
    # written by the login on the Setup page and is as powerful as your login, so treat it like a password.
    telegram_api_id: str | None = None
    telegram_api_hash: str | None = None
    telegram_session: str | None = None
    telegram_watch: str = ""            # chat ids to read, comma-separated
    alert_daily_spend_usd: float = 27.0         # OpenRouter spend today that raises an alert (caps sum to ~$26: the Deep End's fit, takes and Reddit, and the Oracle since it left Claude)
    alert_dashboard_url: str = "http://localhost:8787/#/health"

    @property
    def database_dsn(self) -> str:
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


settings = Settings()

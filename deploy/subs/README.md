# Subscriptions gateway

Optional. It lets the agents run on your own ChatGPT plan (through the Codex CLI) or your own Claude plan (through
the Claude Code CLI). Without it, the agents run on OpenRouter as before.

## Start it

```bash
docker compose --profile subscriptions up -d
```

This builds the `subs` image and starts it with the other services. It has no network ports. Ingest and the
dashboard reach it over a Unix socket in the `subs_sock` volume. Your sign-ins are kept in the `subs_home` volume and
nowhere else.

## Sign in

Open Setup at http://localhost:8787.

- **ChatGPT.** Press sign in, open the link it shows and enter the code. If that doesn't work for your account, run
  `codex login` on a computer with a browser and paste the file `~/.codex/auth.json` on Setup instead.
- **Claude.** On any computer with Claude Code, run `claude setup-token`, sign in, and paste the token on Setup. The
  gateway makes one tiny call with it and keeps it only if that works.

Then switch the plan on and choose where the agents run (ChatGPT plan, Claude plan or OpenRouter).

## What a call does

Each agent call is one run of the CLI's headless mode. Every tool is off, the agent's own instructions are the system
prompt, and it runs in a new empty folder that is deleted afterwards. A Claude run gets only its sign-in token in its
environment, never `ANTHROPIC_API_KEY`, with telemetry, update checks and other nonessential traffic off.

When the plan runs out of allowance, the agents fall back to OpenRouter if you set an OpenRouter key. Without a key
they skip their run until the plan has room again. The Claude plan has no web search here, so the research agents
(the Oracle, its calendar, and the Deep End's fit and Reddit scouts) use the ChatGPT plan or OpenRouter.

## Sign out or remove it

Sign out on Setup. To remove the gateway and its sign-ins:

```bash
docker compose --profile subscriptions down
docker volume rm albatross_subs_home
```

## Updating the CLIs

Both CLI versions are pinned in `deploy/subs/Dockerfile`, and the gateway's lockdown depends on their flags. Before a
bump, check the flags (`claude --help`, `codex exec --help`) and run `tests/test_subs.py` and `tests/test_codex.py`.
Then rebuild with `docker compose --profile subscriptions up -d --build subs`.

On a server that uses `deploy/compose.server.yml`, the gateway runs on the host instead
(`deploy/install-codex-gateway.sh`). Run only one of the two.

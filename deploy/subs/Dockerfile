# The subscriptions gateway (deploy/codex_gateway.py): Albatross agents on your own ChatGPT plan (Codex CLI) or Claude
# plan (Claude Code CLI). Opt in with `docker compose --profile subscriptions up -d`; see deploy/subs/README.md.
# Built from ./deploy (docker-compose.yml); deploy/.dockerignore keeps everything but the gateway out of the context.
FROM node:24.22.1-trixie-slim

# python3 runs the gateway (stdlib only); tzdata lets it read a limit's reset time in the time zone Claude names.
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Pinned: the gateway's lockdown rests on these CLIs' flags. Recheck them before you bump either version.
RUN npm install -g --no-fund --no-audit @openai/codex@0.160.0 @anthropic-ai/claude-code@2.1.312 \
 && npm cache clean --force

# Logins live in the subs_home volume, the socket in subs_sock; Docker seeds each new volume with these owners.
RUN useradd --uid 10001 --create-home --home-dir /home/subs --shell /usr/sbin/nologin subs \
 && chmod 700 /home/subs \
 && install -d -o subs -g subs -m 750 /run/albatross-codex

COPY codex_gateway.py /opt/albatross-subs/codex_gateway.py

USER subs
WORKDIR /home/subs
ENV HOME=/home/subs \
    CODEX_GATEWAY_SOCK=/run/albatross-codex/gateway.sock \
    CODEX_GATEWAY_WORK=/home/subs/work/run \
    CODEX_BIN=/usr/local/bin/codex \
    CLAUDE_BIN=/usr/local/bin/claude \
    PYTHONUNBUFFERED=1

CMD ["python3", "/opt/albatross-subs/codex_gateway.py"]

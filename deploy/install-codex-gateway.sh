#!/bin/sh
# Install or update the Codex gateway on the server (run from the repo root with sudo). The gateway runs as the `codex`
# user, which can't read the repo, so the script is copied to /opt/albatross-codex. The socket directory is
# bind-mounted into the ingest container (deploy/compose.server.yml).
#
# On a new box, first (once):
#   sudo npm install -g @openai/codex@0.160.0               # the CLI, pinned (needs Node 18+)
# After any CLI upgrade, rerun the canary in docs/MODELS.md before trusting it: the safety rests on its flags.
#   sudo useradd --system --create-home --shell /usr/sbin/nologin codex && sudo chmod 700 /home/codex
#   ssh -t -L 1455:localhost:1455 <your-server> "sudo -u codex -H codex login"   # you sign in in your browser
# The ChatGPT login then lives in /home/codex/.codex, readable by that user only. Then set CODEX_ENABLED=true.
# Check your plan's terms first: a personal subscription is for your own use.
set -eu
WANT=0.160.0                 # the Codex CLI version the gateway's flags were checked on (2026-10-03)
HAVE=$(sudo -u codex -H sh -c 'cd /home/codex && codex --version' 2>/dev/null | awk '{print $NF}')
[ "$HAVE" = "$WANT" ] || { echo "codex CLI is ${HAVE:-missing}, the gateway was checked on $WANT: rerun the canary first" >&2; exit 1; }
install -d -o root -g root -m 755 /opt/albatross-codex
install -o root -g root -m 644 deploy/codex_gateway.py /opt/albatross-codex/codex_gateway.py
install -d -o codex -g codex -m 700 /home/codex/work
install -d -o codex -g codex -m 750 /var/lib/albatross-codex /var/lib/albatross-codex/sock
install -o root -g root -m 644 deploy/albatross-codex.service /etc/systemd/system/albatross-codex.service
systemctl daemon-reload
systemctl enable albatross-codex >/dev/null 2>&1
systemctl restart albatross-codex
sleep 2
systemctl is-active albatross-codex

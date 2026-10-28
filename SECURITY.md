# Security

Please report vulnerabilities privately through this repository's **Security** tab ("Report a vulnerability"),
not in a public issue. Include what you found, how to reproduce it, and what it exposes. We aim to reply within a
week.

## How Albatross is meant to be run

- Keys live in `.env` and `data.keys` (both gitignored), or in the database when saved on the Setup page
  (`app_settings`): treat database backups like the keys themselves. The code is written never to log or print a key,
  and error text is redacted before it is logged or stored. The Setup page never sends a saved key back.
- The Setup page answers only to a signed-in visitor when a site password is set, and otherwise only at localhost (or
  the names in `ALLOWED_HOSTS`), so another website can't reach it through your browser.
- The dashboard binds to 127.0.0.1. On a server it sits behind `deploy/compose.server.yml`: a Caddy gate that asks the
  app's `/auth/check` before every request, and a Cloudflare Tunnel, so no port is open to the internet. Changing a
  control needs the `X-Albatross-Action` header, which another site's page can't send.
- Model output is treated as untrusted. Agents get precomputed numbers, their claims must cite evidence the run
  actually saw, and nothing hosted on GitHub reaches a model.
- Subscription CLIs (Codex, Claude Code) run in their own container as a non-root user with every tool off, or as a
  separate locked-down user on a server; the Grok CLI runs in a sandbox outside the repo. Their logins never enter
  the app's containers or the database.
- The Telegram listener's saved session works like your Telegram login. Sign out on the Setup page, or end it in
  Telegram under Settings > Devices.
- Albatross never holds exchange or wallet keys and never places trades.

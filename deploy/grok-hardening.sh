#!/usr/bin/env bash
# Locks down the Grok Build CLI on the Albatross host. Idempotent; run as the user who runs grok.
#
# Why: nothing from this machine should leave it through the CLI. xAI's server config sends
# disable_codebase_upload=true but, when checked in 2026, still trace_upload_enabled=true. Albatross runs
# grok in an empty non-git dir with --sandbox strict (see albatross/llm/cli.py); this script adds
# the pieces that sandbox needs and pins every upload/telemetry switch off in grok's own config.
# Verify after a run:  grep trace.upload.decision ~/.grok/logs/unified.jsonl | tail -1
#   -> "uploads_enabled":false,"data_collection_disabled":true
set -euo pipefail

# 1. --sandbox strict needs bubblewrap, and Ubuntu 24.04 blocks unprivileged user namespaces
#    unless the binary has an AppArmor profile granting `userns`. Grant it to bwrap only.
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq bubblewrap >/dev/null
sudo tee /etc/apparmor.d/bwrap >/dev/null <<'PROFILE'
# Allow bubblewrap (used by Grok CLI --sandbox) to create user namespaces; nothing else gains the right.
abi <abi/4.0>,
include <tunables/global>

profile bwrap /usr/bin/bwrap flags=(unconfined) {
  userns,
  include if exists <local/bwrap>
}
PROFILE
sudo apparmor_parser -r /etc/apparmor.d/bwrap

# 2. Upload/telemetry switches off in grok's config (user config wins over remote settings).
CFG="$HOME/.grok/config.toml"
mkdir -p "$HOME/.grok"
touch "$CFG"
if ! grep -q "Albatross hardening" "$CFG"; then
  cat >> "$CFG" <<'TOML'

# Albatross hardening: nothing from this box goes to xAI except the prompt and search.
[telemetry]
trace_upload = false
otel_enabled = false

[features]
telemetry = "off"
codebase_indexing = false
feedback = false

[harness]
disable_workspace_teleport = true

[memory_v2]
capture_enabled = false
automatic_dream_enabled = false

[tools]
respect_gitignore = true
TOML
fi
echo "grok hardened: bwrap $(bwrap --version | cut -d' ' -f2), config pinned in $CFG"

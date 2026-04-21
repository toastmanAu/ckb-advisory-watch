# ckb-advisory-watch

Security advisory watcher for the Nervos CKB ecosystem.

Polls curated advisory sources (GHSA, OSV.dev, RustSec, PyPA advisory-db, npm audit, oss-security), maintains a SQLite component database of Nervos projects and their upstream dependencies (direct + transitive via lockfiles), and cross-references new advisories against that dep graph.

When a match is found it emits the same markdown report artifact to three surfaces:

- A note inside an Obsidian vault folder
- A page on [wyltekindustries.com](https://wyltekindustries.com) under `/advisories`
- A Telegram bot post (short alert + `.md` attached)

## Host

Runs as a systemd user service on an **Orange Pi Zero 3** (Armbian / Ubuntu 24.04 LTS, aarch64, 4 cores, 3.8 GiB RAM) on the home LAN at `192.168.68.121`. Pull-based polling only — no advisory source offers WebSocket streams, so 10–60 minute cadence with `ETag` / `If-Modified-Since` is the baseline. GitHub webhooks → agent HTTP endpoint is the low-latency upgrade path later.

## Architecture

```
┌──────────────────────────────────────────────┐
│  Python asyncio agent (single process)       │
├──────────────────────────────────────────────┤
│  1. Component DB (SQLite)                    │
│     - Nervos repos + metadata                │
│     - Direct deps from manifests             │
│     - Transitive deps from lockfiles         │
│  2. GitHub poller (daily)                    │
│     - commits/tags on tracked repos          │
│     - re-parse manifests on change           │
│  3. Advisory ingest (10–60 min poll)         │
│     - GHSA (REST + ETag)                     │
│     - OSV.dev (hourly bulk ZIP ~2MB)         │
│     - RustSec (git pull)                     │
│     - PyPA advisory-db (git pull)            │
│     - npm (covered by GHSA)                  │
│  4. Matching engine                          │
│     - Dep + version-range intersection       │
│     - CVSS severity normalisation            │
│     - Dedup across sources                   │
│  5. Output fan-out                           │
│     - write MD into vault target dir         │
│     - publish to wyltekindustries/advisories │
│     - Telegram bot.sendMessage + sendDocument│
└──────────────────────────────────────────────┘
```

## Layout

| Path | Purpose |
|---|---|
| `agent/` | Python source — asyncio main, pollers, matcher, outputs |
| `db/schema.sql` | SQLite schema |
| `systemd/` | User service unit for running on the Zero 3 |
| `config.example.toml` | Template config (copy → `config.toml`) |

## Phases

- **Phase 0** ✓ Infra: SSH key, repo, deps install, agent skeleton, SQLite schema
- **Phase 1** — Component DB: 75 projects seeded ✓; Cargo.lock parser ✓; npm / go.mod / pyproject parsers pending; GitHub walker pending
- **Phase 2** ✓ Advisory ingest (OSV): bulk-zip fetcher with If-None-Match caching, 5 ecosystems (crates.io, npm, PyPI, Go, Maven), per-ecosystem error isolation, asyncio poll loop wired into `main.py`. GHSA / RustSec / PyPA direct pollers as freshness upgrades later.
- **Phase 3** — Matching engine: version-range intersection, CVSS, dedup, manual allowlist
- **Phase 4** — Outputs: browser dashboard ✓ (read-only, share-to-email); Telegram pager ✓ (per-advisory, DM + channel); vault sync, wyltekindustries page pending

## Install (Zero 3)

```bash
sudo apt install sqlite3 python3-venv git
git clone https://github.com/toastmanAu/ckb-advisory-watch.git
cd ckb-advisory-watch
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
sqlite3 data/state.db < db/schema.sql
cp config.example.toml config.toml   # then edit
cp systemd/ckb-advisory-watch.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ckb-advisory-watch
```

## Publishing the public mirror

The mirror is an unlisted static HTML snapshot of the dashboard, refreshed
hourly to `advisories.wyltekindustries.com` via Cloudflare Pages.
Share buttons become `mailto:` links. Severity floor: `medium`+ by default.

### One-time setup (Pi)

1. **Install Node + Wrangler**
   ```bash
   sudo apt install -y nodejs npm
   sudo npm install -g wrangler
   ```

2. **Create Cloudflare project (via dashboard UI)**
   - Pages → Create a project → Direct Upload
   - Name: "ckb-advisories" (or whatever you set in `project_name`)

3. **Add custom domain (via dashboard UI)**
   - Inside the project: Custom domains → Add
   - Domain: advisories.wyltekindustries.com
   - Follow the CNAME prompt (DNS lands in your wyltekindustries zone)

4. **Issue API token (via dashboard UI)**
   - My Profile → API Tokens → Create Token → Custom token
   - Permissions:
     - Account → Cloudflare Pages → Edit
     - User → User Details → Read
   - Scope: single account

5. **Populate config.toml**
   ```toml
   [outputs.public_mirror]
   enabled = true
   api_token = "<token from step 4>"
   account_id = "<copy from dashboard sidebar>"
   ```

6. **Smoke-test a render locally (no deploy)**
   ```bash
   ~/.venv/bin/python -c "
   import sqlite3
   from pathlib import Path
   from agent.mirror.render import render_all
   conn = sqlite3.connect('file:data/state.db?mode=ro', uri=True)
   print(render_all(conn, Path('/tmp/mirror-smoke'), severity_floor=('critical','high','medium')))
   "
   ls /tmp/mirror-smoke   # expect index.html, p/, a/, static/
   ```

7. **Full end-to-end (runs wrangler)**
   ```bash
   ~/.venv/bin/python -m agent.mirror --config config.toml
   ```

8. **Install the hourly timer**
   ```bash
   cp systemd/ckb-mirror.service systemd/ckb-mirror.timer ~/.config/systemd/user/
   systemctl --user daemon-reload
   systemctl --user enable --now ckb-mirror.timer
   systemctl --user list-timers ckb-mirror.timer
   ```

### Verifying the deploy

After step 7 or after the first timer fire:

- `journalctl --user -u ckb-mirror.service -n 50` — look for "mirror tick complete"
- `curl -s -o /dev/null -w '%{http_code}\n' https://advisories.wyltekindustries.com/` — expect `200`
- Click a 📤 link on an advisory page — your mail client should open with
  the subject pre-filled.

### Disabling

Flip `[outputs.public_mirror].enabled = false` in `config.toml`. The timer
will keep firing but the CLI logs "disabled" and exits 0 immediately.
Cloudflare Pages retains the last successful deploy until you delete the
project — no TTL on static uploads.

## Open the dashboard

Once the service is running:

```
http://<host-or-ip>:8080/
```

URL structure:
- `/` — landing (glance + triage + exploration)
- `/p/<owner>/<repo>` — per-project matches
- `/a/<source-id>` — per-advisory affected projects

Share buttons on match rows and advisory pages send a structured email
via Gmail SMTP to the address in `[share].recipient` — configure
`smtp_user` and `smtp_password` (app password) in `config.toml`.

## Wire the pager (Telegram)

1. Create a bot via [@BotFather](https://t.me/BotFather) → `/newbot` → save the HTTP API token.
2. Start a chat with your new bot (`/start`) — this is your DM destination.
3. Run a throwaway `curl` to get your DM chat_id:
   ```bash
   curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | python3 -m json.tool | grep '"id"'
   ```
   Look for the first `id` inside `"chat": { ... }`. That's your `chat_id`.
4. (Optional) To also post to a private channel, add the bot as admin,
   then have someone post `/start@your_bot_name` in the channel. Re-run
   step 3 and find the `id` whose value is a large negative integer
   starting with `-100`. That's the `channel_id`.
5. Edit `config.toml`:
   ```toml
   [outputs.telegram]
   enabled      = true
   bot_token    = "<bot-token>"
   chat_id      = "<your DM id>"
   channel_id   = "<channel id, optional>"
   min_severity = "medium"
   ```
6. `systemctl --user restart ckb-advisory-watch` (or just re-run
   `python -m agent.main` locally).

First run silently *baselines* the current backlog — you won't get 250
notifications. Only genuinely new advisories after that point fire pings.

## License

MIT — see [LICENSE](./LICENSE).

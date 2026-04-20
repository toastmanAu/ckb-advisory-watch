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
- **Phase 4** — Outputs: browser dashboard ✓ (read-only, share-to-email); Telegram bot, vault sync, wyltekindustries page pending

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

## License

MIT — see [LICENSE](./LICENSE).

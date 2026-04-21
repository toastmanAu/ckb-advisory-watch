# Public static mirror — design

**Status:** design approved, awaiting plan
**Date:** 2026-04-21
**Author:** Phill (brainstormed with Claude)

## 1. Goal

Generate a static HTML snapshot of the private dashboard and publish it to
`advisories.wyltekindustries.com` on Cloudflare Pages. Cadence: hourly.
Severity floor: medium+. Share buttons become `mailto:` links. Unlisted
(no nav link on `wyltekindustries.com`), selectively shareable by URL.

## 2. Non-goals

- **Link in main-site navigation.** Page exists but is unadvertised; shared
  by URL only.
- **Cloudflare Access / auth gating.** Deferred to a follow-up. v0 relies on
  URL-obscurity.
- **Live data.** Mirror is a snapshot refreshed hourly; dashboard is the
  live surface.
- **Server-side share actions.** No POST endpoints on the mirror. SMTP
  credentials never travel to the public side.
- **Filter projects.** All 75 tracked projects appear (subject to the
  severity floor excluding their low/unknown-only matches).
- **Mirror the telegram poll state, email share templates, or any other
  private surface.** Only the read-only dashboard pages ship.

## 3. Architecture

One new module in the agent, `agent/mirror/`, plus an external hourly cron
that calls a new `python -m agent.mirror` CLI entry point.

```
  Pi (192.168.68.121)
  ┌────────────────────────────────────────────┐
  │                                            │
  │  agent/mirror/__main__.py  (CLI)           │
  │    │                                       │
  │    ├─ renders static HTML via Jinja2       │
  │    │  (re-uses agent/dashboard templates)  │
  │    │                                       │
  │    ├─ writes to  /tmp/mirror-out/          │
  │    │                                       │
  │    └─ wrangler pages deploy                │
  │         --project-name=ckb-advisories      │
  │         /tmp/mirror-out/                   │
  │                                            │
  │  (hourly cron: systemd timer OR crontab)   │
  └────────────────────────────────────────────┘
                     │
                     │ HTTPS upload
                     ▼
         Cloudflare Pages (ckb-advisories)
                     │
                     ▼
       advisories.wyltekindustries.com
```

Why NOT in-process on the agent: the mirror is infrequent (hourly) and
benefits from running as a one-shot that exits, so we avoid bloating the
agent's memory footprint with a Wrangler subprocess. A systemd timer
invoking `python -m agent.mirror` is simpler than scheduling inside the
asyncio loop — plus the cron failure mode (non-zero exit) is visible in
journalctl.

### 3.1 Module layout

```
agent/mirror/
  __init__.py
  __main__.py           CLI entrypoint: parses config, runs render + deploy
  render.py             render_all(conn, out_dir, min_severity) → int (pages written)
  deploy.py             deploy_via_wrangler(out_dir, project_name, api_token) → None
  templates/
    (none — reuses agent/dashboard/templates/ directly)
```

### 3.2 Rendering

Uses `agent/dashboard/server.py`'s Jinja env + existing templates
(`base.html`, `index.html`, `project.html`, `advisory.html`). The same
`queries.landing_data` / `project_context` / `advisory_context` helpers
supply the data. One subtle change: the **severity floor** is applied
everywhere before rendering (see §4).

Routes rendered, one HTML file per URL:

| URL on mirror | File in out_dir |
|---|---|
| `/` | `index.html` |
| `/p/<owner>/<repo>/` | `p/<owner>/<repo>/index.html` |
| `/a/<source-id>/` | `a/<source-id>/index.html` |

Trailing slashes + `index.html` pattern is Cloudflare Pages' default for
clean URLs without `.html` in the address bar.

Logo + favicon copied verbatim from `agent/dashboard/static/`.

### 3.3 Share button rewrite

Private dashboard renders share buttons as `<form POST>` to
`/share/match/<id>`. The mirror's version of the same templates must
instead emit `mailto:` links. Implementation options:

- Pass a `mirror: bool` context variable through every template render;
  templates branch on it.
- OR: post-process the rendered HTML, regex-replace `<form POST>` blocks
  with `<a href="mailto:…">` equivalents.

**Pick option 1** (template-native) because it keeps HTML well-formed —
regex HTML surgery is fragile.

New helper in `agent/mirror/render.py`:

```python
def mailto_href(advisory, match=None) -> str:
    """Build mailto:?subject=...&body=... URL-encoded for one match row
    or an advisory-wide share (when match is None)."""
```

Template dispatch:

```jinja
{% if mirror %}
  <a class="share" href="{{ mailto_href(advisory, m) }}">📤 forward</a>
{% else %}
  <form method="POST" action="/share/match/{{ m.match_id }}" style="margin:0">
    <button type="submit" class="share sm">📤</button>
  </form>
{% endif %}
```

### 3.4 Severity floor

Only matches with `advisory.severity IN ('medium','high','critical')`
appear on the mirror. Implementation: the `render_all` entry point calls
the existing query helpers with a severity filter, then passes the
filtered results to the templates unchanged.

- Landing KPI tiles still show all five counts — but the triage/sidebar
  lists are drawn from the floor-filtered data.
- Per-project pages list only floor-qualifying matches.
- Per-advisory pages render only if the advisory itself is floor-qualifying
  (a `low` advisory is simply not mirrored — no stub file, 404 on public).

Concretely, `queries.landing_data` and friends already take a
`triage_severities` tuple; we pass `("critical","high","medium")`. A new
parameter on `project_context` and `advisory_context` filters similarly.

### 3.5 Deploy via Wrangler

Prerequisites on the Pi:

```bash
# One-time install:
sudo apt install nodejs npm  # (already present on most Armbian builds)
npm install -g wrangler
wrangler --version  # sanity
```

API token: Cloudflare dashboard → My Profile → API Tokens → Create
Token → "Custom token" → permissions:
- Account → Cloudflare Pages → Edit
- User → User Details → Read

Scope to a single account (the one hosting wyltekindustries). Saved into
`config.toml`:

```toml
[outputs.public_mirror]
enabled = true
project_name = "ckb-advisories"
api_token = "<cloudflare-token>"
account_id = "<cf-account-id>"
min_severity = "medium"
# Where to stage generated HTML before deploy. /tmp is fine — deploy is
# a single command that uploads+forgets.
out_dir = "/tmp/mirror-out"
```

Deploy command the mirror module runs:

```bash
CLOUDFLARE_API_TOKEN=<token> \
CLOUDFLARE_ACCOUNT_ID=<account> \
wrangler pages deploy /tmp/mirror-out \
  --project-name=ckb-advisories \
  --branch=main \
  --commit-dirty=true
```

`--branch=main` makes this the production deployment (vs preview
deployments from other branches). `--commit-dirty=true` suppresses
Wrangler's warning about an uncommitted working tree (we're not in a git
repo for the output dir anyway).

### 3.6 Hourly cron

A systemd user timer calls `python -m agent.mirror`:

```ini
# ~/.config/systemd/user/ckb-mirror.service
[Unit]
Description=CKB advisory public mirror — render and deploy
After=ckb-advisory-watch.service

[Service]
Type=oneshot
WorkingDirectory=%h/ckb-advisory-watch
ExecStart=%h/ckb-advisory-watch/.venv/bin/python -m agent.mirror \
  --config %h/ckb-advisory-watch/config.toml
```

```ini
# ~/.config/systemd/user/ckb-mirror.timer
[Unit]
Description=Render + deploy public advisory mirror hourly

[Timer]
OnBootSec=5min
OnUnitActiveSec=1h
Unit=ckb-mirror.service
Persistent=true

[Install]
WantedBy=default.target
```

Install once:
```bash
systemctl --user enable --now ckb-mirror.timer
```

## 4. Error handling

| Condition | Response |
|---|---|
| `[outputs.public_mirror].enabled=false` | CLI exits 0 with log "disabled". |
| `api_token` empty | Exit 2 with clear error. |
| `wrangler` not on PATH | Exit 2 with "install wrangler: npm install -g wrangler". |
| Render phase fails (template error, DB locked) | Exit 1, log traceback. Timer retries hourly. |
| Deploy phase fails (network, 4xx from CF API) | Exit 1, log full wrangler stderr. Timer retries hourly. |
| DB locked during render | `busy_timeout=10000` on the reader conn + retry once after a 5s sleep. |

Timer's `Persistent=true` ensures that if the Pi was offline at a
scheduled run, systemd fires one catch-up execution on next boot.

## 5. Privacy / leakage checklist

Things that MUST NOT land in the mirror:

- `config.toml` — secrets. Verified by not being copied into `out_dir`.
- `bot_token` / `smtp_password` / `api_token` — none of these are ever
  referenced by the dashboard templates, so they can't leak via rendering.
- `advisory.raw_json` — large, potentially includes internal notes? No,
  these are upstream OSV records, public information. But they're not
  rendered by any template, so moot.
- Match `ack_note` — not rendered by templates either. v0 dashboard
  doesn't expose ack notes. Verify this explicitly in a render test.

Actions:

1. Verify generated `out_dir` contains only `.html` + `.png` files. No
   `.toml`, `.db`, `.log`, or source code.
2. Grep the full output for any string that looks like a secret pattern:
   `bot_token`, `smtp_password`, `api_token`, `ghp_`, `gho_`, `CLOUDFLARE_`.
3. Scan for `chat_id`: your DM `1790655432` must NOT appear.

These run as a post-render verification step; if any trip, deploy aborts.

## 6. Config additions

```toml
[outputs.public_mirror]
enabled = false                      # opt in
project_name = "ckb-advisories"
api_token = ""                       # Cloudflare API token, Pages:Edit scope
account_id = ""                      # Cloudflare account ID (for wrangler env)
min_severity = "medium"              # floor for what appears publicly
out_dir = "/tmp/mirror-out"
# Optional base URL override — only used in rendered <link rel="canonical">
# tags and footer. Default inferred from project_name.
base_url = "https://advisories.wyltekindustries.com"
```

No migration required; the `[outputs.public_mirror]` section is optional
and defaults all keys to safe no-ops.

## 7. Testing

### 7.1 Unit

- `mailto_href(advisory, match)` — verify URL-encoding of subject + body,
  covers special chars in advisory summary (quotes, &, newlines), length
  cap (~2000 chars max per RFC).
- `render_all(conn, out_dir, min_severity="medium")` — seeded DB with a
  mix of severities; assert the generated tree contains index.html + one
  project subdir + one advisory subdir for the critical; assert low/
  unknown advisory pages are NOT generated.
- `deploy_via_wrangler` — mock `subprocess.run` via `unittest.mock`; assert
  correct argv, env, and error surfaces.

### 7.2 Integration

- Run the CLI against a seeded test DB, verify the output directory
  structure matches expected files, verify all emitted HTML parses as
  valid via `html.parser.HTMLParser`, verify no POST forms leak into the
  mirrored pages.
- Secret-scan regression: construct a mirror render with a test advisory
  containing no secret strings; grep the full `out_dir` for known-bad
  tokens; assert zero matches.

### 7.3 Live smoke

On driveThree (not Pi — avoids disturbing running service):
1. Create a Cloudflare Pages project `ckb-advisories-staging` via dashboard.
2. Generate API token scoped to that project.
3. Run `python -m agent.mirror --config /tmp/staging.toml` against a
   local DB copy.
4. Verify deploy succeeds, pages load at `<project>.pages.dev`, match
   rows look right, mailto links compose correctly in a real mail client.
5. Tear down the staging project.

## 8. Code paths touched

**New files:**
```
agent/mirror/__init__.py
agent/mirror/__main__.py
agent/mirror/render.py
agent/mirror/deploy.py
agent/mirror/templates/               # optional — override dashboard templates if mirror-specific changes needed
tests/test_mirror_render.py
tests/test_mirror_deploy.py
systemd/ckb-mirror.service           # user unit file
systemd/ckb-mirror.timer
```

**Modified:**
```
agent/dashboard/queries.py            # optional: add severity_floor param to project_context / advisory_context
agent/dashboard/templates/*.html      # add {% if mirror %} branches for share buttons
config.example.toml                   # [outputs.public_mirror] section
README.md                             # "Publishing the mirror" section
```

## 9. Rollout sequence

1. Template additions: add `mirror` context flag + mailto-replacement branches
   to the three dashboard templates. Unit tests green against live private
   dashboard (flag defaults False, nothing visible changes). Commit.
2. `mailto_href()` helper + unit tests. Commit.
3. `render.py::render_all()` — walks the URL structure, writes files to
   `out_dir`, copies static assets. Unit + integration tests. Commit.
4. `deploy.py::deploy_via_wrangler()` — subprocess wrapper + unit tests
   with mocked `subprocess.run`. Commit.
5. `__main__.py` CLI — config loading, enabled check, render → deploy,
   secret scan. Commit.
6. Systemd unit + timer + README update. Commit.
7. **Manual** (user-side, not code):
   - Create Cloudflare Pages project `ckb-advisories`
   - Issue API token
   - Set DNS CNAME for `advisories.wyltekindustries.com`
   - Populate `config.toml` on Pi
   - `systemctl --user enable --now ckb-mirror.timer`
   - Confirm first deploy lands

## 10. Open for plan, not design

- Whether to render per-project pages for projects with zero
  floor-qualifying matches — probably yes (keeps URL structure predictable),
  page will show "no current exposure" and hide share buttons. Plan time.
- Whether to include sidemark/canonical pointing back to the source repo
  (github.com/toastmanAu/ckb-advisory-watch) — nice to have, cheap. Plan time.
- Favicon differentiation (e.g., mirror uses a slightly different shield
  color so browser tabs are distinguishable from the private dashboard) —
  polish, defer.

## 11. Out of scope — separate plans later

- Cloudflare Access gating (allowlist emails for a proper "few people" auth)
- Per-project permalinks on the mirror that match the private dashboard's
  exact shape for easy URL mirroring (already the case per §3.2)
- Commit history → "match appeared / match disappeared" diffs as a feed
- RSS / Atom for match changes
- Archive of past snapshots (S3 lifecycle-managed)
- Opt-in projects: a way for a CKB dev to request their repo be added to
  the mirror's seed list directly

# ckb-advisory-watch dashboard — design

**Status:** design approved, awaiting plan
**Date:** 2026-04-20
**Author:** Phill (brainstormed with Claude)

## 1. Goal

A browser-accessible dashboard that turns the 500+ matches already in the
SQLite database into a surface a human can actually act on. Primary
audience: Phill + CKB developers he pings with specific findings (role B).
Secondary path left open: public mirror at `wyltekindustries.com/advisories`
(role C), enabled by URL-addressable pages.

The dashboard replaces "grep the database" as the consumption surface. It
is the third output channel alongside the existing poll loops and the
future Telegram bot.

## 2. Non-goals

- **Auth / gating.** LAN-only on the Pi. Public mirror is a static copy
  without share buttons; no login needed anywhere.
- **Real-time updates.** No WebSockets, no SSE, no polling JS. Every page
  is rendered per-request from live SQLite. Users refresh the browser.
- **Match state mutation.** The dashboard is read-only against `match`.
  Ack/suppress workflows live outside it (direct CLI / SQL for v0).
- **Full-text search.** URL-param filters by project/advisory/severity only.
  Search can be added later if it's missed.
- **SPA / JavaScript.** No JS frameworks, no client-side state. Forms POST,
  responses are HTML + 303 redirects. Works in a 1366×768 kiosk browser
  with zero polish.
- **Background refresh cache.** Per-request SQLite queries are fast enough
  at our data volume (566 matches, 250k advisories, ~10ms query time).

## 3. Audience & primary use cases

**Phill, morning:** open dashboard, glance the severity tiles, see if
anything new landed overnight, move on.

**Phill, triage session:** open dashboard, scan the critical+high list, click
into the one advisory that matters, hit "share" → email lands in Gmail with
a structured summary ready to forward to the upstream maintainer.

**CKB dev receiving a link:** Phill sends `http://wyltek.../a/GHSA-xxx` in a
Telegram DM. They click, see the advisory + which of their repos are hit +
fix version. No auth friction. No context loss.

## 4. Architecture

**Approach 2** — dynamic Jinja rendering in the existing agent process.

```
  ┌────────────────────────────────────────────────────┐
  │ agent/main.py (existing asyncio loop)              │
  │                                                    │
  │   ┌──────────────────┐  ┌──────────────────┐       │
  │   │ osv_poll_loop    │  │ github_poll_loop │       │
  │   └────────┬─────────┘  └────────┬─────────┘       │
  │            │                     │                 │
  │            └──── write ──────────┘                 │
  │                    │                               │
  │              ┌─────▼────┐              	       │
  │              │  SQLite   │ ← read ─┐  	       │
  │              │ state.db  │          │              │
  │              └───────────┘          │              │
  │                                     │              │
  │   ┌─────────────────────────────────┴─┐            │
  │   │ aiohttp.web.Application           │            │
  │   │   routes:                         │            │
  │   │     GET  /                        │            │
  │   │     GET  /p/<owner>/<repo>        │            │
  │   │     GET  /a/<source-id>           │            │
  │   │     POST /share/match/<id>        │            │
  │   │     POST /share/advisory/<id>     │            │
  │   │   renderer: Jinja2 templates      │            │
  │   │   share   : stdlib smtplib → Gmail│  	       │
  │   └───────────────────────────────────┘            │
  └────────────────────────────────────────────────────┘
                    ▲
                    │  :8080 (LAN)
            browser on any machine
```

### 4.1 Process model

One Python process. The aiohttp server shares the asyncio event loop with
the existing poll loops. No separate systemd unit.

The single SQLite connection currently held by the poll loops is **not**
reused for the web handlers — web handlers open their own read-only
connection per-request (`sqlite3.connect("file:…?mode=ro", uri=True)`). WAL
mode on the writer side means readers never block writers. This isolates
the web path from walker/matcher transaction windows.

### 4.2 Request lifecycle

```
GET /
  handler: index_view(request)
    → queries.landing_data(conn) returns:
        { kpis: {critical, high, medium, low, unknown}
        , triage: [match_row, ...]  # top N critical+high sorted
        , top_projects: [(slug, count), ...]
        , top_advisories: [(source_id, count), ...]
        , last_osv_ingest: timestamp
        , last_github_walk: timestamp
        }
    → template.render("index.html", **context)
    → aiohttp.web.Response(text=html, content_type="text/html")

POST /share/advisory/<source-id>
  handler: share_advisory_view(request)
    → queries.advisory_context(conn, source_id) returns full advisory + list of matches
    → share.build_email(ctx, kind="advisory") returns EmailPayload
    → share.send(payload)  # stdlib smtplib.SMTP_SSL, Gmail
    → aiohttp.web.HTTPSeeOther("/a/<source-id>?sent=1")
  on SMTP failure:
    → log.error, redirect with ?sent_error=1
```

### 4.3 Static asset strategy

CSS inlined in `base.html`. No external stylesheet fetch = one HTTP round
trip per page load. Fonts are system (`Inter`, `JetBrains Mono`,
fallback stack). Favicon is a single 32×32 PNG served from
`/static/favicon.png` by aiohttp's `web.FileResponse`.

### 4.4 Listener binding

Bind to `0.0.0.0:8080` by default (configurable). Pi's firewall is
permissive on the LAN; no HTTPS termination at this layer (public mirror
is static and served by Cloudflare/whatever).

## 5. Page spec

### 5.1 `GET /` — landing

Target: 1366×768 without scroll for the above-the-fold zones.

**Top strip (≈48px):**
- 32px gradient logo tile (placeholder until final 7007/7010 is exported)
- Service name + version
- Process health indicator (green dot if agent running, red if last_matched > 2× poll interval)
- Right-aligned timestamps: "osv ingest: 38s ago" and "walker: 18m ago"
- Font: Inter 600 14px for name, JetBrains Mono 11px uppercase for meta

**KPI strip (≈72px):**
- 5 tiles in a row: critical / high / medium / low / unknown
- Tile = 3px left-border in severity color + 24px JetBrains Mono number +
  10px uppercase label
- Colors: red `#ff4d5b` / orange `#ff8a4d` / yellow `#f0c94d` /
  green `#4dc67b` / slate `#7b8fb3`. Dark background variants have
  `background:#2a1319` etc.

**Main grid (fill remainder):**
- Left column (flexible): triage table, `grid-template-columns: 1fr 260px`
- Right column (260px): exploration sidebar

**Triage table:**
- Columns: sev pill / cvss / advisory_id (link) / project slug (link) /
  affected pkg@ver / fixed_in / first_seen (relative time) / share button
- Sort order: `severity DESC, cvss DESC NULLS LAST, first_seen DESC`
- Filter: `severity IN ('critical','high') AND match.state = 'open'`
- Limit: top 12 above-the-fold; `show all N open matches →` link at table foot
- Font: JetBrains Mono 12px, row height 28px, zebra striping with `#141820`

**Exploration sidebar:**
- "Projects by match count" — top 8 projects with match counts, link to `/p/<slug>`
- "Top advisories" — top 6 advisories by number of project matches, link to `/a/<id>`
- "all N projects →" and "all N advisories →" deep links at each section foot

### 5.2 `GET /p/<owner>/<repo>` — per-project

Same top strip + KPI strip as landing. Main area replaced with:

- Project header: full slug, display_name, last walked timestamp, link to
  GitHub repo, link to current source_sha
- Full matches table for that project (same columns as triage, no severity
  filter — show all severities). Share actions are per-row (per-match) and
  per-advisory via click-through — no project-wide share button in v0.
  Filter widget row above: `[severity ▾] [ecosystem ▾]` that are
  `<form GET>` with URL params

URL params: `?severity=critical,high`, `?ecosystem=crates.io`. Multi-select
as comma-separated values. Empty = all.

### 5.3 `GET /a/<source-id>` — per-advisory

- Advisory header: severity pill, CVSS number, modified date, summary
  (single paragraph), details (collapsed behind "show details" summary/details)
- External links strip: GHSA / RUSTSEC / CVE / CWE — whichever are present
  in `references_json`
- "AFFECTS N PROJECTS" section heading + single "📤 share to inbox" button
  that produces the per-advisory email
- Table of affected matches: project slug (link to per-project page) /
  dep version / lockfile path / first_seen
- Affected range block: raw `affected[].ranges` expression rendered as
  `pkg < fix_version`, plus `fixed in X.Y.Z` highlighted

### 5.4 Per-match share POST handler

- `POST /share/match/<match-id>` receives no body (form has no fields
  beyond the hidden match-id in the URL)
- Builds `EmailPayload` with subject `[CKB advisory] <source-id> — <pkg>@<ver> in <slug>`
- Template scope: one match row, plus the advisory metadata
- SMTP send, 303 redirect back to `Referer` with `?sent=1` appended

### 5.5 Per-advisory share POST handler

- `POST /share/advisory/<source-id>`
- Subject `[CKB advisory] <source-id> — <pkg> < <fix-version> (N matches)`
- Template scope: advisory + list of all matches
- SMTP send, 303 redirect back to `/a/<source-id>?sent=1`

### 5.6 Flash message convention

`?sent=1` and `?sent_error=<short-reason>` as URL params. Rendered by
`base.html` as a top-of-page banner that's present when the param is.
No cookies, no session.

## 6. Email spec

### 6.1 Subject format

Machine-parseable so Gmail filters can pin these:

- Per-match: `[CKB advisory] <source-id> — <pkg>@<ver> in <slug>`
- Per-advisory: `[CKB advisory] <source-id> — <pkg> < <fix-version> (N matches)`

### 6.2 Body (multipart alternative — text + HTML)

Text and HTML bodies render from two Jinja2 templates with the same
context dict. HTML used in Gmail; text for mobile fallback and paranoia.

**Per-advisory body** (sections, in order):
1. Severity line: `Severity: CRITICAL · CVSS 9.8 · modified 2024-02-04`
2. `Summary:` paragraph
3. `Affects N projects in tracked stack:` bulleted list of
   `<slug> — <pkg>@<ver> (<lockfile path>)`
4. `Fix:` line if fixed_in available
5. `Links:` bulleted list — GHSA / RUSTSEC / CVE / Dashboard view
6. Signature footer: `Reported by ckb-advisory-watch · <host> · <timestamp>`

**Per-match body:** same structure but (3) is one line, (1)+(2) unchanged.

### 6.3 SMTP delivery

Gmail `smtp.gmail.com:465` with SSL, `smtplib.SMTP_SSL`. Auth: Gmail app
password stored in `config.toml` under `[share]`. Sender and recipient
both configurable (expected to be the same address, Phill sends to himself).

Failure mode: catch SMTP errors, log with `logging.error`, return 303
redirect with `?sent_error=<reason-code>` so the flash banner surfaces it.

### 6.4 Dashboard back-link

Every email includes a link back to the dashboard advisory page:
`<dashboard_base_url>/a/<source-id>`. This URL is also configurable — set
to `http://192.168.68.121:8080` on the Pi; when public mirror exists, set
to `https://wyltekindustries.com/advisories`.

## 7. Visual system (chain palette)

Derived from the 7007/7010 logo direction.

```
--bg              #0f1418   /* main canvas */
--bg-alt          #141c23   /* header, KPI strip, sidebar */
--bg-row          #141820   /* zebra striping */
--border          #1e2a35
--text            #e6edf3
--text-muted      #a5b3cc
--text-dim        #6a7a88
--link            #6fb0ff
--accent          #4285ff   /* primary button fill */

/* severity — same values as in the KPI tile CSS mockup */
--sev-critical    #ff4d5b
--sev-high        #ff8a4d
--sev-medium      #f0c94d
--sev-low         #4dc67b
--sev-unknown     #7b8fb3

/* severity tile backgrounds */
--sev-critical-bg #2a1319
--sev-high-bg     #2a1e13
--sev-medium-bg   #2a2513
--sev-low-bg      #132a1b
--sev-unknown-bg  #1b1e2a
```

Typography:
- UI & body: `Inter, system-ui, sans-serif`
- Data & IDs: `"JetBrains Mono", "SF Mono", Menlo, monospace`
- `font-variant-numeric: tabular-nums` globally
- Scale: 10 / 11 / 12 / 13 / 14 / 18 / 24 — 11-13px dominates

Layout:
- 4px spacing grid
- Max-width on content pages: 1366px (no max on dashboards per data-dense norm)
- All tables: sticky header, right-align numeric columns, mono for IDs/versions

## 8. Code layout

```
agent/dashboard/
  __init__.py
  server.py          aiohttp.web.Application factory + signal wiring
  queries.py         SQL helpers, all SELECT (no writes)
  share.py           EmailPayload dataclass + build_email + send_email
  templates/
    base.html        top strip + KPI strip + flash banner + slot for main
    index.html       extends base, triage table + exploration sidebar
    project.html     extends base, filterable match table
    advisory.html    extends base, advisory detail + affected projects
    email.html       multipart HTML body (advisory OR match)
    email.txt        multipart text body
  static/
    favicon.png
```

`agent/main.py` gains:
- `start_dashboard(conn_factory, config, stop) -> Task` — starts the aiohttp
  runner on a background task, shares the asyncio loop
- Added to the `asyncio.gather(...)` alongside the two poll loops

Config additions (`[dashboard]`, `[share]`):

```toml
[dashboard]
host = "0.0.0.0"
port = 8080
base_url = "http://192.168.68.121:8080"   # for email links

[share]
enabled = true
recipient = "toastmanau@gmail.com"
sender   = "toastmanau@gmail.com"
smtp_host = "smtp.gmail.com"
smtp_port = 465
smtp_user = "toastmanau@gmail.com"
smtp_password = "<gmail-app-password>"
```

## 9. Testing

Unit:
- `tests/test_dashboard_queries.py` — each query helper against an
  in-memory SQLite with realistic fixture data. Covers landing_data,
  project_context, advisory_context.
- `tests/test_dashboard_share.py` — `build_email()` returns correct
  subject format, recipient, body sections. Mocks `smtplib.SMTP_SSL`.
  Covers SMTP exceptions → caller gets a structured error.

Integration:
- `tests/test_dashboard_routes.py` — `aiohttp.test_utils.TestClient`
  against the app. Checks GET / returns 200 with expected markup
  skeletons, POST /share/... triggers send + redirects to referer with
  flash param. Uses a mock SMTP sender.

No browser/e2e tests — the no-JS constraint means all behavior is
inspectable in HTML; the unit/integration layers cover it.

## 10. Rollout sequence

1. Scaffolding: `agent/dashboard/` package, add deps (`aiohttp`,
   `jinja2`), wire into `main.py` alongside existing loops. Empty routes
   that return "hello". Test that `agent.main` starts cleanly with the
   extra task.
2. Queries + landing. `landing_data` + index.html render, no shares yet.
3. Per-project + per-advisory detail pages.
4. Share: email templates + SMTP dispatch + POST handlers + flash UI.
5. Config: `[dashboard]` and `[share]` sections in `config.example.toml`,
   documentation in README.
6. Deploy to Pi: bump deps, update config with Gmail app password,
   `systemctl --user restart`. Confirm from LAN browser.

Each step ships a PR-sized commit with its own tests green.

## 11. Open items to decide in plan, not design

- Exact triage table limit (12? 20?) — tune after first use
- Exact sidebar project limit (8? 10?) — same
- Whether "show details" on advisory page is collapsible (`<details>`) or
  always-expanded — UX call on first real use
- Favicon render (export from 7007 or 7010 at 32×32, 64×64 for retina) —
  asset work, defer

## 12. Out of scope — tracked separately

- pnpm-lock.yaml / yarn.lock parsers (Phase 1c)
- Python lockfile parsers (requirements.txt, poetry.lock, uv.lock)
- Telegram output (Phase 4 push channel, separate spec)
- Match state mutation UI (ack / suppress)
- GitHub issue as share target (swap mailer for HTTPS dispatcher)
- Static mirror for `wyltekindustries.com/advisories` (wget + rsync hook)
- Suppression workflow (SQL for now, UI later)
- Authentication
- Rate limiting

# ckb-advisory-watch Telegram output — design

**Status:** design approved, awaiting plan
**Date:** 2026-04-21
**Author:** Phill (brainstormed with Claude)

## 1. Goal

Wire `@ckb_osv_bot` as a push-pager for new advisory matches. One Telegram
message per advisory (grouped across affected projects) delivered to a DM
and/or a private channel, gated by `min_severity`, deduped via the existing
`emission` table. Complements the passive dashboard — Telegram is "something
new happened since I last checked," not a browse surface.

## 2. Non-goals

- **Inbound command handling.** No webhook, no `getUpdates` poll loop on the
  receive side. No ack/suppress/share-from-Telegram buttons. URL-only inline
  keyboards (they don't need callback handlers).
- **Channel chat_id auto-discovery.** Captured manually by the operator once
  via `/start@ckb_osv_bot` in the channel — post-deploy task, not a design
  concern.
- **Message editing / deletion / threading / topics / forums.** Send-only
  for v0.
- **Attachments** (no `sendDocument`, no images, no stickers).
- **Alert throttling / coalescing.** One message per new advisory, bounded
  by `min_severity`. If an ingest produces a flood, the flood gets sent.
  Re-visit if it becomes a problem.
- **Per-user filtering.** One severity threshold, shared by DM + channel.
- **Cold-start backfill.** First enable baselines everything as already-emitted;
  the dashboard is the recovery path for the backlog.

## 3. Scope

One new module + one new poll loop + three new config keys. All changes
land behind `[outputs.telegram].enabled=false` by default, so existing
users are unaffected until they flip the flag.

## 4. Architecture

**Approach B** from the brainstorm — separate poll loop, decoupled from the matcher.

```
  ┌─────────────────────────────────────────────────────┐
  │ agent/main.py (asyncio loop)                        │
  │                                                     │
  │   ┌────────────────┐  ┌─────────────────┐           │
  │   │ osv_poll_loop  │  │ github_poll_loop│           │
  │   └────────┬───────┘  └────────┬────────┘           │
  │            │                   │                    │
  │            └──── write to ─────┤                    │
  │                    match/      │                    │
  │                 advisory tables│                    │
  │                                ▼                    │
  │              ┌───────────────────────────┐          │
  │              │         SQLite            │          │
  │              │  (WAL; readers never      │          │
  │              │   block writers)          │          │
  │              └──┬───────────┬────────────┘          │
  │                 │ read      │ read                  │
  │   ┌─────────────▼─────┐   ┌─▼──────────────────┐    │
  │   │ dashboard aiohttp │   │ telegram_poll_loop │    │
  │   │ (existing)        │   │ (NEW)              │    │
  │   └───────────────────┘   └─┬──────────────────┘    │
  └──────────────────────────────┼──────────────────────┘
                                 │ HTTPS
                                 ▼
                         Telegram Bot API
                         sendMessage
```

### 4.1 Poll loop

`telegram_poll_loop(conn, config, stop)` runs as a third coroutine alongside
`osv_poll_loop` and `github_poll_loop` in `main.py`'s `asyncio.gather`.

Each tick (default 30 seconds):

1. **Disabled check.** If `[outputs.telegram].enabled=false`, sleep and loop.
   If `bot_token` empty or both `chat_id`+`channel_id` empty, log once-per-run
   "no destinations" and sleep.
2. **Baseline check.** On first tick after enable, call `baseline_if_first_run(conn)`.
   See §5.
3. **Find unemitted advisories per sub-channel.** SQL is parameterised on
   sub-channel name (`telegram.dm` or `telegram.channel`):
   ```sql
   SELECT DISTINCT m.advisory_id
   FROM match m
   JOIN advisory a ON a.id = m.advisory_id
   LEFT JOIN emission e
     ON e.match_id = m.id AND e.channel = ?   -- sub-channel name
   WHERE e.id IS NULL
     AND m.state = 'open'
     AND CASE COALESCE(a.severity, 'unknown')
           WHEN 'critical' THEN 4
           WHEN 'high'     THEN 3
           WHEN 'medium'   THEN 2
           WHEN 'low'      THEN 1
           ELSE 0
         END >= ?   -- min_severity as numeric
   ORDER BY a.published DESC
   ```
4. **For each advisory** (processed one at a time, sequentially):
   a. Call existing `queries.advisory_context(conn, source_id)` for the full
      bundle (same data source the email share uses).
   b. Filter `advisory.matches` to only those with no `telegram` emission row
      yet (re-query or filter in Python against a set of unemitted match_ids).
   c. Render HTML message via `format_message(advisory, filtered_matches, config)`.
   d. POST to Telegram `sendMessage` for the sub-channel's destination. See
      §6 for the exact request shape.
   e. On success (200 OK): within a single DB transaction, INSERT `emission`
      rows for every unemitted match_id of this advisory, using this
      sub-channel's name as `emission.channel` and the returned Telegram
      `message_id` as `artifact_path`.
   f. On transient failure (HTTP 5xx, 429): log, sleep per `retry_after`
      if 429, leave emission rows uninserted, return from the iteration.
      Next tick retries this advisory on this sub-channel only — the other
      sub-channel is unaffected.
   g. On permanent failure (HTTP 400 + "chat not found" / bad payload): log
      with full error, INSERT emission rows with `artifact_path='error:<reason>'`
      so the loop doesn't retry a poison message every 30 seconds on this
      sub-channel. The other sub-channel still runs normally.
5. **Sleep** `poll.telegram` seconds (default 30), interruptible via `stop` Event.

### 4.2 Sub-channels per destination

To track each destination's delivery independently without a schema change,
the `emission.channel` column carries a sub-channel name:

- `telegram.dm` — delivered to the DM `chat_id`
- `telegram.channel` — delivered to the `channel_id`

Each destination gets its own `emission` row per match. The existing
`UNIQUE (match_id, channel)` constraint prevents duplicate delivery
per-destination. Cost: 2× emission rows per match when both are configured —
acceptable for an append-only audit table.

**Poll loop processes each configured sub-channel independently.** One tick
finds unemitted `telegram.dm` matches, renders once per advisory, sends to
the DM chat_id, commits DM emission rows. Then separately finds unemitted
`telegram.channel` matches, sends to channel_id, commits channel emission
rows. A transient failure on one sub-channel only retries that sub-channel
on the next tick — no duplicate DM when the channel is flaky.

Rendering happens once per advisory per tick and is reused across both
sub-channels that tick, so we don't pay the template cost twice.

If only one of `chat_id` / `channel_id` is set, that sub-channel processes
alone. If neither is set, the whole loop sleeps (see §8).

## 5. Cold-start baseline

`baseline_if_first_run(conn, sub_channel, min_severity_level)` inserts
`emission` rows for every currently open match at or above the severity
floor, without actually sending. Runs once per sub-channel.

Guards: check `poller_state` for key `telegram.baseline_done.<sub_channel>`.
Present → skip. Absent → run baseline for this sub-channel then write the
key. Keying per sub-channel means adding a second destination later
(e.g., capturing `channel_id` a week after going live) cleanly baselines
just the new sub-channel.

SQL (transaction, parameterised on sub-channel name and severity floor):
```sql
INSERT INTO emission (match_id, channel, emitted_at, artifact_path)
SELECT m.id, ?, strftime('%s','now'), 'baseline'
FROM match m
JOIN advisory a ON a.id = m.advisory_id
LEFT JOIN emission e
  ON e.match_id = m.id AND e.channel = ?
WHERE e.id IS NULL
  AND m.state = 'open'
  AND CASE COALESCE(a.severity, 'unknown')
        WHEN 'critical' THEN 4
        WHEN 'high'     THEN 3
        WHEN 'medium'   THEN 2
        WHEN 'low'      THEN 1
        ELSE 0
      END >= ?   -- min_severity as numeric
;
INSERT OR REPLACE INTO poller_state (key, value, updated_at)
VALUES (?, '1', strftime('%s','now'));  -- telegram.baseline_done.<sub_channel>
```

Rationale for baselining at `>= min_severity` (not ALL severities): the
user's current threshold is their declared interest level. If they later
*drop* the threshold from `"medium"` to `"low"`, old low-severity matches
*will* fire — that's the user asking to see what was previously hidden,
which is the expected behaviour of a threshold knob.

Side effect: if the user *raises* the threshold later (e.g., `"medium"` →
`"high"`), the old baseline still covers the now-out-of-scope matches, so
they won't page retroactively. Good.

If the user ever wants to catch up on the current backlog, they can
delete the `telegram.baseline_done.<sub_channel>` key and relaunch — the
next tick will re-baseline only matches at or above the *then-current*
threshold, and newly-added matches since baseline will fire.

## 6. Message format

### 6.1 HTML body

Parse mode: `HTML`. All user-controlled fields (`advisory.summary`,
`advisory.details`, package names, project slugs, version strings) pass
through `html.escape()` before interpolation.

Template (Jinja2, new file `agent/output/templates/telegram.html`):

```jinja
{{ sev_emoji }} <b>{{ sev_label|upper }}</b>
{%- if advisory.cvss %} · CVSS {{ "%.1f"|format(advisory.cvss) }}{% endif %}
<b>{{ advisory.source_id|e }}</b>

<i>{{ summary_truncated|e }}</i>

Affects <b>{{ matches|length }}</b> project(s) in your stack:
{% for m in matches[:max_matches] -%}
• {{ m.project_slug|e }} — <code>{{ m.dep_name|e }}@{{ m.dep_version|e }}</code>
{% endfor -%}
{% if matches|length > max_matches -%}
… and <b>{{ matches|length - max_matches }} more</b> (see dashboard)
{% endif -%}
{% if advisory.fixed_in %}
Fix: upgrade to <code>{{ advisory.fixed_in|e }}</code>
{% endif %}
```

Jinja's `|e` filter double-quotes-safe-escapes into HTML entities.
Jinja's autoescape is NOT used (it would break `<b>` and `<code>` tags we
actually want). We escape explicitly per-field.

### 6.2 Limits & truncation

| Quantity | Value | Rationale |
|---|---|---|
| `summary_truncated` max chars | 500 | Keeps message skimmable |
| `max_matches` | 8 | "… and N more" if exceeded |
| Total message cap | 4000 chars | Telegram's `text` field limit is 4096; `reply_markup` is a separate field and doesn't count against the text budget. 96-char margin covers off-by-one errors in truncation math. |

If the rendered message exceeds 4000 chars after all of the above, truncate
the summary further in 50-char steps and re-render. Never split into two
messages — splitting by advisory breaks the one-message-per-CVE model.

### 6.3 Severity emoji map

```python
{
    "critical": "🔴",
    "high":     "🟠",
    "medium":   "🟡",
    "low":      "🟢",
    "unknown":  "⚪",
}
```

Emoji doubles as the iOS/Android lock-screen notification icon hint —
severity is readable without opening the app.

### 6.4 Inline keyboard (URL buttons only)

```json
{
  "inline_keyboard": [[
    {"text": "View on dashboard", "url": "<base_url>/a/<source-id>"},
    {"text": "View on GHSA",      "url": "<first advisory.references url of type ADVISORY, else first any ref>"}
  ]]
}
```

Buttons are on one row. If no `references` is present, omit the "View on GHSA"
button; if no `base_url` in config, omit the "View on dashboard" button. Never
send an empty inline_keyboard.

## 7. Config additions

```toml
[outputs.telegram]
enabled      = false               # EXISTING
bot_token    = ""                  # EXISTING
chat_id      = ""                  # EXISTING — your DM chat id
channel_id   = ""                  # NEW — channel chat id once captured
min_severity = "medium"            # EXISTING — floor: low | medium | high | critical

[poll]
telegram     = 30                  # NEW — seconds between emission passes
```

No migration. All new keys default to sensible values when absent.

## 8. Error handling

| Condition | Response |
|---|---|
| `enabled=false` | Loop sleeps; no work. |
| `bot_token` empty | Log once "telegram: bot_token missing", sleep. |
| Both `chat_id` and `channel_id` empty | Log once "telegram: no destinations", sleep. |
| Network error / DNS / connection refused | Log warning, don't mark as emitted, retry next tick. |
| HTTP 5xx | Log warning, retry next tick. |
| HTTP 429 (rate limit) | Respect `retry_after` from response JSON (seconds), `await asyncio.sleep(retry_after)`, then retry same advisory. |
| HTTP 400 with "chat not found" / bad token / forbidden | Log error, insert emission rows with `artifact_path='error:<reason>'` to prevent infinite retry. Operator fixes config and clears poison row. |
| HTTP 400 with HTML parse error | Same as above — treat as poison. Developer bug in template or escaping. |
| DB lock during emission insert | SQLite raises `OperationalError`. Catch and retry once; second failure logs warning and returns, next tick retries. |
| Transient success then process killed mid-emission | Ok — no emission row written, next restart re-sends. Telegram may receive a duplicate. Accept duplicate as lesser evil than silent drop. |

`httpx.AsyncClient` with `timeout=httpx.Timeout(connect=10, read=30, write=10, pool=10)` and `http2=False` (Telegram's API is HTTP/1.1).

## 9. Code layout

```
agent/output/
  __init__.py           # empty package marker
  telegram.py           # send_message, format_message, telegram_poll_loop,
                        # baseline_if_first_run
  templates/
    telegram.html       # Jinja2 template for message body
```

Module exports:
- `telegram_poll_loop(conn, config, stop)` — the asyncio coroutine wired into `main.py`. Iterates configured sub-channels per tick.
- `format_message(advisory, matches, config) -> tuple[str, dict]` — returns (html_body, inline_keyboard). Sub-channel-agnostic; called once per advisory per tick.
- `send_message(client, bot_token, destination_chat_id, html_body, inline_keyboard) -> int` — returns Telegram message_id; raises on permanent errors. Called once per sub-channel per advisory per tick.
- `baseline_if_first_run(conn, sub_channel, min_severity_level)` — idempotent via poller_state key `telegram.baseline_done.<sub_channel>`.

Sub-channel constants (module-level):
```python
SUBCH_DM      = "telegram.dm"
SUBCH_CHANNEL = "telegram.channel"
```

Reuses:
- `queries.advisory_context` and `AdvisoryContext`/`MatchRow` dataclasses from the dashboard package.
- `httpx.AsyncClient` — already a project dep.
- `jinja2.Environment` — already a project dep.

No new dependencies.

## 10. Testing

### 10.1 Unit

- `format_message` — fixture advisories with critical/high/medium/low/unknown
  severities, with/without CVSS, with/without fixed_in, 1 project / 8 / 12 (tests
  "and N more"), long summary (tests truncation), special chars in slugs/names
  (tests HTML escaping).
- `baseline_if_first_run` — seeds 3 open matches, asserts 3 `emission` rows
  with `artifact_path='baseline'` and `poller_state.telegram.baseline_done=1`.
  Second call is a no-op.
- `_unemitted_advisories_above(conn, min_severity)` — seeds mixed severities,
  asserts correct advisory IDs returned per threshold.

### 10.2 Integration (respx + aiohttp)

- `test_telegram_poll_sends_on_new_match` — seed one unemitted critical match,
  DM-only config, respx mocks Telegram API, assert sendMessage called with
  expected body + HTML, assert `telegram.dm` emission row written with
  `artifact_path` = mocked message_id.
- `test_telegram_poll_groups_by_advisory` — two unemitted matches, same
  advisory, different projects: ONE sendMessage call, TWO `telegram.dm`
  emission rows, both with the same `artifact_path` message_id.
- `test_telegram_poll_respects_min_severity` — seed below threshold,
  assert no sendMessage, no emission rows.
- `test_telegram_poll_429_retries_after_delay` — respx returns 429 with
  `retry_after: 1`, assert sleep + retry happens on the same sub-channel.
- `test_telegram_poll_400_poisons_emission` — respx returns 400 "chat not
  found", assert `telegram.dm` emission row written with
  `artifact_path='error:...'`, assert next tick does NOT retry that advisory
  on that sub-channel.
- `test_telegram_poll_baseline_skips_send` — matches exist but
  `telegram.baseline_done.telegram.dm` key absent, assert baseline inserts
  `telegram.dm` emission rows (at-or-above severity floor only) with
  `artifact_path='baseline'` and WITHOUT calling sendMessage.
- `test_telegram_poll_baseline_respects_severity_floor` — seed critical +
  low matches, min_severity=medium. Assert baseline inserts `telegram.dm`
  emission row ONLY for the critical match. Low match remains unemitted
  (and future ticks won't send it either because the floor still filters it).
- `test_telegram_poll_dm_and_channel_independent` — both destinations
  configured; DM returns 200 and channel returns 400 "chat not found".
  Assert `telegram.dm` emission rows written with real message_id, assert
  `telegram.channel` emission rows written with `artifact_path='error:...'`.
  Next tick: no sendMessage calls (both sub-channels are either sent or
  poisoned for this advisory).
- `test_telegram_poll_dm_transient_does_not_affect_channel` — DM returns
  500, channel returns 200. Assert `telegram.channel` emission rows
  written, assert no `telegram.dm` emission rows, assert next tick retries
  only the DM sub-channel.
- `test_telegram_poll_both_destinations_on_success` — both destinations
  return 200. Assert TWO sets of emission rows: one per sub-channel, each
  with the corresponding message_id in `artifact_path`.

### 10.3 Live smoke

End-to-end with real Telegram API against the @ckb_osv_bot. Manual
verification: (a) DM receives formatted message with working buttons,
(b) channel (once chat_id captured) receives same message, (c) cold-start
baseline silently adds emission rows without sending.

## 11. Rollout sequence

1. Scaffold `agent/output/` package + add `telegram_poll_loop` stub wired
   into `main.py` (returns immediately when enabled=false). Tests: module
   imports, poll loop exits cleanly with stop event.
2. Implement `format_message` + template + all unit tests for it (happy path,
   truncation, escaping).
3. Implement `send_message` + respx-mocked tests for the HTTP call (200, 429,
   400, 5xx, network error).
4. Implement `_unemitted_advisories_above` query helper + tests.
5. Implement `baseline_if_first_run` + test (idempotent via poller_state).
6. Wire the poll loop body: fetch unemitted, group, render, send, write
   emission. Integration tests via respx.
7. Config: add `channel_id` + `poll.telegram` to config.example.toml.
   README: Telegram setup section (BotFather flow, channel chat_id capture
   via `/start@ckb_osv_bot` trick).
8. Live smoke test on driveThree with real bot token (already captured).
   Confirm DM receives correctly formatted messages. Channel capture deferred
   until user forwards a channel message to get the id.

Each step ships a PR-sized commit with its tests green.

## 12. Out of scope — tracked separately

- Inbound callback handling (Ack / Suppress / Share-via-Email buttons)
- Channel chat_id auto-discovery via bot command
- Message edits when advisory data changes (e.g. CVSS update)
- Rate-limit coordination across multiple simultaneous advisories
- Image/chart alerts for critical advisories
- Scheduled digest messages (daily/weekly)
- User-specific severity thresholds (multi-recipient bot)
- Integration with GitHub Issue Bot (separate channel spec)

## 13. Open items for plan, not design

- Exact `poll.telegram` default value (30s vs 60s) — tune after first week
- Whether to log full Telegram API responses (useful for debugging, noisy
  otherwise) — default to concise-on-success, verbose-on-error
- Favicon / bot profile picture for `@ckb_osv_bot` (asset work, defer)

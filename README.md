# klaxon

Self-hosted social media monitoring and brand protection. Lightweight alternative
to ZeroFox / Akamai Brand Guardian, focused on **awareness and alerting** — never
automated remediation.

> The Python package is named `socmon` (the CLI is `socmon` too). The repo name
> is `klaxon` — short, on-brand for an alerting tool.

> **Status:** impersonation detector + mention/keyword spike detectors running
> end-to-end. Reddit + RSS collectors wired. Slack, **PagerDuty (Events API
> v2), and generic webhook (HMAC-signed)** alerters all live. Continuous mode
> (`socmon run`) and demo mode (`socmon demo`) both ship. systemd / launchd
> deployment templates + external heartbeat (Healthchecks.io-compatible) +
> retention via `socmon prune --older-than-days N`. 138 tests passing.
> Roadmap: `fake_job` detector, email alerter, digest routing.

## What it does

| Detector        | What it catches                                                  |
|-----------------|------------------------------------------------------------------|
| `mention_spike` | Statistically significant spikes in brand mention volume         |
| `keyword_spike` | Same idea, per configured keyword/expression (breach, leak, 0day…) |
| `impersonation` | Accounts mimicking the brand or executives (handles, avatars, bios) |
| `fake_job`      | Job listings that claim to be from us but don't match our ATS    |

Alerts go to Slack, email, PagerDuty, or any webhook. Out of scope (by design):
takedowns, evidence preservation pipelines, ML training.

## Architecture

```
collectors/   one adapter per platform   (Collector interface)
detectors/    spike + impersonation + jobs (Detector interface)
alerters/     slack / email / pd / webhook (Alerter interface)
storage/      sqlite for dev, postgres for prod (Storage interface)
config.py     pydantic schema, YAML loader
cli.py        socmon init | run | scan | backtest | alerts test
```

Three rules:
1. **Adding a platform = new Collector.** Nothing else changes.
2. **Adding a signal = new Detector** that reads from storage and emits Findings.
3. **Adding a notification channel = new Alerter.**

Detectors are stateless where possible; rolling baselines and seen-account hashes
live in the DB so `socmon backtest` can rerun the same logic over history.

## Claude Code Quick Start

Paste this prompt into Claude Code from any directory and it'll walk you through
setup interactively. Bring your brand name, legit handles, exec list, and (if
you want Slack alerts) a webhook URL.

```
You are setting up klaxon (https://github.com/aidan269/klaxon) — a self-hosted
social monitoring + brand protection tool. Drive the setup end-to-end:

1. If you're not already in a klaxon checkout, clone it into the current
   directory and cd in.
2. Verify Python 3.11+ is on PATH; if not, install it (brew install python@3.11
   on macOS, system package manager elsewhere) and use that interpreter.
3. Create .venv and `pip install -e .[dev,slack]`. Run `pytest -q` and
   confirm all tests pass before continuing.
4. Run `socmon init` to write a starter socmon.yaml.
5. Walk me through filling out the YAML, ONE QUESTION AT A TIME. Apply each
   answer to the file. Ask me for:
     - organization name
     - brand name + aliases + corporate domains
     - legitimate brand handles per platform (reddit / twitter / bluesky / ...)
     - executives to monitor (name, title, their legit handles)
     - keywords to track (and severity per keyword)
     - any local logo image paths (used for avatar pHashing)
   Skip optional sections I say I don't need.
6. If I want Slack alerts: prompt me for the webhook URL, tell me to
   `export SOCMON_SLACK_BRAND_WEBHOOK=...` in this shell, then run
   `socmon alerts test --channel slack-brand` to verify.
7. Run `socmon scan --window-hours 168` once. Read me the human summary: for
   each new account/post/finding it lists, give me the handle/title and the
   URL so I can click through. Note anything skipped because it's a stub
   (fake_job detector, PagerDuty / email / webhook alerters).
8. If any findings fired, walk me through the highest-severity one's metadata
   (signals breakdown for impersonation, top_authors + z-score for spikes) so
   I understand what triggered it.
9. Ask if I want klaxon to run on a recurring schedule. If yes, ask for the
   cadence (e.g. every 15 min, hourly, every 6 hours) and detect my OS:
     - macOS → write a `~/Library/LaunchAgents/com.klaxon.scan.plist` that
       runs `socmon scan` from this venv and config on the cadence; tell me
       to `launchctl load` it.
     - Linux → write a crontab line that does the same and tell me how to
       install it.
   The continuous `socmon run` mode is still on the roadmap, so cron/launchd
   is the recommended way to schedule until that lands.

Be concise. Don't paste full file contents back at me. Stop and wait for my
input when you need brand-specific values.
```

## Quick start (manual)

```bash
pip install -e ".[dev,slack]"       # Python 3.11+ required
socmon init                          # writes a starter socmon.yaml
$EDITOR socmon.yaml                  # fill in brand, execs, keywords, alerters
export SOCMON_SLACK_BRAND_WEBHOOK=https://hooks.slack.com/services/...
socmon alerts test --channel slack-brand   # verify alerting wiring
socmon demo                          # seed fixture data → see all detectors fire
socmon scan --window-hours 168       # one-shot collect + detect + alert
socmon run                           # continuous mode (Ctrl-C to stop)
```

### Demoing

`socmon demo` seeds a deterministic fixture dataset into a separate SQLite
file (`socmon-demo.db`) and runs the detectors over it. Every invocation
produces the same findings, so it's reproducible for screen-shares and
recordings — and it never touches your real `storage.dsn`.

What it seeds, all anchored to wall-clock `now` so timestamps look fresh:
- **5 candidate accounts** for impersonation scoring: a typosquat
  (`acme_officia1`), a Cyrillic homoglyph (`аcme_official`), an exec
  impersonation (matches your first configured exec), the legitimate
  handle as a control (should score 0), and a random unrelated user.
- **A 7-day baseline + recent spike** for `mention_spike` (~2 mentions/hr
  baseline → 30 in the current hour → critical, z>200).
- **8 recent "breach" posts** for `keyword_spike` against a near-zero
  baseline. Matches every configured keyword that contains your brand
  name (including a `{brand} AND breach` expression the demo injects so
  it works regardless of your real keyword list).

Expected output: ~7 findings across `mention_spike`, `keyword_spike`, and
`impersonation`, with severities ranging from HIGH to CRITICAL. The
`fake_job` detector is skipped (still a stub).

By default `socmon demo` does NOT call your alerters — the findings are
returned and printed, not dispatched. Pass `--alerts` to route them through
every configured Slack / email / PagerDuty / webhook channel as if they
were real (CLI confirms before sending; `--yes` skips the prompt for
scripted demos):

```bash
export SOCMON_SLACK_BRAND_WEBHOOK=https://hooks.slack.com/services/...
socmon demo --alerts          # asks before firing; ~7 real Slack messages
socmon demo --alerts --yes    # no prompt — useful for recordings
```

#### Showing continuous monitoring

Two demo paths, depending on whether you want a real-API view or a guaranteed
visual cadence.

**Production-shaped — real polling against Reddit and RSS:**

```bash
socmon -v run --detector-interval-seconds 30
```

The scheduler boots, each enabled collector fires immediately, and detectors
re-evaluate every 30s. Log lines show every collector tick, every detector
pass, and any findings as they fire — concrete proof the system is running.
Whether anything fires depends on what's actually happening upstream right
now, so it's honest but not deterministic.

**Drip-fed — guaranteed cadence of Slack alerts:**

```bash
socmon demo --watch --alerts --yes --interval-seconds 30
```

Performs the initial bulk seed (7 alerts to Slack at t=0), then every 30s
adds a new impersonation candidate and fires its finding through to Slack.
Manager sees a continuous trickle: t=0 → 7 messages; t=30 → 1 message;
t=60 → 1 message; … until Ctrl-C. Style of impersonation rotates between
typosquat / homoglyph / "_support" / "_team" / "_help" so the alerts feel
varied. Use this when you want a live screen-share that's visually busy
without depending on real upstream signal.

## Configuration

See [`examples/socmon.yaml`](examples/socmon.yaml) for an annotated example.
Credentials are referenced by env-var name only — the config file never holds secrets.

Key knobs:
- `baseline_window_days` — how much history feeds the spike baseline (default 7)
- `spike_z_threshold` — z-score above which we call something a spike (default 3.0)
- `spike_min_volume` — ignore "spikes" off near-zero baselines (default 5)
- per-detector `options` block overrides the defaults

### What klaxon remembers between runs

State lives in the SQLite (or Postgres) DB at `storage.dsn`. Persisted across
runs:

- **observations** — every account/post we've ingested, keyed by stable
  platform-id. Re-collecting is idempotent.
- **findings** — every alert ever fired, keyed by a deterministic id
  (detector + entity + bucket). The same finding cannot re-alert.
- **watermarks** — per-collector "latest `created_at` we successfully
  ingested," so the next run only fetches posts newer than that.
- **kv_state** — generic detector state. Today, the impersonation detector
  uses it to remember a hash of each account's (handle, display name, bio,
  avatar pHash); unchanged accounts don't regenerate identical findings.

Recomputed every run (no persistence needed):

- Rolling baselines for the spike detectors — derived from raw observations.
  This is what makes `socmon backtest` work: replaying detectors over the
  same DB always produces the same findings.
- Brand-logo pHashes — re-hashed from `brand.logo_paths` on startup.

There is no automatic retention or GC yet. The DB grows. That's fine for v1
on a single tenant; we'll add retention when it bites.

### Scheduling

Two ways to keep klaxon ticking. Pick one — they're mutually exclusive.

**`socmon run` — long-running process.** One Python process, APScheduler
ticks each enabled collector on its `poll_interval_seconds` and ticks all
detectors together on `detector_interval_seconds` (config-level, default
300s). Storage / collectors / detectors / alerters are built once at startup
and reused, so the steady-state per-tick cost is lower than cron's "boot a
new Python every minute" model. Ctrl-C or SIGTERM drains in-flight jobs and
exits. Pair with `systemd` (Linux) or `launchd` (macOS) for restart-on-crash
supervision.

```bash
socmon run                                 # use config's detector_interval_seconds
socmon run --detector-interval-seconds 60  # override for demo / incident mode
```

**`socmon scan` under cron — fire-and-exit.** Process boots, scans once,
exits. More robust to memory leaks and crashes (each tick is a fresh
process). Plays nicely with cron / GitHub Actions cron / k8s `CronJob`.

```bash
# crontab -e — every 15 minutes
*/15 * * * * cd /path/to/klaxon && /path/to/.venv/bin/socmon scan --window-hours 24 >> /tmp/klaxon.log 2>&1
```

The Claude Code Quick Start prompt above can write the launchd plist or
crontab line for you on request.

### Running klaxon in production

For set-and-forget operation, wrap `socmon run` in a process supervisor
(systemd / launchd / Docker) and add a heartbeat so you find out when
*klaxon itself* dies, not when your Slack alerts mysteriously stop.

**systemd (Linux):** [`deploy/klaxon.service`](deploy/klaxon.service) is a
ready-to-go unit. Runs as an unprivileged `klaxon` user, restarts on
failure with rate-limiting, logs to journald, hardened with the standard
`ProtectSystem`/`PrivateTmp` flags. Secrets (webhook URLs, etc.) live in
`/etc/klaxon/klaxon.env`, never in the YAML.

```bash
sudo cp deploy/klaxon.service /etc/systemd/system/klaxon.service
sudo systemctl daemon-reload
sudo systemctl enable --now klaxon
journalctl -u klaxon -f          # tail logs
```

**launchd (macOS):** [`deploy/com.klaxon.plist`](deploy/com.klaxon.plist)
is the equivalent. Replace the `$USER` placeholders and webhook string,
drop it into `~/Library/LaunchAgents/`, then:

```bash
launchctl load -w ~/Library/LaunchAgents/com.klaxon.plist
launchctl list | grep klaxon     # confirm it's running
```

**Heartbeat (works with both):** add a `heartbeat_url` to your config and
klaxon will GET it after every successful detector tick. Designed for
[Healthchecks.io](https://healthchecks.io) but works with anything that
listens for a HEAD/GET as a "still alive" signal:

```yaml
# socmon.yaml
heartbeat_url: https://hc-ping.com/<your-uuid>
heartbeat_timeout_seconds: 5    # optional; defaults to 5s
```

Detector-tick failures deliberately skip the ping, so a silent klaxon is
distinguishable from a healthy one that just hasn't found anything yet.
Configure your Healthchecks.io check with a grace period a few minutes
longer than `detector_interval_seconds` and you'll get paged the moment
klaxon actually stops working — independent of whether Slack happens to
have anything to say.

**How frequent can I go?**

| Cadence  | Status                   | Why                                                              |
|----------|--------------------------|------------------------------------------------------------------|
| ≥ 5 min  | Production sweet spot    | Well under Reddit's anonymous ~60 req/min limit                  |
| 1–4 min  | Demo / incident-response | Reddit holds; some RSS feeds will start 429ing on tighter cycles |
| < 1 min  | Only via launchd/systemd | cron's floor is 1 min; expect upstream rate-limit degradation    |

The Reddit collector self-throttles to ~1.1s between requests and a typical
scan costs ~5–15 seconds of upstream API time (depending on how many brand
aliases + executives are configured). RSS feeds are source-dependent —
BleepingComputer and Krebs tolerate tight polling, Google News doesn't.

**Klaxon is dedup-safe at any cadence.** Watermarks per collector prevent
re-ingesting observations we've already seen, and findings are keyed on a
deterministic id (detector + entity + time-bucket), so an over-aggressive
cron only burns upstream API quota — it does not produce duplicate alerts.

### Tuning thresholds

Use `socmon backtest --start ... --end ... --dry-run` to replay detectors over
historical observations and print the findings that *would* have fired. Tweak
thresholds in YAML, rerun. Once you're happy, drop `--dry-run` to populate the
findings table for real (alerts still suppressed).

## Adding a collector

1. Create `socmon/collectors/<platform>.py`.
2. Subclass `Collector`, decorate with `@register("<platform>")`.
3. Implement `collect()` (yields `Observation`s honoring `query.since`) and
   `discover_accounts()` (yields `AccountObservation`s for the impersonation
   detector — return early if the platform has no account concept).
4. Reference it in `socmon.yaml`:

```yaml
collectors:
  - name: my-platform
    type: <platform>
    poll_interval_seconds: 300
    credentials_env: { TOKEN: MYPLAT_TOKEN }
    options: { ... }
```

That's it — no other code changes.

## Tests

```bash
pytest
```

Tests under `tests/` cover the spike math, the keyword DSL parser/evaluator,
the impersonation scoring (incl. confusables, exclusions, severity bands),
end-to-end runs of both spike detectors over a real SQLite round-trip,
the continuous-mode scheduler (job registration, per-tick failure isolation,
graceful shutdown, heartbeat ping policy), and the `socmon demo` fixture
pipeline (deterministic seeds → multi-detector findings, plus drip-fed
`--watch` mode). 113 cases total; the suite finishes in a couple of seconds
with no network calls.

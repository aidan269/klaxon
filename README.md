# klaxon

Self-hosted social media monitoring and brand protection. Lightweight alternative
to ZeroFox / Akamai Brand Guardian, focused on **awareness and alerting** — never
automated remediation.

> The Python package is named `socmon` (the CLI is `socmon` too). The repo name
> is `klaxon` — short, on-brand for an alerting tool.

> **Status:** impersonation detector + mention/keyword spike detectors running
> end-to-end. Reddit + RSS collectors and Slack alerter wired. 86 tests passing.
> Roadmap: `fake_job` detector, PagerDuty/email/webhook alerters, continuous
> scheduler (`socmon run`).

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
socmon scan --window-hours 168       # one-shot collect + detect + alert
# socmon run                         # continuous mode — on the roadmap
```

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

`socmon run` (continuous mode with APScheduler) is on the roadmap. Until it
lands, run `socmon scan` on a cadence via cron (Linux) or launchd (macOS):

```bash
# crontab -e — every 15 minutes
*/15 * * * * cd /path/to/klaxon && /path/to/.venv/bin/socmon scan --window-hours 24 >> /tmp/klaxon.log 2>&1
```

The Claude Code Quick Start prompt above can write the launchd plist or
crontab line for you on request.

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
the impersonation scoring (incl. confusables, exclusions, severity bands), and
end-to-end runs of both spike detectors over a real SQLite round-trip. 86 cases
total; the suite finishes in a couple of seconds with no network calls.

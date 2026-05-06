# klaxon

Self-hosted social media monitoring and brand protection. Lightweight alternative
to ZeroFox / Akamai Brand Guardian, focused on **awareness and alerting** — never
automated remediation.

> The Python package is named `socmon` (the CLI is `socmon` too). The repo name
> is `klaxon` — short, on-brand for an alerting tool.

> **Status:** scaffold + interfaces. Implementations land after structure review.

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

## Quick start (after impl lands)

```bash
pip install -e ".[dev,slack]"
socmon init                         # writes a starter socmon.yaml
$EDITOR socmon.yaml
export SOCMON_SLACK_BRAND_WEBHOOK=https://hooks.slack.com/services/...
socmon alerts test                  # verify alerting wiring
socmon scan                         # one-shot
socmon run                          # continuous
```

## Configuration

See [`examples/socmon.yaml`](examples/socmon.yaml) for an annotated example.
Credentials are referenced by env-var name only — the config file never holds secrets.

Key knobs:
- `baseline_window_days` — how much history feeds the spike baseline (default 7)
- `spike_z_threshold` — z-score above which we call something a spike (default 3.0)
- `spike_min_volume` — ignore "spikes" off near-zero baselines (default 5)
- per-detector `options` block overrides the defaults

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

Detector tests use fixture observations under `tests/fixtures/` so the spike math,
impersonation scoring, and fake-job heuristics can be verified deterministically.

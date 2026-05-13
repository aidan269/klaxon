"""Click CLI: `socmon`.

Subcommands:
  init           write a starter socmon.yaml
  run            continuous mode — start scheduler, poll forever
  scan           one-shot — collect once, run detectors, exit
  backtest       run detectors over historical observations only (no collection)
  demo           seed fixture data + run detectors → deterministic demo output
  prune          delete observations and findings older than N days
  alerts test    fire a synthetic finding through every configured alerter
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click

from socmon import runner
from socmon.config import load_config


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


@click.group()
@click.option("--config", "-c", default="socmon.yaml", show_default=True,
              help="Path to YAML config.")
@click.option("--verbose", "-v", is_flag=True, help="Debug logging.")
@click.pass_context
def main(ctx: click.Context, config: str, verbose: bool) -> None:
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config
    _setup_logging(verbose)


@main.command()
@click.option("--force", is_flag=True, help="Overwrite existing socmon.yaml.")
@click.pass_context
def init(ctx: click.Context, force: bool) -> None:
    """Write the example socmon.yaml to the current directory."""
    target = Path(ctx.obj["config_path"])
    if target.exists() and not force:
        raise click.ClickException(f"{target} already exists; rerun with --force to overwrite")
    src = Path(__file__).parent.parent / "examples" / "socmon.yaml"
    if not src.exists():
        raise click.ClickException(f"example config not found at {src}")
    shutil.copy(src, target)
    click.echo(f"wrote {target}")


@main.command()
@click.option("--detector-interval-seconds", type=int, default=None,
              help="Override config's detector_interval_seconds for this run.")
@click.pass_context
def run(ctx: click.Context, detector_interval_seconds: int | None) -> None:
    """Continuous mode: poll collectors and run detectors on a cadence.

    Each enabled collector ticks on its own `poll_interval_seconds` from the
    config. All detectors tick together on `detector_interval_seconds`
    (default 300s). Ctrl-C / SIGTERM drains in-flight jobs and exits.
    """
    from socmon.scheduler import SocmonScheduler

    cfg = load_config(ctx.obj["config_path"])
    if detector_interval_seconds is not None:
        cfg.detector_interval_seconds = detector_interval_seconds
    sched = SocmonScheduler(cfg)
    sched.setup()
    click.echo("klaxon running. Ctrl-C to stop.")
    sched.run()


@main.command()
@click.option("--window-hours", default=24, show_default=True,
              help="Detection window size, in hours.")
@click.option("--findings-only", is_flag=True,
              help="Skip the Accounts and Posts sections; show findings only.")
@click.option("--json", "as_json", is_flag=True,
              help="Emit JSON instead of the human-readable summary.")
@click.pass_context
def scan(ctx: click.Context, window_hours: int, findings_only: bool, as_json: bool) -> None:
    """One-shot scan: collect, detect, alert."""
    cfg = load_config(ctx.obj["config_path"])
    summary = runner.scan(cfg, window_hours=window_hours)
    if as_json:
        click.echo(json.dumps(summary, indent=2))
    else:
        _render_scan_summary(summary, findings_only=findings_only)


@main.command()
@click.option("--start", required=True, help="ISO8601 start of backtest window.")
@click.option("--end", required=True, help="ISO8601 end of backtest window.")
@click.option("--detector", "-d", multiple=True, help="Limit to these detector names.")
@click.option("--dry-run/--no-dry-run", default=True,
              help="If True (default), do not insert findings or fire alerts.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
@click.pass_context
def backtest(ctx: click.Context, start: str, end: str, detector: tuple[str, ...],
             dry_run: bool, as_json: bool) -> None:
    """Replay detectors over already-collected observations."""
    cfg = load_config(ctx.obj["config_path"])
    summary = runner.backtest(
        cfg,
        start=datetime.fromisoformat(start),
        end=datetime.fromisoformat(end),
        detector_names=list(detector) or None,
        dry_run=dry_run,
    )
    if as_json:
        click.echo(json.dumps(summary, indent=2))
    else:
        _render_backtest_summary(summary)


# ---------------------------------------------------------------------------
# Render helpers
# ---------------------------------------------------------------------------


def _render_scan_summary(summary: dict, *, findings_only: bool = False) -> None:
    """Compact text layout. URLs are emitted bare so terminals make them clickable.

    When `findings_only` is True, the Accounts and Posts blocks are skipped —
    for demos and incident-response views where only the alerts matter.
    """
    win = summary.get("window") or ["?", "?"]
    click.echo("")
    click.echo(f"Scan window: {_fmt_ts(win[0])} → {_fmt_ts(win[1])}")
    click.echo("")

    sections: list[tuple[str, dict, object]] = []
    if not findings_only:
        sections.append(("Accounts", summary["new_accounts"], _format_account_line))
        sections.append(("Posts", summary["new_posts"], _format_post_line))
    sections.append(("Findings", summary["new_findings"], _format_finding_line))

    for label, section, fmt in sections:
        count = section["count"]
        click.echo(f"  {label}: {count} new")
        for item in section["items"]:
            click.echo(f"    └ {fmt(item)}")
        if section["truncated"]:
            shown = len(section["items"])
            click.echo(f"    └ … ({count - shown} more not shown)")
        click.echo("")


def _render_backtest_summary(summary: dict) -> None:
    win = summary.get("window") or ["?", "?"]
    click.echo("")
    click.echo(
        f"Backtest window: {_fmt_ts(win[0])} → {_fmt_ts(win[1])}  "
        f"(dry_run={summary['dry_run']})"
    )
    click.echo("")
    f = summary["new_findings"]
    click.echo(f"  Findings: {f['count']} new")
    for item in f["items"]:
        click.echo(f"    └ {_format_finding_line(item)}")
    if f["truncated"]:
        click.echo(f"    └ … ({f['count'] - len(f['items'])} more not shown)")
    click.echo("")


def _fmt_ts(iso: str) -> str:
    """Drop microseconds + trailing tz suffix for readable display."""
    if not iso or iso == "?":
        return iso or "?"
    try:
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, TypeError):
        return iso


def _format_account_line(item: dict) -> str:
    handle = item.get("handle") or "?"
    url = item.get("url") or ""
    return f"{item['platform']}/{handle}" + (f"  {url}" if url else "")


def _format_post_line(item: dict) -> str:
    author = item.get("author") or "?"
    title = item.get("title") or "(no text)"
    url = item.get("url") or ""
    return f"[{item['platform']}] {author}: {title}" + (f"  {url}" if url else "")


def _format_finding_line(item: dict) -> str:
    sev = (item.get("severity") or "").upper()
    score = item.get("score")
    score_str = f" (score {score:.1f})" if isinstance(score, (int, float)) else ""
    url = item.get("url") or ""
    return f"[{sev}] {item['title']}{score_str}" + (f"  {url}" if url else "")


@main.command()
@click.option("--alerts", is_flag=True,
              help="Route demo findings through configured alerters. "
                   "Fires REAL Slack/email/PagerDuty messages — confirm before use.")
@click.option("--yes", is_flag=True, help="Skip the --alerts confirmation prompt.")
@click.option("--watch", is_flag=True,
              help="Continuous-monitoring demo: initial seed + a new "
                   "impersonation finding every --interval-seconds, "
                   "until Ctrl-C. Pair with --alerts for live Slack drip.")
@click.option("--interval-seconds", default=60, show_default=True, type=int,
              help="Seconds between drips in --watch mode.")
@click.option("--findings-only", is_flag=True,
              help="Show only the Findings block — skips Accounts and Posts. "
                   "Recommended for recordings and screen-shares.")
@click.option("--catch", "catch_url", is_flag=False, flag_value="http://127.0.0.1:8765/",
              default=None,
              help="Route demo findings to a local webhook receiver (default URL: "
                   "http://127.0.0.1:8765/). Pair with `python examples/catch.py` "
                   "in another pane to show alerts firing without touching real Slack.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
@click.pass_context
def demo(ctx: click.Context, alerts: bool, yes: bool, watch: bool,
         interval_seconds: int, findings_only: bool, catch_url: str | None,
         as_json: bool) -> None:
    """Seed fixture data + run detectors → deterministic demo findings.

    Uses a separate SQLite file (socmon-demo.db in cwd) so your real DB is
    never touched. Each invocation wipes the demo DB and reseeds, so the
    output is reproducible — good for screen-sharing and recordings.

    By default, alerters are NOT called (the demo is local-only). Pass
    --alerts to fire fixture findings through your configured Slack /
    email / PagerDuty / webhook channels. Pair with --watch to drip a
    fresh finding every --interval-seconds so a manager can see the
    pipeline running continuously.
    """
    from socmon import demo as demo_mod

    if alerts and not yes:
        msg = (
            f"--alerts --watch will keep sending real messages every "
            f"{interval_seconds}s until Ctrl-C. Continue?"
            if watch else
            "--alerts will send ~7 real messages to every alerter your routes "
            "match (Slack/email/PagerDuty/etc). Continue?"
        )
        click.confirm(msg, abort=True)

    cfg = load_config(ctx.obj["config_path"])

    if watch:
        if catch_url:
            suffix = f" · catch → {catch_url}"
        elif alerts:
            suffix = " · alerts → configured channels"
        else:
            suffix = " · no network calls"
        click.echo(
            f"klaxon demo --watch · initial seed + drip every {interval_seconds}s"
            f"{suffix} · Ctrl-C to stop"
        )
        demo_mod.run_demo_watch(
            cfg,
            route_alerts=alerts,
            drip_interval_seconds=interval_seconds,
            catch_url=catch_url,
        )
        return

    summary = demo_mod.run_demo(cfg, route_alerts=alerts, catch_url=catch_url)
    if as_json:
        click.echo(json.dumps(summary, indent=2))
    else:
        if catch_url:
            suffix = f" · catch → {catch_url}"
        elif alerts:
            suffix = " · alerts dispatched"
        else:
            suffix = " · no network calls"
        click.echo(f"klaxon demo — fixture data, DB: {summary['db']}{suffix}")
        _render_scan_summary(summary, findings_only=findings_only)


@main.command()
@click.option("--older-than-days", required=True, type=int,
              help="Delete data with created_at / detected_at older than N days.")
@click.option("--dry-run", is_flag=True, default=False,
              help="Show how much would be deleted without actually deleting.")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
@click.pass_context
def prune(ctx: click.Context, older_than_days: int, dry_run: bool, yes: bool) -> None:
    """Delete old observations and findings from storage.

    Watermarks and kv_state are deliberately preserved — they're tiny and the
    detectors' correctness depends on them (kv_state in particular keeps the
    impersonation detector from re-emitting findings against accounts it has
    already scored).
    """
    if older_than_days < 1:
        raise click.BadParameter("--older-than-days must be >= 1")

    cfg = load_config(ctx.obj["config_path"])
    storage = runner.build_storage(cfg)
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)

    n_obs, n_findings = storage.prune(before=cutoff, dry_run=True)

    if n_obs == 0 and n_findings == 0:
        click.echo(f"Nothing to prune (no rows older than {cutoff.isoformat()}).")
        return

    msg = (
        f"{'DRY RUN: would delete' if dry_run else 'About to delete'} "
        f"{n_obs} observation(s) + {n_findings} finding(s) older than "
        f"{cutoff.isoformat()}."
    )
    click.echo(msg)

    if dry_run:
        return
    if not yes:
        click.confirm("Proceed? This is irreversible.", abort=True)

    storage.prune(before=cutoff, dry_run=False)
    click.echo(f"deleted {n_obs} observation(s) + {n_findings} finding(s).")


@main.group()
def alerts() -> None:
    """Alert utilities."""


@alerts.command("test")
@click.option("--channel", "-c", multiple=True, help="Limit to these alerter names.")
@click.pass_context
def alerts_test(ctx: click.Context, channel: tuple[str, ...]) -> None:
    """Send a synthetic finding through each configured alerter."""
    cfg = load_config(ctx.obj["config_path"])
    runner.alerts_test(cfg, channels=list(channel) or None)
    click.echo("sent test alert(s)")


if __name__ == "__main__":
    main()

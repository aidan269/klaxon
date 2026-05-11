"""Click CLI: `socmon`.

Subcommands:
  init           write a starter socmon.yaml
  run            continuous mode — start scheduler, poll forever
  scan           one-shot — collect once, run detectors, exit
  backtest       run detectors over historical observations only (no collection)
  demo           seed fixture data + run detectors → deterministic demo output
  alerts test    fire a synthetic finding through every configured alerter
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime
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
@click.option("--json", "as_json", is_flag=True,
              help="Emit JSON instead of the human-readable summary.")
@click.pass_context
def scan(ctx: click.Context, window_hours: int, as_json: bool) -> None:
    """One-shot scan: collect, detect, alert."""
    cfg = load_config(ctx.obj["config_path"])
    summary = runner.scan(cfg, window_hours=window_hours)
    if as_json:
        click.echo(json.dumps(summary, indent=2))
    else:
        _render_scan_summary(summary)


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


def _render_scan_summary(summary: dict) -> None:
    """Compact text layout. URLs are emitted bare so terminals make them clickable."""
    win = summary.get("window") or ["?", "?"]
    click.echo("")
    click.echo(f"Scan window: {win[0]} → {win[1]}")
    click.echo("")

    sections = [
        ("Accounts", summary["new_accounts"], _format_account_line),
        ("Posts", summary["new_posts"], _format_post_line),
        ("Findings", summary["new_findings"], _format_finding_line),
    ]
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
    click.echo(f"Backtest window: {win[0]} → {win[1]}  (dry_run={summary['dry_run']})")
    click.echo("")
    f = summary["new_findings"]
    click.echo(f"  Findings: {f['count']} new")
    for item in f["items"]:
        click.echo(f"    └ {_format_finding_line(item)}")
    if f["truncated"]:
        click.echo(f"    └ … ({f['count'] - len(f['items'])} more not shown)")
    click.echo("")


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
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
@click.pass_context
def demo(ctx: click.Context, alerts: bool, yes: bool, as_json: bool) -> None:
    """Seed fixture data + run detectors → deterministic demo findings.

    Uses a separate SQLite file (socmon-demo.db in cwd) so your real DB is
    never touched. Each invocation wipes the demo DB and reseeds, so the
    output is reproducible — good for screen-sharing and recordings.

    By default, alerters are NOT called (the demo is local-only). Pass
    --alerts to fire the fixture findings through your configured Slack /
    email / PagerDuty / webhook channels — useful for "this is what an
    impersonation alert looks like" demos in a real Slack workspace.
    """
    from socmon import demo as demo_mod

    if alerts and not yes:
        click.confirm(
            "--alerts will send ~7 real messages to every alerter your routes "
            "match (Slack/email/PagerDuty/etc). Continue?",
            abort=True,
        )

    cfg = load_config(ctx.obj["config_path"])
    summary = demo_mod.run_demo(cfg, route_alerts=alerts)
    if as_json:
        click.echo(json.dumps(summary, indent=2))
    else:
        suffix = " · alerts dispatched" if alerts else " · no network calls"
        click.echo(f"klaxon demo — fixture data, DB: {summary['db']}{suffix}")
        _render_scan_summary(summary)


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

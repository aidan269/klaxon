"""Click CLI: `socmon`.

Subcommands:
  init           write a starter socmon.yaml
  run            continuous mode — start scheduler, poll forever     (stub: scheduler slice)
  scan           one-shot — collect once, run detectors, exit
  backtest       run detectors over historical observations only (no collection)
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
@click.pass_context
def run(ctx: click.Context) -> None:
    """Continuous mode (scheduler)."""
    raise click.ClickException(
        "`socmon run` is not implemented yet — the scheduler lands with the "
        "spike-detector slice. Use `socmon scan` (one-shot) or schedule it via cron."
    )


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

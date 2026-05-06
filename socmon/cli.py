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
@click.pass_context
def scan(ctx: click.Context, window_hours: int) -> None:
    """One-shot scan: collect, detect, alert."""
    cfg = load_config(ctx.obj["config_path"])
    summary = runner.scan(cfg, window_hours=window_hours)
    click.echo(json.dumps(summary, indent=2))


@main.command()
@click.option("--start", required=True, help="ISO8601 start of backtest window.")
@click.option("--end", required=True, help="ISO8601 end of backtest window.")
@click.option("--detector", "-d", multiple=True, help="Limit to these detector names.")
@click.option("--dry-run/--no-dry-run", default=True,
              help="If True (default), do not insert findings or fire alerts.")
@click.pass_context
def backtest(ctx: click.Context, start: str, end: str, detector: tuple[str, ...],
             dry_run: bool) -> None:
    """Replay detectors over already-collected observations."""
    cfg = load_config(ctx.obj["config_path"])
    summary = runner.backtest(
        cfg,
        start=datetime.fromisoformat(start),
        end=datetime.fromisoformat(end),
        detector_names=list(detector) or None,
        dry_run=dry_run,
    )
    click.echo(json.dumps(summary, indent=2))


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

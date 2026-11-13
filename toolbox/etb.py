#!/app/.venv/bin/python
"""etb: esgf-toolbox helper. What is in this image, and a log of what was run with it.

\b
    etb versions [--json | --live]          versions of everything in the image (build manifest)
    etb run [--note TEXT] -- COMMAND ...     run a command and record it in the run log
    etb log [--last N] [--json]              show the run log

The run log is a JSON lines file (default ./etb-runs.jsonl, or --log / $ETB_LOG). Each run writes a
"start" record before the command and an "end" record after it, so a run killed by a job time limit
still leaves a trace. Every record carries the image build_id, which links it to the version manifest.
"""

from __future__ import annotations

import json
import os
import pwd
import signal
import socket
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from importlib.metadata import entry_points
from pathlib import Path
from typing import Annotated

import typer

sys.path.insert(0, str(Path(__file__).resolve().parent))
import versions  # noqa: E402  (toolbox/versions.py, next to this file)

app = typer.Typer(help=__doc__, no_args_is_help=True, add_completion=False, rich_markup_mode=None)

LogOption = Annotated[Path, typer.Option("--log", envvar="ETB_LOG", help="run log (JSON lines)")]
DEFAULT_LOG = Path("etb-runs.jsonl")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _user() -> str:
    # $USER is not reliable here: the image sets USER=root, whatever uid the container runs as.
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        return str(os.getuid())


def _image() -> dict | None:
    if not versions.MANIFEST.is_file():
        return None
    manifest = json.loads(versions.MANIFEST.read_text())
    return {"build_id": manifest["build_id"], **manifest["build"]}


def _provider(command: str) -> dict | None:
    """The installed package that provides this console command, e.g. esgdrs -> esgprep 3.4.0."""
    for ep in entry_points(group="console_scripts", name=Path(command).name):
        if ep.dist is not None:
            return {"package": ep.dist.name, "version": ep.dist.version}
    return None


def _append(log: Path, record: dict) -> None:
    with log.open("a") as out:  # one write per record: lines from concurrent runs do not interleave
        out.write(json.dumps(record) + "\n")


@app.command("versions")
def versions_command(
    as_json: Annotated[bool, typer.Option("--json", help="print the full manifest as JSON")] = False,
    live: Annotated[bool, typer.Option("--live", help="compare with what is installed now (exit 1 on drift)")] = False,
):
    """Versions of everything in the image, recorded when it was built."""
    raise typer.Exit(versions.main(["--json"] if as_json else ["--live"] if live else []))


@app.command("run", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def run_command(
    command: Annotated[list[str], typer.Argument(help="the command to run, after --")],
    log: LogOption = DEFAULT_LOG,
    note: Annotated[str | None, typer.Option(help="free text saved with the run")] = None,
):
    """Run a command and record it in the run log. Exits with the command's exit code."""
    run_id = uuid.uuid4().hex[:12]
    start = time.monotonic()
    base = {"run_id": run_id, "time": _now()}
    try:
        _append(log, {**base, "event": "start", "argv": command, "cwd": os.getcwd(), "user": _user(),
                      "uid": os.getuid(), "host": socket.gethostname(), "image": _image(),
                      "provider": _provider(command[0]), "note": note})
    except OSError as exc:
        typer.echo(f"etb: cannot write the run log {log}: {exc}. Nothing was run (use --log PATH).", err=True)
        raise typer.Exit(2) from None

    env = {**os.environ, "ETB_RUN_ID": run_id}
    try:
        child = subprocess.Popen(command, env=env)
    except OSError as exc:
        _append(log, {**base, "time": _now(), "event": "end", "status": "not_started", "exit_code": 127,
                      "error": str(exc), "duration_seconds": 0.0})
        typer.echo(f"etb: cannot run {command[0]}: {exc}", err=True)
        raise typer.Exit(127) from None

    # A cancelled job (SIGTERM) or Ctrl-C must still get its "end" record. Ctrl-C already reaches the
    # command through the terminal's process group, so only SIGTERM is forwarded.
    def forward(signum, _frame):
        child.send_signal(signum)
    previous = {signal.SIGTERM: signal.signal(signal.SIGTERM, forward),
                signal.SIGINT: signal.signal(signal.SIGINT, signal.SIG_IGN)}
    try:
        returncode = child.wait()
    finally:
        for s, handler in previous.items():
            signal.signal(s, handler)

    exit_code = returncode if returncode >= 0 else 128 - returncode  # killed by signal N -> 128 + N
    # "nonzero", not "failed": some tools exit non-zero by design (esgf-fix-tool classify: 10, 20, ...).
    status = "ok" if returncode == 0 else "killed" if returncode < 0 else "nonzero"
    _append(log, {**base, "time": _now(), "event": "end", "status": status, "exit_code": exit_code,
                  "duration_seconds": round(time.monotonic() - start, 3)})
    raise typer.Exit(exit_code)


def read_runs(log: Path) -> list[dict]:
    """Merge start/end records into one dict per run, oldest first. A run without "end" is still running
    or was killed without notice (e.g. SIGKILL at a job time limit)."""
    runs: dict[str, dict] = {}
    for line in log.read_text().splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        run = runs.setdefault(record["run_id"], {"status": "no_end_record"})
        if record.pop("event") == "start":
            record["start"] = record.pop("time")
        else:
            record["end"] = record.pop("time")
        run.update(record)
    return list(runs.values())


@app.command("log")
def log_command(
    log: LogOption = DEFAULT_LOG,
    last: Annotated[int, typer.Option(help="number of runs to show")] = 20,
    as_json: Annotated[bool, typer.Option("--json", help="one merged JSON object per run")] = False,
):
    """Show the run log, most recent last."""
    if not log.is_file():
        typer.echo(f"etb: no run log at {log}", err=True)
        raise typer.Exit(1)
    runs = read_runs(log)[-last:]
    if as_json:
        for run in runs:
            typer.echo(json.dumps(run))
        return
    for run in runs:
        image = (run.get("image") or {}).get("build_id", "-")
        duration = run.get("duration_seconds")
        took = f"{duration:.0f}s" if duration is not None else "-"
        exit_code = run.get("exit_code", "-")
        typer.echo(f"{run['run_id']}  {run.get('start', '-')}  {run['status']:13} exit {exit_code!s:3} "
                   f"{took:>6}  build {image}  {' '.join(run.get('argv', []))[:80]}")


if __name__ == "__main__":
    app()

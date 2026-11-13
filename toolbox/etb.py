#!/app/.venv/bin/python
"""etb: esgf-toolbox helper. What is in this image, what was run with it, and where each simulation stands.

\b
    etb versions [--json | --live]          versions of everything in the image (build manifest)
    etb run [--note TEXT] -- COMMAND ...     run a command and record it in the run log
    etb log [--last N] [--json]              show the run log
    etb copy SRC WORK [--dry-run]            copy a simulation to the shared work directory (milestone copied)
    etb done SIMU MILESTONE                  record a workflow milestone: fixed, linked, published, src-removed
    etb todo [SIMU] [--json]                 where each simulation stands, and what to do next

The run log is a JSON lines file (default ./etb-runs.jsonl, or --log / $ETB_LOG). Each run writes a
"start" record before the command and an "end" record after it, so a run killed by a job time limit
still leaves a trace. Every record carries the image build_id, which links it to the version manifest.
Workflow milestones go in the same file; share it ($ETB_LOG in the work directory) so the team sees them.
"""

from __future__ import annotations

import calendar
import json
import os
import pwd
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from datetime import UTC, date, datetime
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
    # $USER is not reliable here: the image sets USER=root, whatever uid the container runs as, and a host
    # uid usually has no name inside the container. The tb wrapper passes the host login as ETB_USER.
    if os.environ.get("ETB_USER"):
        return os.environ["ETB_USER"]
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
        if record.get("event") not in ("start", "end"):  # workflow milestones share the file
            continue
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


# ---------------------------------------------------------------- workflow: copy, done, todo

# Milestones in workflow order, with the next action an operator takes after each one.
MILESTONES = {
    "copied": "deduplicate ATM/SRF, then QA -> fix loop until classify exits 0 (steps 2-5)",
    "fixed": "esgdrs make upgrade --link into the DRS (step 6)",
    "linked": "final QA gate, mapfiles, publication (step 7)",
    "published": "wait for the delay, then rm -r the source (step 8)",
    "src-removed": "nothing: done",
}


def _milestone(log: Path, simu: str, milestone: str, **details) -> dict:
    record = {"event": "milestone", "time": _now(), "simu": simu, "milestone": milestone, "user": _user(),
              "uid": os.getuid(), "host": socket.gethostname(), "image": _image(), **details}
    _append(log, record)
    return record


def read_milestones(log: Path) -> dict[str, list[dict]]:
    """Milestone records per simulation, oldest first."""
    found: dict[str, list[dict]] = {}
    if log.is_file():
        for line in log.read_text().splitlines():
            if line.strip():
                record = json.loads(line)
                if record.get("event") == "milestone":
                    found.setdefault(record["simu"], []).append(record)
    return found


def _today() -> date:
    return datetime.now(UTC).date()


def _add_months(day: date, months: int) -> date:
    month = day.month - 1 + months
    year, month = day.year + month // 12, month % 12 + 1
    return day.replace(year=year, month=month, day=min(day.day, calendar.monthrange(year, month)[1]))


@app.command("copy")
def copy_command(
    src: Annotated[Path, typer.Argument(help="the simulation output directory (source, left untouched)")],
    work: Annotated[Path, typer.Argument(help="the copy in the shared work directory, e.g. .../work/$SIMU")],
    log: LogOption = DEFAULT_LOG,
    simu: Annotated[str | None, typer.Option(help="simulation name; default: the name of WORK")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="show what would be copied, copy nothing")] = False,
):
    """Copy SRC to WORK and record the milestone 'copied'. Safe to run again: files already copied are kept.

    Each file is written under a temporary name and renamed when complete, so an interrupted copy never
    leaves a truncated file under its real name. A file already in WORK with the same size is kept; one with
    a different size is reported and left alone (exit 1)."""
    src, work = src.resolve(), work.resolve()
    simu = simu or work.name
    if not src.is_dir():
        typer.echo(f"etb: {src} is not a directory", err=True)
        raise typer.Exit(2)
    if work.is_relative_to(src) or src.is_relative_to(work):
        typer.echo("etb: SRC and WORK must not contain each other", err=True)
        raise typer.Exit(2)
    previous = [m for m in read_milestones(log).get(simu, []) if m["milestone"] == "copied"]
    if previous:
        typer.echo(f"etb: {simu} was already copied on {previous[-1]['time']} by {previous[-1]['user']} "
                   f"({previous[-1]['files']} files); checking for missing files.")

    copied = kept = 0
    total_bytes = 0
    mismatched, skipped_links = [], []
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src)
        if path.is_symlink():
            skipped_links.append(str(rel))
            continue
        if path.is_dir():
            if not dry_run:
                (work / rel).mkdir(parents=True, exist_ok=True)
            continue
        size = path.stat().st_size
        total_bytes += size
        target = work / rel
        if target.exists():
            if target.stat().st_size == size:
                kept += 1
            else:
                mismatched.append(str(rel))
            continue
        copied += 1
        if dry_run:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(f".{target.name}.etb-partial")
        shutil.copy2(path, partial)
        os.replace(partial, target)

    files = copied + kept + len(mismatched)
    verb = "would copy" if dry_run else "copied"
    typer.echo(f"{simu}: {verb} {copied} file(s), {kept} already there, {len(mismatched)} different, "
               f"{len(skipped_links)} symlink(s) skipped; {files} files, {total_bytes / 1e9:.2f} GB in SRC")
    for rel in mismatched[:20]:
        typer.echo(f"  different size in WORK, left alone: {rel}", err=True)
    for rel in skipped_links[:20]:
        typer.echo(f"  symlink not copied: {rel}", err=True)
    if dry_run:
        return
    if mismatched:
        typer.echo("etb: milestone 'copied' not recorded: fix the files above, then run etb copy again", err=True)
        raise typer.Exit(1)
    _milestone(log, simu, "copied", src=str(src), work=str(work), files=files, bytes=total_bytes,
               symlinks_skipped=len(skipped_links))


@app.command("done")
def done_command(
    simu: Annotated[str, typer.Argument(help="simulation name, as used by etb copy")],
    milestone: Annotated[str, typer.Argument(help="fixed, linked, published or src-removed")],
    log: LogOption = DEFAULT_LOG,
    note: Annotated[str | None, typer.Option(help="free text saved with the milestone")] = None,
):
    """Record that a simulation reached a workflow milestone."""
    order = list(MILESTONES)
    if milestone not in order or milestone == "copied":
        typer.echo(f"etb: unknown milestone {milestone!r}; use one of: {', '.join(order[1:])} "
                   "(copied is recorded by etb copy)", err=True)
        raise typer.Exit(2)
    reached = {m["milestone"] for m in read_milestones(log).get(simu, [])}
    missing = [m for m in order[:order.index(milestone)] if m not in reached]
    if missing:
        typer.echo(f"etb: warning: {simu} has no {', '.join(missing)} milestone yet", err=True)
    _milestone(log, simu, milestone, note=note)
    typer.echo(f"{simu}: {milestone}")


def status(simu: str, records: list[dict], months: int, today: date) -> dict:
    """Where a simulation stands: last milestone, next action, and when its source can be removed."""
    last = max(records, key=lambda r: list(MILESTONES).index(r["milestone"]))
    copied = next((r for r in records if r["milestone"] == "copied"), {})
    result = {"simu": simu, "milestone": last["milestone"], "since": last["time"][:10],
              "next": MILESTONES[last["milestone"]], "src": copied.get("src")}
    published = [r for r in records if r["milestone"] == "published"]
    if published and last["milestone"] == "published":
        when = _add_months(date.fromisoformat(published[-1]["time"][:10]), months)
        result["src_removable_from"] = when.isoformat()
        days = (when - today).days
        result["next"] = (f"rm -r {result['src'] or 'the source'} possible since {when}" if days <= 0
                          else f"rm -r the source possible from {when} (in {days} days)")
    return result


@app.command("todo")
def todo_command(
    simu: Annotated[str | None, typer.Argument(help="one simulation; default: all")] = None,
    log: LogOption = DEFAULT_LOG,
    months: Annotated[int, typer.Option(help="delay after publication before the source can be removed")] = 2,
    as_json: Annotated[bool, typer.Option("--json", help="one JSON object per simulation")] = False,
):
    """Where each simulation stands, from the milestones in the log, and what to do next."""
    found = read_milestones(log)
    names = [simu] if simu else sorted(found)
    if simu and simu not in found:
        typer.echo(f"{simu}: nothing recorded yet (start with: etb copy SRC WORK)")
        return
    if not names:
        typer.echo(f"etb: no milestone in {log} yet (start with: etb copy SRC WORK)")
        return
    today = _today()
    for name in names:
        info = status(name, found[name], months, today)
        if as_json:
            typer.echo(json.dumps(info))
        else:
            typer.echo(f"{name:32} {info['milestone']:12} {info['since']}   -> {info['next']}")


if __name__ == "__main__":
    app()

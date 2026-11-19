#!/app/.venv/bin/python
"""etb: esgf-toolbox helper. What is in this image, what was run with it, and where each simulation stands.

\b
    etb versions [--json | --live]          versions of everything in the image (build manifest)
    etb run [--note TEXT] -- COMMAND ...     run a command and record it in the run log
    etb log [--last N] [--json]              show the run log
    etb copy SRC WORK [--only DIR] [--dry-run]   copy a simulation to the shared work directory (milestone copied)
    etb done SIMU MILESTONE                  record a workflow milestone: fixed, linked, published, src-removed
    etb reopen SIMU [--note TEXT]            start again from step 3 for a new version of a simulation
    etb restart SIMU --from 1|3 [--delete]   start again: from the copy (1) or from QA (3)
    etb abandon SIMU [--delete]              drop a simulation: hidden from etb todo, WORK and RUN removed
    etb todo [SIMU] [--later | --all]        each step of each simulation: done, running, dead, next

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
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
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


HEARTBEAT_SECONDS = 60
ALIVE_WITHIN = 5 * 60  # a run with no "end" record and no heartbeat for this long is dead


def _alive_dir(log: Path) -> Path:
    return log.with_name(f".{log.name}.alive")


class _Heartbeat:
    """Touch .<log>.alive/<run_id> next to the log every minute while a run lasts, so etb todo can tell a
    running command from one killed without notice (SIGKILL at a job time limit). Best effort: a run goes
    on without it if the file cannot be written."""

    def __init__(self, log: Path, run_id: str):
        self.path = _alive_dir(log) / run_id
        self.stop = threading.Event()

    def __enter__(self):
        try:
            self.path.parent.mkdir(exist_ok=True)
            try:
                self.path.parent.chmod(0o2775)  # the team writes there too (only its creator can chmod)
            except OSError:
                pass
            self.path.touch()
        except OSError:
            return self
        threading.Thread(target=self._beat, daemon=True).start()
        return self

    def _beat(self):
        while not self.stop.wait(HEARTBEAT_SECONDS):
            try:
                os.utime(self.path)
            except OSError:
                return

    def __exit__(self, *exc):
        self.stop.set()
        self.path.unlink(missing_ok=True)


def _start_record(log: Path, argv: list[str], note: str | None = None, **extra) -> dict:
    base = {"run_id": uuid.uuid4().hex[:12], "time": _now()}
    _append(log, {**base, "event": "start", "argv": argv, "cwd": os.getcwd(), "user": _user(),
                  "uid": os.getuid(), "host": socket.gethostname(), "image": _image(),
                  "provider": _provider(argv[0]), "note": note, **extra})
    return base


def _end_record(log: Path, base: dict, status: str, exit_code: int, started: float, **extra) -> None:
    _append(log, {**base, "time": _now(), "event": "end", "status": status, "exit_code": exit_code,
                  "duration_seconds": round(time.monotonic() - started, 3), **extra})


@contextmanager
def _recorded(log: Path, argv: list[str], **extra):
    """Record an etb subcommand as a run (start, heartbeat, end), like etb run does for other commands."""
    started = time.monotonic()
    try:
        base = _start_record(log, argv, **extra)
    except OSError as exc:
        typer.echo(f"etb: cannot write the run log {log}: {exc} (use --log PATH)", err=True)
        raise typer.Exit(2) from None
    status, code = "nonzero", 1
    try:
        with _Heartbeat(log, base["run_id"]):
            yield base
        status, code = "ok", 0
    except typer.Exit as exc:
        code = exc.exit_code
        status = "ok" if code == 0 else "nonzero"
        raise
    except KeyboardInterrupt:
        status, code = "killed", 130
        raise
    finally:
        _end_record(log, base, status, code, started)


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
    start = time.monotonic()
    try:
        base = _start_record(log, command, note)
    except OSError as exc:
        typer.echo(f"etb: cannot write the run log {log}: {exc}. Nothing was run (use --log PATH).", err=True)
        raise typer.Exit(2) from None

    env = {**os.environ, "ETB_RUN_ID": base["run_id"]}
    try:
        child = subprocess.Popen(command, env=env)
    except OSError as exc:
        _end_record(log, base, "not_started", 127, start, error=str(exc))
        typer.echo(f"etb: cannot run {command[0]}: {exc}", err=True)
        raise typer.Exit(127) from None

    # A cancelled job (SIGTERM) or Ctrl-C must still get its "end" record. Ctrl-C already reaches the
    # command through the terminal's process group, so only SIGTERM is forwarded.
    def forward(signum, _frame):
        child.send_signal(signum)
    previous = {signal.SIGTERM: signal.signal(signal.SIGTERM, forward),
                signal.SIGINT: signal.signal(signal.SIGINT, signal.SIG_IGN)}
    try:
        with _Heartbeat(log, base["run_id"]):
            returncode = child.wait()
    finally:
        for s, handler in previous.items():
            signal.signal(s, handler)

    exit_code = returncode if returncode >= 0 else 128 - returncode  # killed by signal N -> 128 + N
    # "nonzero", not "failed": some tools exit non-zero by design (esgf-fix-tool classify: 10, 20, ...).
    status = "ok" if returncode == 0 else "killed" if returncode < 0 else "nonzero"
    _end_record(log, base, status, exit_code, start)
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



class _Progress:
    """Copy progress on stderr: rewritten in place on a terminal, one line every 30 s otherwise (job logs)."""

    def __init__(self, files: int, total: int):
        self.files, self.total = files, total
        self.done_files = self.done = 0
        self.start = self.shown = time.monotonic()
        self.tty = sys.stderr.isatty()
        self.every = 1 if self.tty else 30
        self.current = ""

    def add(self, n: int, current: str) -> None:
        self.done += n
        self.current = current
        if time.monotonic() - self.shown >= self.every:
            self.show()

    def file_done(self) -> None:
        self.done_files += 1

    def show(self, end: str = "") -> None:
        self.shown = now = time.monotonic()
        rate = self.done / max(now - self.start, 1e-9)
        left = (self.total - self.done) / rate if rate else 0
        pct = 100 * self.done / self.total if self.total else 100
        line = (f"{pct:5.1f}%  {self.done / 1e9:.1f}/{self.total / 1e9:.1f} GB  "
                f"{self.done_files}/{self.files} files  {rate / 1e6:.0f} MB/s  "
                f"ETA {int(left // 3600)}h{int(left % 3600 // 60):02d}  {self.current}")
        if self.tty:
            width = shutil.get_terminal_size().columns - 1
            sys.stderr.write("\r" + line[:width].ljust(width) + end)
        else:
            sys.stderr.write(line + "\n")
        sys.stderr.flush()

    def close(self) -> None:
        if self.files:
            self.current = ""
            self.show(end="\n")


def _copy_file(src: Path, dst: Path, progress: _Progress, label: str, chunk: int = 16 << 20) -> None:
    """shutil.copy2, in chunks so the progress line moves during a large file."""
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        while block := fin.read(chunk):
            fout.write(block)
            progress.add(len(block), label)
    shutil.copystat(src, dst)


@app.command("copy")
def copy_command(
    src: Annotated[Path, typer.Argument(help="the simulation output directory (source, left untouched)")],
    work: Annotated[Path, typer.Argument(help="the copy in the shared work directory, e.g. .../work/$SIMU")],
    log: LogOption = DEFAULT_LOG,
    simu: Annotated[str | None, typer.Option(help="simulation name; default: the name of WORK")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="show what would be copied, copy nothing")] = False,
    only: Annotated[list[str] | None, typer.Option(
        "--only", help="copy only this sub-folder of SRC, e.g. CMIP7 (not MONITORING); may be repeated")] = None,
):
    """Copy SRC to WORK and record the milestone 'copied'. Safe to run again: files already copied are kept.
    With --only CMIP7, only SRC/CMIP7 is copied, to WORK/CMIP7.

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
    only = sorted(set(only or []))
    bad = [part for part in only if Path(part).name != part or part in ("", ".", "..") or not (src / part).is_dir()]
    if bad:
        typer.echo(f"etb: --only {bad[0]}: not a sub-folder of {src}", err=True)
        raise typer.Exit(2)
    previous = [m for m in read_milestones(log).get(simu, []) if m["milestone"] == "copied"]
    if previous:
        typer.echo(f"etb: {simu} was already copied on {previous[-1]['time']} by {previous[-1]['user']} "
                   f"({previous[-1]['files']} files); checking for missing files.")

    if dry_run:
        _copy_tree(src, work, simu, log, only, dry_run=True)
        return
    argv = ["etb", "copy", str(src), str(work), *(a for part in only for a in ("--only", part))]
    with _recorded(log, argv, simu=simu):
        _copy_tree(src, work, simu, log, only, dry_run=False)


def _copy_tree(src: Path, work: Path, simu: str, log: Path, only: list[str], *, dry_run: bool) -> None:
    # first list everything, so the progress line knows the total; then copy
    if not dry_run:
        typer.echo(f"{simu}: listing {src}{' (' + ', '.join(only) + ' only)' if only else ''} ...", err=True)
    kept = 0
    total_bytes = 0
    mismatched, skipped_links, dirs, to_copy = [], [], [], []
    tops = [src / part for part in only] if only else [src]
    for path in sorted(p for top in tops for p in ([top] if only else []) + list(top.rglob("*"))):
        rel = path.relative_to(src)
        if path.is_symlink():
            skipped_links.append(str(rel))
        elif path.is_dir():
            dirs.append(rel)
        else:
            size = path.stat().st_size
            total_bytes += size
            target = work / rel
            if not target.exists():
                to_copy.append((rel, size))
            elif target.stat().st_size == size:
                kept += 1
            else:
                mismatched.append(str(rel))
    copied = len(to_copy)
    if not dry_run:
        typer.echo(f"{simu}: {copied} file(s) to copy, {sum(s for _, s in to_copy) / 1e9:.2f} GB", err=True)
        for rel in dirs:
            (work / rel).mkdir(parents=True, exist_ok=True)
        progress = _Progress(len(to_copy), sum(s for _, s in to_copy))
        for rel, _size in to_copy:
            target = work / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = target.with_name(f".{target.name}.etb-partial")
            _copy_file(src / rel, partial, progress, str(rel))
            os.replace(partial, target)
            progress.file_done()
        progress.close()

    files = copied + kept + len(mismatched)
    verb = "would copy" if dry_run else "copied"
    typer.echo(f"{simu}: {verb} {copied} file(s), {kept} already there, {len(mismatched)} different, "
               f"{len(skipped_links)} symlink(s) skipped; {files} files, {total_bytes / 1e9:.2f} GB in SRC{'/' + ','.join(only) if only else ''}")
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
               symlinks_skipped=len(skipped_links), only=only or None)


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


# The command a milestone should be backed by in the run log, run between the previous milestone and it.
# (copied is recorded by etb copy itself; src-removed happens outside the image.)
EVIDENCE = {
    "fixed": ("esgf-fix-tool apply", lambda argv: argv[1:2] == ["apply"] and "--dry-run" not in argv, True),
    "linked": ("esgdrs make upgrade --link", lambda argv: argv[1:3] == ["make", "upgrade"] and "--link" in argv, True),
    "published": ("esgmapfile make", lambda argv: argv[1:2] == ["make"], False),
}
EVIDENCE_TOOL = {"fixed": "esgf-fix-tool", "linked": "esgdrs", "published": "esgmapfile"}


def missing_evidence(records: list[dict], runs: list[dict], window: str = "") -> list[str]:
    """Milestones with no matching successful command in the log, e.g. 'linked' but no esgdrs --link run.
    A command counts when it ran between the previous milestone and this one; apply and esgdrs must also
    name the simulation's WORK directory (known from etb copy). esgmapfile works on the DRS, so for
    'published' only the time window applies."""
    order = list(MILESTONES)
    by_name = {}
    for r in sorted(records, key=lambda r: r["time"]):
        by_name.setdefault(r["milestone"], r)  # first time each milestone was reached
    work = by_name.get("copied", {}).get("work")
    warnings = []
    for milestone, (what, matches, needs_work) in EVIDENCE.items():
        if milestone not in by_name:
            continue
        reached = by_name[milestone]["time"]
        earlier = [by_name[m]["time"] for m in order[:order.index(milestone)] if m in by_name]
        since = max([*earlier, window])
        found = any(
            Path(run["argv"][0]).name == EVIDENCE_TOOL[milestone] and matches(run["argv"])
            and run.get("exit_code") == 0 and since <= run.get("start", "") <= reached
            and (not needs_work or not work or any(work in a for a in run["argv"]))
            for run in runs if run.get("argv")
        )
        if not found:
            where = f" on {work}" if needs_work and work else ""
            warnings.append(f"{milestone} without a successful {what}{where} in the log")
    return warnings


# ---------------------------------------------------------------- etb todo: each step of each simulation
# The steps come from the milestones and from the commands in the run log (recorded by tb / etb run).
# Previews (--dry-run, esgdrs make tree, cmip7repack -x) never make a step done; they only move the
# suggested command on to the real one. Each step carries the command to run next, ready to copy-paste.

STEPS = ["copy", "deduplicate", "QA", "fix", "repack", "DRS", "final QA + publication", "rm SRC"]
MILESTONE_STEP = {"copied": 1, "fixed": 5, "linked": 6, "published": 7, "src-removed": 8}
PERMANENT = ("copied", "src-removed")  # done once per simulation, kept by etb reopen
SYMBOL = {"done": "✓", "next": "▶", "running": "⧗", "dead": "†", "failed": "✗", "skipped": "–",
          "waiting": "…", "todo": "·"}
DECISIONS = {0: "clean", 10: "known", 20: "new_rule: rule or review needed, PR on esgf-fix-tool",
             21: "blocked_upstream: tell the simulation team", 30: "no_change: same fixable findings, tool bug",
             40: "checker_error: the checker crashed on some files (classify --show 30 says which)"}
def _under(arg: str, path: str) -> bool:
    return arg == path or arg.startswith(path.rstrip("/") + "/")


def _names(run: dict, simu: str, work: str | None) -> bool:
    """Whether a recorded command is about this simulation: it names WORK, or a path with the simulation
    name as a whole component (e.g. $RUN/qa_1/... = .../_runs/SIMU/qa_1/...)."""
    if run.get("simu") == simu:
        return True
    for arg in run["argv"][1:]:
        for piece in arg.replace("=", " ").split():
            if (work and _under(piece, work)) or simu in Path(piece).parts:
                return True
    return False


def run_kind(argv: list[str], work: str | None) -> tuple[str, bool] | None:
    """What a recorded command is in the workflow, and whether it is only a preview."""
    name, sub = Path(argv[0]).name, argv[1] if len(argv) > 1 else ""
    if name == "etb" and sub == "copy":
        return "copy", "--dry-run" in argv
    if name == "esgf-fix-tool" and sub == "classify":
        return ("final-classify" if "--final" in argv else "classify"), False
    if name == "esgf-fix-tool" and sub in ("deduplicate", "plan", "apply"):
        return sub, "--dry-run" in argv
    if name == "esgqa":  # on WORK: the QA loop; elsewhere (the DRS): the final gate
        return ("qa" if not work or any(_under(a, work) for a in argv[1:]) else "final-qa"), False
    if name == "esgdrs" and argv[1:3] in (["make", "upgrade"], ["make", "tree"]):
        return "drs", argv[2] == "tree"
    if name == "esgmapfile" and sub == "make":
        return "mapfile", False
    if any("cmip7repack" in a for a in argv):  # usually inside sh -c 'find ... | xargs cmip7repack -o'
        tokens = " ".join(argv).replace("'", " ").replace('"', " ").split()
        return "repack", "-o" not in tokens
    return None


def run_state(run: dict, log: Path) -> str:
    """ok, failed (non-zero exit), running (no end record, recent heartbeat) or dead."""
    status = run.get("status")
    if status == "ok":
        return "ok"
    if status == "no_end_record":
        try:
            beat = (_alive_dir(log) / run["run_id"]).stat().st_mtime
        except OSError:
            return "dead"
        return "running" if time.time() - beat < ALIVE_WITHIN else "dead"
    return "dead" if status == "killed" else "failed"


def _job(name: str, simu: str) -> str | None:
    """A site's job command for a heavy step, from $ETB_JOB_QA / $ETB_JOB_COPY ({simu} is replaced), or None.
    E.g. ETB_JOB_QA='SIMU={simu} ccc_msub -r qa-{simu} /path/qa-job.sh' (set by a site's environment script)."""
    template = os.environ.get(f"ETB_JOB_{name}", "").strip()
    return template.replace("{simu}", simu) if template else None


def _duration(seconds: float) -> str:
    return f"{seconds / 3600:.1f} h" if seconds >= 3600 else f"{seconds / 60:.0f} min"


def _apply_progress(run: dict, running: bool = False) -> str:
    """'n/total files (p%), f failed, ~t left' for an apply run: its plan, and its own lines in its fixlog
    (etb_run_id), so a resubmitted run does not count what an earlier one did. '' if unknown."""
    argv, cwd = run["argv"], Path(run.get("cwd") or ".")
    try:
        fixlog = argv[argv.index("--fixlog") + 1] if "--fixlog" in argv[:-1] else "fixlog.jsonl"
        total = len(json.loads((cwd / argv[2]).read_text())["files"])
        mine = [r for r in (json.loads(line) for line in (cwd / fixlog).read_text().splitlines()
                            if run["run_id"] in line) if r.get("etb_run_id") == run["run_id"]]
    except (OSError, ValueError, IndexError, KeyError, TypeError):
        return ""
    done, failed = len(mine), sum(r.get("status") == "failed" for r in mine)
    text = f"{done}/{total} files ({100 * done / total:.0f}%)" if total else f"{done} files"
    text += f", {failed} failed" if failed else ""
    if running and 0 < done < total:
        spent = (datetime.now(UTC) - datetime.fromisoformat(run["start"])).total_seconds()
        text += f", ~{_duration(spent / done * (total - done))} left"
    return text


def _shard(argv: list[str]) -> tuple[int, int] | None:
    """(k, K) of an apply run with --shard k/K, else None."""
    value = argv[argv.index("--shard") + 1] if "--shard" in argv[:-1] else next(
        (a.split("=", 1)[1] for a in argv if a.startswith("--shard=")), None)
    m = re.fullmatch(r"(\d+)/(\d+)", value or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def _plan_path(run: dict) -> str:
    return str(Path(run.get("cwd") or ".") / run["argv"][2]) if len(run["argv"]) > 2 else ""


def _plan_progress(runs: list[dict], running: list[dict]) -> str:
    """'n/total files (p%), f failed, ~t left' over every apply run of one plan (shards, resumes): each run's
    own lines in its fixlog (etb_run_id), the latest record of a file winning. '' if unknown."""
    latest = {}
    try:
        total = len(json.loads(Path(_plan_path(runs[-1])).read_text())["files"])
        for run in runs:
            argv, cwd = run["argv"], Path(run.get("cwd") or ".")
            fixlog = cwd / (argv[argv.index("--fixlog") + 1] if "--fixlog" in argv[:-1] else "fixlog.jsonl")
            if not fixlog.is_file():
                continue
            for line in fixlog.read_text().splitlines():
                if run["run_id"] in line:
                    record = json.loads(line)
                    if record.get("etb_run_id") == run["run_id"]:
                        latest[record["file"]] = record.get("status")
    except (OSError, ValueError, IndexError, KeyError, TypeError):
        return ""
    done, failed = len(latest), sum(status == "failed" for status in latest.values())
    text = f"{done}/{total} files ({100 * done / total:.0f}%)" if total else f"{done} files"
    text += f", {failed} failed" if failed else ""
    rate = 0.0  # files per second of the runs still going on
    for run in running:
        spent = (datetime.now(UTC) - datetime.fromisoformat(run["start"])).total_seconds()
        mine = sum(1 for line in _fixlog_lines(run) if line.get("etb_run_id") == run["run_id"])
        rate += mine / spent if spent > 0 else 0
    if rate and done < total:
        text += f", ~{_duration((total - done) / rate)} left"
    return text


def _fixlog_lines(run: dict) -> list[dict]:
    argv, cwd = run["argv"], Path(run.get("cwd") or ".")
    fixlog = cwd / (argv[argv.index("--fixlog") + 1] if "--fixlog" in argv[:-1] else "fixlog.jsonl")
    try:
        return [json.loads(line) for line in fixlog.read_text().splitlines() if run["run_id"] in line]
    except (OSError, ValueError):
        return []


def _classify_summary(run: dict) -> str:
    """' — fixable 960/8625 · rules or fixes needed 7649/8625 · ...' from the summary esgf-fix-tool >= 0.8 writes
    next to the report (<report>.classify.json); '' if there is none."""
    report = next((a for a in run["argv"][2:] if a.endswith(".json") and not a.startswith("-")), None)
    if report is None:
        return ""
    path = Path(run.get("cwd") or ".") / report
    try:
        summary = json.loads(path.with_name(path.stem + ".classify.json").read_text())
        blocking = summary["blocking"]
        total = blocking["findings"]
    except (OSError, ValueError, KeyError, TypeError):
        return ""
    need = sum(blocking.get(k, 0) for k in ("unknown", "fix_missing", "review"))
    text = f" — fixable {blocking.get('fix', 0)}/{total} · rules or fixes needed {need}/{total}"
    text += f" · upstream {blocking['upstream']}" if blocking.get("upstream") else ""
    known = sum((summary.get("known_checker_errors") or {}).values())
    text += f" · known checker errors {known}" if known else ""
    text += f" · checker crashed on {summary['unknown_checker_errors']} files" if summary.get("unknown_checker_errors") else ""
    return text


def _rerun(argv: list[str]) -> str:
    """The command line that runs a recorded command again, through tb."""
    return "tb " + shlex.join([Path(argv[0]).name, *argv[1:]])


def _output(argv: list[str]) -> str | None:
    for flag in ("-o", "--output"):
        if flag in argv[:-1]:
            return argv[argv.index(flag) + 1]
    return None


def steps_of(simu: str, records: list[dict], runs: list[dict], log: Path, months: int, today: date) -> dict:
    """Each step of one simulation: state, detail, date and the command to run; the current step."""
    # reopen / restart start a new window: steps 3-8 (or, after restart --from 1, every step) only count
    # what happened after it
    resets = sorted((r for r in records if r["milestone"] in ("reopened", "restarted")), key=lambda r: r["time"])
    window = resets[-1]["time"] if resets else ""
    fresh = max((r["time"] for r in resets if r.get("from_step") == 1), default="")
    reached = {}
    for r in sorted(records, key=lambda r: r["time"]):
        if r["milestone"] in MILESTONE_STEP and r["time"] >= (fresh if r["milestone"] in PERMANENT else window):
            reached[r["milestone"]] = r  # the last one
    abandoned = max((r for r in records if r["milestone"] == "abandoned" and r["time"] > window),
                    key=lambda r: r["time"], default=None)
    work = reached.get("copied", {}).get("work")
    found, previews = [], []  # (kind, run, start), oldest first
    for run in sorted(runs, key=lambda r: r.get("start", "")):
        if run.get("argv") and _names(run, simu, work):
            kind = run_kind(run["argv"], work)
            if kind:
                (previews if kind[1] else found).append((kind[0], run, run.get("start", "")))
    # Commands name this simulation's own paths: the shell's $WORK / $RUN may be another simulation's.
    # $QA_TESTS, $PROJECT, $DRS_ROOT, $VERSION, $DRS_SIMU, $COPY_ONLY come from etb-env.sh.
    w = work or "$WORK"
    rd = str(_run_dir(work, simu)) if work else "$RUN"
    src = reached.get("copied", {}).get("src") or "$SRC"
    steps = {i: {"step": i, "name": STEPS[i - 1], "state": "todo", "detail": "", "date": "", "command": ""}
             for i in STEPS_RANGE}

    def put(i, state, detail, source=None, command=""):
        when = (source or {}).get("start") or (source or {}).get("time") or ""
        steps[i].update(state=state, detail=detail, date=when[:10], command=command)

    def from_run(i, run, label, ok=None, fail=None, look=None):
        state = run_state(run, log)
        if state == "ok":
            ok_state, ok_detail, *ok_command = ok or ("done", f"{label} (exit 0)")
            put(i, ok_state, ok_detail, run, ok_command[0] if ok_command else "")
        elif state == "running":
            put(i, "running", f"{label} running since {run['start'][:16].replace('T', ' ')}", run)
        elif state == "dead":
            put(i, "dead", f"{label} stopped with no end record (killed?): run it again", run, _rerun(run["argv"]))
        else:
            put(i, "failed", fail or f"{label} exit {run.get('exit_code')}", run, look or _rerun(run["argv"]))

    def last(*kinds, everywhere=False):  # everywhere: also before reopen (steps 1-2 are done once)
        since = fresh if everywhere else window
        return next(((k, r) for k, r, start in reversed(found) if k in kinds and start >= since), (None, None))

    def previewed(kind, after="", everywhere=False):
        """The latest finished preview of this kind: exit 0, or exit 1 (some files or pairs would fail)."""
        since = max(after, fresh if everywhere else window)
        return next((r for k, r, start in reversed(previews) if k == kind and start >= since and (
            run_state(r, log) == "ok" or (run_state(r, log) == "failed" and r.get("exit_code") == 1))), None)

    # 1-2: once per simulation, whatever the window
    if "copied" in reached:
        put(1, "done", f"copied, {reached['copied'].get('files', '?')} files", reached["copied"])
    elif (run := last("copy", everywhere=True)[1]):
        from_run(1, run, "etb copy", fail="etb copy: files differ in WORK, see its output")
    else:
        check = "tb etb copy $SRC $WORK --only $COPY_ONLY"
        job = _job("COPY", simu)
        put(1, "todo", "copy SRC to WORK: the copy job, then the check (it records the milestone)" if job
            else "copy SRC to WORK (big simulation: a parallel copy job first, then this to check)",
            command=f"{job}   # when it has finished: {check}" if job else check)
    if (run := last("deduplicate", everywhere=True)[1]):
        from_run(2, run, "deduplicate", fail="deduplicate exit 1: some pairs failed, both files kept",
                 look=f"grep '\"status\": \"failed\"' {rd}/deduplicate.jsonl | tail -5")
    elif previewed("deduplicate", everywhere=True):
        put(2, "todo", "dry run done: now for real (apply also deduplicates, so this can be skipped)",
            command=f"tb esgf-fix-tool deduplicate {w} --only-under {w} --fixlog {rd}/deduplicate.jsonl")
    else:
        put(2, "todo", "dry run first", command=f"tb esgf-fix-tool deduplicate {w} --only-under {w} --dry-run "
                                                f"--fixlog {rd}/deduplicate.preview.jsonl")

    # 3-4: the QA -> fix loop, done when classify exits 0
    def report(k):
        return f"{rd}/qa_{k}/qa_result_*[0-9a-f].json"

    qa_job = _job("QA", simu)  # finds the iteration itself, resumes a cut run, then runs classify

    def qa(k):
        return qa_job or f"tb esgqa $QA_TESTS -o {rd}/qa_{k} {w}"

    def classify(k):
        return f"tb esgf-fix-tool classify {report(k)}" + (f" --previous {report(k - 1)}" if k > 1 else "")

    apply_job = _job("APPLY", simu)  # writes the plan if needed, then applies it (DRYRUN=1: dry run)

    def apply(plan, dry):
        if apply_job:
            return ("DRYRUN=1 " if dry else "") + apply_job
        return (f"tb esgf-fix-tool apply {plan} --only-under {w} --workers 8 "
                + (f"--dry-run --fixlog {rd}/fixlog.dry-run.jsonl" if dry else f"--fixlog {rd}/fixlog.jsonl"))

    put(3, "todo", "QA iteration 1" + ("" if qa_job else " (big simulation: better as a job)"), command=qa(1))
    def shard_command(k, total, plan_path):
        if apply_job:
            return f"SHARD={k}/{total} RESUME=1 {apply_job}"
        return (f"tb esgf-fix-tool apply {plan_path} --only-under {w} --workers 8 --shard {k}/{total} "
                f"--resume '{rd}/fixlog*.jsonl' --fixlog {rd}/fixlog.shard-{k}.jsonl")

    def resume_command(plan_path):
        if apply_job:
            return f"RESUME=1 {apply_job}"
        return (f"tb esgf-fix-tool apply {plan_path} --only-under {w} --workers 8 --resume '{rd}/fixlog*.jsonl' "
                f"--fixlog {rd}/fixlog.jsonl")

    def settle(applies, n):
        """Step 4 from every apply run of fix iteration n (one plan): a whole run, a resume, or K shards."""
        if not applies:
            return
        plan_path = _plan_path(applies[-1])
        runs = [r for r in applies if _plan_path(r) == plan_path]
        states = {r["run_id"]: run_state(r, log) for r in runs}
        newest = {}  # only the latest attempt of each shard (or of the whole plan) can still be running
        for r in runs:
            newest[_shard(r["argv"]) or "whole"] = r
        running = [r for r in newest.values() if states[r["run_id"]] == "running"]
        progress = _plan_progress(runs, running)
        suffix = f": {progress}" if progress else ""
        whole = [r for r in runs if _shard(r["argv"]) is None]
        sharded = [r for r in runs if _shard(r["argv"]) is not None]
        total = _shard(sharded[-1]["argv"])[1] if sharded else 0
        latest = {}  # shard k -> its latest run
        for r in sharded:
            if _shard(r["argv"])[1] == total:
                latest[_shard(r["argv"])[0]] = r
        last = runs[-1]

        def finished(r):  # to the end: exit 0, or exit 1 = some files failed (they come back in the next QA)
            return states[r["run_id"]] == "ok" or (states[r["run_id"]] == "failed" and r.get("exit_code") == 1)
        complete = (whole and finished(whole[-1]) and (not sharded or whole[-1] is last)) or (
            total and len(latest) == total and all(finished(r) for r in latest.values()))
        if running:
            what = (f"apply, fix iteration {n}, shard{'s' if len(running) > 1 else ''} "
                    + ", ".join(f"{_shard(r['argv'])[0]}/{total}" for r in running) if sharded and
                    all(_shard(r["argv"]) for r in running) else f"apply, fix iteration {n}")
            put(4, "running", f"{what} running since {running[0]['start'][:16].replace('T', ' ')}{suffix}",
                running[0])
            return
        if complete:
            failures = [r for r in (list(latest.values()) if total else [whole[-1]]) if states[r["run_id"]] != "ok"]
            put(4, "done", f"apply, fix iteration {n} ({'some files failed, they come back in QA' if failures else 'exit 0'})"
                           f"{suffix}", last)
            put(3, "next", f"QA iteration {n + 1}", last, qa(n + 1))
            return
        if sharded and not (whole and whole[-1] is last):
            bad = [(k, r) for k, r in sorted(latest.items()) if states[r["run_id"]] != "ok"]
            missing = [k for k in range(1, total + 1) if k not in latest]
            if bad:
                k, r = bad[0]
                state = states[r["run_id"]]
                why = "stopped with no end record (killed?)" if state == "dead" else f"exit {r.get('exit_code')}"
                put(4, state, f"apply, fix iteration {n}, shard {k}/{total} {why}{suffix}", r,
                    shard_command(k, total, plan_path))
            else:
                put(4, "next", f"fix iteration {n}: shard{'s' if len(missing) > 1 else ''} "
                               f"{', '.join(map(str, missing))} of {total} still to run{suffix}", last,
                    shard_command(missing[0], total, plan_path))
            return
        state = states[last["run_id"]]
        if state == "dead":
            put(4, "dead", f"apply, fix iteration {n} stopped with no end record (killed?): resume it{suffix}",
                last, resume_command(plan_path))
        else:
            put(4, "failed", f"apply, fix iteration {n}: exit {last.get('exit_code')}, some files failed{suffix}",
                last, f"grep '\"status\": \"failed\"' {rd}/fixlog*.jsonl | tail -3")

    n, plan, planned, applies = 0, None, "", []
    for kind, run, start in found:
        if start < window or kind not in ("qa", "classify", "plan", "apply"):
            continue
        number = re.search(r"qa_(\d+)", " ".join(run["argv"]))
        state = run_state(run, log)
        if kind in ("qa", "classify") and applies:
            settle(applies, n)
            applies = []
            plan = None
        if kind == "qa":
            n = int(number.group(1)) if number else n + 1
            from_run(3, run, f"QA iteration {n}", ok=("next", f"iteration {n}: QA done, now classify", classify(n)))
            if state == "dead" and qa_job:  # the job resumes the cut run (esgqa -r)
                steps[3]["command"] = qa_job
            if state == "ok" and steps[4]["state"] == "next":
                put(4, "todo", "")
        elif kind == "classify":
            n = int(number.group(1)) if number else n
            code = run.get("exit_code")
            if state in ("running", "dead"):
                from_run(3, run, f"classify qa_{n}")
            elif code == 0:
                put(3, "done", f"iteration {n}: classify exit 0 (clean){_classify_summary(run)}", run)
                if steps[4]["state"] != "done":
                    put(4, "done", "not needed", run)
            elif code == 10:
                put(3, "done", f"iteration {n}: classify exit 10 (known){_classify_summary(run)}", run)
                if apply_job:
                    put(4, "next", f"fix iteration {n}: plan + dry run, as a job", run, apply(None, dry=True))
                else:
                    put(4, "next", f"fix iteration {n}: plan the fixes", run,
                        f"tb esgf-fix-tool plan {report(n)} -o {rd}/fixplan_{n}.json")
            else:
                put(3, "failed", f"iteration {n}: classify exit {code}, {DECISIONS.get(code, 'error')}"
                                 f"{_classify_summary(run)}", run, f"tb0 esgf-fix-tool classify {report(n)} --show 30")
                if code in (20, 21, 40):  # what the tool knows can be fixed meanwhile (not 30: the fixes don't work)
                    put(4, "next", f"fix iteration {n}: meanwhile, the fixable findings: "
                                   + ("plan + dry run, as a job" if apply_job else "plan the fixes"), run,
                        apply(None, dry=True) if apply_job
                        else f"tb esgf-fix-tool plan {report(n)} -o {rd}/fixplan_{n}.json")
        elif kind == "plan" and state == "ok" and steps[4]["state"] == "next":
            plan, planned = _output(run["argv"]) or f"{rd}/fixplan_{n}.json", start
            put(4, "next", f"fix iteration {n}: fixplan written, dry run first", run, apply(plan, dry=True))
        elif kind == "apply":
            applies.append(run)
    settle(applies, n)
    if applies:
        plan = None
    if plan and steps[4]["state"] == "next" and (dry_done := previewed("apply", after=planned)):
        would_fail = run_state(dry_done, log) == "failed"
        progress = _apply_progress(dry_done) if would_fail else ""
        put(4, "next", f"fix iteration {n}: dry run done"
                       + (f", some files would fail ({progress}): they stay for the next QA" if would_fail else "")
                       + ", now for real", None, apply(plan, dry=False))
    # a dry run still going on (or cut) after the last real apply
    dry = next((r for k, r, start in reversed(previews) if k == "apply" and start >= max(window, planned)), None)
    if dry and steps[4]["state"] in ("next", "todo") and run_state(dry, log) in ("running", "dead"):
        progress = _apply_progress(dry, run_state(dry, log) == "running")
        if run_state(dry, log) == "running":
            put(4, "running", f"apply dry run running since {dry['start'][:16].replace('T', ' ')}"
                              + (f": {progress}" if progress else ""), dry)
        else:
            put(4, "dead", "apply dry run stopped with no end record (killed?): run it again"
                           + (f" ({progress})" if progress else ""), dry, apply(plan, dry=True) if apply_job
                else _rerun(dry["argv"]))

    # 5-8: confirmed by milestones; the log says how far the step went before that
    def repack(option):
        return f"tb sh -c 'find \"$1\" -name \"*.nc\" -print0 | xargs -0 cmip7repack {option}' _ {w}"

    if "fixed" in reached:
        put(5, "done", "milestone fixed", reached["fixed"])
    elif (run := last("repack")[1]):
        from_run(5, run, "cmip7repack", ok=("next", "repacked: record the milestone", f"tb etb done {simu} fixed"))
    elif previewed("repack"):
        put(5, "todo", "preview done: now for real, in place (-o)", command=repack("-o"))
    else:
        put(5, "todo", "preview first (-x)", command=repack("-x"))
    if "linked" in reached:
        put(6, "done", f"milestone linked {reached['linked'].get('note') or ''}".strip(), reached["linked"])
    elif (run := last("drs")[1]):
        if "--link" not in run["argv"]:
            put(6, "failed", "esgdrs make upgrade WITHOUT --link: the files were moved out of WORK", run)
        else:
            from_run(6, run, "esgdrs make upgrade --link",
                     ok=("next", "linked: check a DRS file has 2 links (stat -L -c %h), then record it",
                         f"tb etb done {simu} linked --note v$VERSION"))
    elif previewed("drs"):
        put(6, "todo", "tree checked (set DRS_SIMU from it): now for real, by hard link",
            command=f"tb esgdrs make upgrade -p $PROJECT --root $DRS_ROOT --version $VERSION --link {w}")
    else:
        put(6, "todo", "preview the DRS tree first", command=f"tb esgdrs make tree -p $PROJECT --root $DRS_ROOT {w}")
    final = f"{rd}/qa_final/qa_result_*[0-9a-f].json"
    if "published" in reached:
        put(7, "done", "milestone published", reached["published"])
    else:
        kind, run = last("final-qa", "final-classify", "mapfile")
        if kind == "final-qa":
            from_run(7, run, "final QA", ok=("next", "final QA done: the final gate",
                                             f"tb esgf-fix-tool classify {final} --final"))
        elif kind == "final-classify" and run_state(run, log) == "ok":
            put(7, "next", "final gate passed: the mapfiles", run,
                f"tb esgmapfile make -p $PROJECT --directory $DRS_SIMU --outdir {rd}/mapfiles --version $VERSION")
        elif kind == "final-classify" and run.get("exit_code") not in (None, 0) and run.get("status") != "killed":
            code = run.get("exit_code")
            put(7, "failed", f"classify --final exit {code}, {DECISIONS.get(code, 'error')}", run,
                f"tb0 esgf-fix-tool classify {final} --final --show 30")
        elif kind:
            from_run(7, run, "final QA" if kind != "mapfile" else "esgmapfile make",
                     ok=("next", "mapfiles written: publish from the ESGF VM, then record it",
                         f"tb etb done {simu} published"))
        else:
            put(7, "todo", "final QA on the DRS", command=f"tb esgqa $QA_TESTS -o {rd}/qa_final $DRS_SIMU")
    removable = None
    if "src-removed" in reached:
        put(8, "done", "milestone src-removed", reached["src-removed"])
    elif "published" in reached:
        removable = _add_months(date.fromisoformat(reached["published"]["time"][:10]), months)
        days = (removable - today).days
        if days <= 0:
            put(8, "next", f"the source can be removed since {removable}",
                command=f"rm -r {src} && tb etb done {simu} src-removed")
        else:
            put(8, "waiting", f"rm -r the source possible from {removable} (in {days} days)")

    # a milestone vouches for the steps before it, even those the log does not show
    milestones = [m for m in reached if m != "src-removed"]
    top = max(milestones, key=MILESTONE_STEP.get, default=None)
    for i in range(1, MILESTONE_STEP[top] if top else 1):
        if steps[i]["state"] != "done":
            steps[i].update(state="skipped", command="",
                            detail=f"{steps[i]['detail'] or 'not seen in the log'} (milestone {top} recorded)")

    # something running or dead is what matters now; otherwise the first step not done
    active = [i for i in STEPS_RANGE if steps[i]["state"] in ("running", "dead")]
    current = active[-1] if active else next(
        (i for i in STEPS_RANGE if steps[i]["state"] not in ("done", "skipped")), None)
    if current == 3 and steps[3]["state"] == "failed" and steps[4]["state"] in ("next", "failed"):
        current = 4  # QA blocked on what needs a rule, a fix or a checker: the known fixes go on meanwhile
    for i in STEPS_RANGE:  # one ▶ only: the current step
        if i != current and steps[i]["state"] == "next":
            steps[i]["state"] = "todo"
    command = ""
    if abandoned:
        category = "abandoned"
        next_action = f"abandoned {abandoned['time'][:10]}" + (f": {abandoned['note']}" if abandoned.get("note") else "")
    elif current is None:
        category, next_action = "done", "nothing: done"
    else:
        if steps[current]["state"] == "todo":
            steps[current]["state"] = "next"
        category = "later" if steps[current]["state"] == "waiting" else "current"
        next_action = f"step {current} {STEPS[current - 1]}: {steps[current]['detail']}"
        command = steps[current]["command"]
    latest = max(reached.values(), key=lambda r: r["time"], default={})
    reset = resets[-1] if resets else None
    return {"simu": simu, "category": category, "current": current, "next": next_action, "command": command,
            "milestone": latest.get("milestone"), "since": latest.get("time", "")[:10],
            "src": reached.get("copied", {}).get("src"),
            "src_removable_from": removable.isoformat() if removable else None,
            "reopened": window[:10] or None, "reset": _reset_text(reset),
            "work": reached.get("copied", {}).get("work"), "steps": list(steps.values()),
            "warnings": missing_evidence([r for r in reached.values()], runs, window)}


STEPS_RANGE = range(1, len(STEPS) + 1)


def _reset_text(reset: dict | None) -> str | None:
    if not reset:
        return None
    what = "reopened" if reset["milestone"] == "reopened" else f"restarted from step {reset.get('from_step')}"
    return f"{what} {reset['time'][:10]}"


def _simulations(log: Path, runs: list[dict]) -> dict[str, list[dict]]:
    """Milestones per simulation, plus simulations whose etb copy has started but has no milestone yet."""
    found = read_milestones(log)
    for run in runs:
        if run.get("simu") and run.get("argv", [""])[:2] == ["etb", "copy"]:
            found.setdefault(run["simu"], [])
    return found


@app.command("todo")
def todo_command(
    simu: Annotated[str | None, typer.Argument(help="one simulation, step by step; default: one line each")] = None,
    log: LogOption = DEFAULT_LOG,
    later: Annotated[bool, typer.Option("--later", help="the published ones, waiting to remove the source")] = False,
    show_all: Annotated[bool, typer.Option("--all", help="every simulation, finished and abandoned included")] = False,
    months: Annotated[int, typer.Option(help="delay after publication before the source can be removed")] = 2,
    as_json: Annotated[bool, typer.Option("--json", help="one JSON object per simulation")] = False,
):
    """Where each simulation stands, step by step, and what to do next.

    \b
    Default: one line per simulation that needs something now:   etb todo
    One simulation, every step with its date and evidence:        etb todo SIMU
    Steps: ✓ done  ▶ next  ⧗ running  † dead (no end record, no heartbeat)  ✗ failed or blocked
           – skipped (a later milestone was recorded)  … waiting (delay before rm SRC)  · not yet
    Under each simulation: the command to run next, with its own paths, ready to copy-paste (it uses
    $QA_TESTS, $PROJECT, $DRS_ROOT, $VERSION, $DRS_SIMU from etb-env.sh). A step is read from the
    milestones and from the commands recorded by tb / etb run; previews (--dry-run, esgdrs make tree,
    cmip7repack -x) do not make a step done, they move the command on to the real one. Warns when a milestone has no matching
    command in the log (e.g. 'linked' but no esgdrs --link run)."""
    runs = read_runs(log) if log.is_file() else []
    found = _simulations(log, runs)
    if simu and simu not in found:
        typer.echo(f"{simu}: nothing recorded yet (start with: etb copy SRC WORK)")
        return
    if not found:
        typer.echo(f"etb: no milestone in {log} yet (start with: etb copy SRC WORK)")
        return
    today = _today()
    infos = [steps_of(name, found[name], runs, log, months, today) for name in ([simu] if simu else sorted(found))]
    if not simu:
        wanted = {"current", "later", "done", "abandoned"} if show_all else {"later"} if later else {"current"}
        hidden = [i for i in infos if i["category"] not in wanted]
        infos = [i for i in infos if i["category"] in wanted]
    if as_json:
        for info in infos:
            typer.echo(json.dumps(info))
        return
    if simu:
        info = infos[0]
        since = f"   (last milestone {info['milestone']} {info['since']})" if info["milestone"] else ""
        typer.echo(f"{simu}{since}" + (f"   {info['reset']}" if info["reset"] else "")
                   + ("   ABANDONED" if info["category"] == "abandoned" else ""))
        for step in info["steps"]:
            typer.echo(f"  {SYMBOL[step['state']]} {step['step']} {step['name']:24} {step['date'] or '':10}  "
                       f"{step['detail']}".rstrip())
        typer.echo(f"  -> {info['next']}")
        if info["command"]:
            typer.echo(f"     {info['command']}")
        for warning in info["warnings"]:
            typer.echo(f"  warning: {warning}")
        return
    if not infos:
        typer.echo("nothing to do now" if not later else "nothing waiting")
    for info in infos:
        strip = "".join(SYMBOL[step["state"]] for step in info["steps"])
        typer.echo(f"{info['simu']:32} {strip}  {info['next']}")
        if info["command"]:
            typer.echo(f"{'':33}{info['command']}")
        for warning in info["warnings"]:
            typer.echo(f"{'':32} warning: {warning}")
    waiting = sum(i["category"] == "later" for i in hidden)
    finished = sum(i["category"] in ("done", "abandoned") for i in hidden)
    if waiting or finished:
        typer.echo(f"(+ {waiting} waiting for the delay: etb todo --later; {finished} done or abandoned: "
                   "etb todo --all)")


@app.command("reopen")
def reopen_command(
    simu: Annotated[str, typer.Argument(help="simulation name, as used by etb copy")],
    log: LogOption = DEFAULT_LOG,
    note: Annotated[str | None, typer.Option(help="why, e.g. the new version and what it fixes")] = None,
):
    """Start a simulation again from step 3, for a new version (e.g. a fix after publication, with
    apply --break-hardlinks). From now on, etb todo only counts what happens after this: QA, fix, repack,
    DRS, publication. copy, deduplicate and src-removed are kept."""
    if simu not in read_milestones(log):
        typer.echo(f"etb: {simu} has no milestone in {log}: nothing to reopen", err=True)
        raise typer.Exit(2)
    _milestone(log, simu, "reopened", note=note)
    typer.echo(f"{simu}: reopened; next: QA on $WORK (step 3)")


# ---------------------------------------------------------------- abandon, restart: cleaning up a simulation

def _run_dir(work: str, simu: str) -> Path:
    """$RUN by the workflow's convention: <work directory>/_runs/SIMU, next to WORK."""
    return Path(work).parent / "_runs" / simu


def _not_deletable(path: Path, simu: str, src: str | None) -> str | None:
    """Why this simulation directory must not be removed (or have files removed from it), or None."""
    if not path.is_absolute() or len(path.parts) < 4:
        return "the path is too short to be a simulation directory"
    if path.name != simu:
        return f"its name is not {simu}"
    if src and (path == Path(src) or Path(src).is_relative_to(path) or path.is_relative_to(Path(src))):
        return "it is, or contains, the source"
    return None


def _remove(paths: list[Path], simu: str, src: str | None) -> list[str]:
    """Remove these simulation directories, or entries directly inside one, after checking them all."""
    problems = [(p, why) for p in paths if (why := _not_deletable(p if p.name == simu else p.parent, simu, src))]
    for p, why in problems:
        typer.echo(f"etb: not removing {p}: {why}", err=True)
    if problems:
        raise typer.Exit(2)
    removed = []
    for p in paths:
        if not p.exists():
            typer.echo(f"  {p}: not there")
            continue
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        else:
            p.unlink()
        removed.append(str(p))
        typer.echo(f"  removed {p}")
    return removed


def _known(simu: str, log: Path) -> tuple[list[dict], dict]:
    runs = read_runs(log) if log.is_file() else []
    records = _simulations(log, runs).get(simu)
    if records is None:
        typer.echo(f"etb: {simu} has nothing recorded in {log}", err=True)
        raise typer.Exit(2)
    return records, steps_of(simu, records, runs, log, 2, _today())


DeleteOption = Annotated[bool, typer.Option("--delete", help="also remove the files (see above); never SRC")]
RunOption = Annotated[Path | None, typer.Option("--run", help="the simulation's RUN directory; default: "
                                                              "<work dir>/_runs/SIMU")]


@app.command("abandon")
def abandon_command(
    simu: Annotated[str, typer.Argument(help="simulation name, as used by etb copy")],
    log: LogOption = DEFAULT_LOG,
    note: Annotated[str | None, typer.Option(help="why, e.g. wrong simulation, broken time axis")] = None,
    delete: DeleteOption = False,
    run_dir: RunOption = None,
):
    """Drop a simulation (e.g. the wrong one, or one that must not be published): etb todo no longer
    lists it (etb todo --all still does). With --delete, its WORK and RUN directories are removed too.
    SRC is never touched. --delete is refused once the simulation was linked into the DRS."""
    records, info = _known(simu, log)
    work = info["work"]
    paths = [Path(work), run_dir or _run_dir(work, simu)] if work else []
    removed = []
    if delete:
        if any(r["milestone"] == "linked" for r in records):
            typer.echo(f"etb: {simu} was linked into the DRS: remove its DRS version first; nothing removed",
                       err=True)
            raise typer.Exit(2)
        if not paths:
            typer.echo(f"etb: {simu} has no WORK recorded (no copied milestone): nothing to remove", err=True)
        removed = _remove(paths, simu, info["src"])
    _milestone(log, simu, "abandoned", note=note, removed=removed)
    typer.echo(f"{simu}: abandoned (etb todo --all still shows it)")
    if not delete and paths:
        typer.echo(f"  its files are still there: etb abandon {simu} --delete, or rm -r {' '.join(map(str, paths))}")


@app.command("restart")
def restart_command(
    simu: Annotated[str, typer.Argument(help="simulation name, as used by etb copy")],
    from_step: Annotated[int, typer.Option("--from", help="1: from the copy; 3: from QA")],
    log: LogOption = DEFAULT_LOG,
    note: Annotated[str | None, typer.Option(help="why")] = None,
    delete: DeleteOption = False,
    run_dir: RunOption = None,
):
    """Start a simulation again; etb todo only counts what happens from now on.

    \b
    --from 1  from the copy. Steps 2-5 change WORK in place, so going back means a fresh copy.
              With --delete, WORK and RUN are removed; then run etb copy SRC WORK again.
    --from 3  from QA, keeping the copy and the deduplication. With --delete, the QA iterations are
              removed from RUN (qa_*, fixplan_*, fixlog*). WORK is kept: fixes already applied stay.
    SRC is never touched. Refused once the simulation is linked into the DRS: for a new version of a
    published simulation, use etb reopen."""
    if from_step not in (1, 3):
        typer.echo("etb: --from is 1 (the copy) or 3 (QA)", err=True)
        raise typer.Exit(2)
    records, info = _known(simu, log)
    steps = {s["step"]: s for s in info["steps"]}
    if steps[6]["state"] == "done":
        typer.echo(f"etb: {simu} is linked into the DRS: use etb reopen {simu} for a new version", err=True)
        raise typer.Exit(2)
    if from_step == 1 and steps[8]["state"] == "done":
        typer.echo(f"etb: the source of {simu} was removed: nothing to copy again", err=True)
        raise typer.Exit(2)
    if from_step == 3 and steps[4]["state"] == "done" and steps[4]["detail"] != "not needed":
        typer.echo(f"etb: warning: fixes already applied to WORK are kept; to undo them: etb restart {simu} "
                   "--from 1", err=True)
    work = info["work"]
    removed = []
    if delete and work:
        run = run_dir or _run_dir(work, simu)
        if from_step == 1:
            if any(r["milestone"] == "linked" for r in records):
                typer.echo(f"etb: {simu} was linked into the DRS once: remove its DRS version first; "
                           "nothing removed", err=True)
                raise typer.Exit(2)
            removed = _remove([Path(work), run], simu, info["src"])
        else:
            iterations = sorted(p for pattern in ("qa_*", "fixplan_*", "fixlog*") for p in run.glob(pattern))
            removed = _remove(iterations, simu, info["src"])
    _milestone(log, simu, "restarted", from_step=from_step, note=note, removed=removed)
    if from_step == 1:
        typer.echo(f"{simu}: restarted from the copy; next: etb copy {info['src'] or '$SRC'} {work or '$WORK'}")
    else:
        typer.echo(f"{simu}: restarted from QA; next: QA iteration 1 on {work or '$WORK'} (step 3)")


if __name__ == "__main__":
    app()

#!/app/.venv/bin/python
"""etb: esgf-toolbox helper. What is in this image, what was run with it, and where each simulation stands.

\b
    etb versions [--json | --live]          versions of everything in the image (build manifest)
    etb run [--note TEXT] -- COMMAND ...     run a command and record it in the run log
    etb log [--last N] [--json]              show the run log
    etb copy SRC WORK [--dry-run]            copy a simulation to the shared work directory (milestone copied)
    etb done SIMU MILESTONE                  record a workflow milestone: fixed, linked, published, src-removed
    etb reopen SIMU [--note TEXT]            start again from step 3 for a new version of a simulation
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

    if dry_run:
        _copy_tree(src, work, simu, log, dry_run=True)
        return
    with _recorded(log, ["etb", "copy", str(src), str(work)], simu=simu):
        _copy_tree(src, work, simu, log, dry_run=False)


def _copy_tree(src: Path, work: Path, simu: str, log: Path, *, dry_run: bool) -> None:
    # first list everything, so the progress line knows the total; then copy
    if not dry_run:
        typer.echo(f"{simu}: listing {src} ...", err=True)
    kept = 0
    total_bytes = 0
    mismatched, skipped_links, dirs, to_copy = [], [], [], []
    for path in sorted(src.rglob("*")):
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
# Previews (--dry-run, esgdrs make tree, cmip7repack -x) never count.

STEPS = ["copy", "deduplicate", "QA", "fix", "repack", "DRS", "final QA + publication", "rm SRC"]
MILESTONE_STEP = {"copied": 1, "fixed": 5, "linked": 6, "published": 7, "src-removed": 8}
PERMANENT = ("copied", "src-removed")  # done once per simulation, kept by etb reopen
SYMBOL = {"done": "✓", "next": "▶", "running": "⧗", "dead": "†", "failed": "✗", "skipped": "–",
          "waiting": "…", "todo": "·"}
DECISIONS = {0: "clean", 10: "known", 20: "new_rule: rule or review needed, PR on esgf-fix-tool",
             21: "blocked_upstream: tell the simulation team", 30: "no_change: same fixable findings, tool bug",
             40: "checker_error: QA failed on some files, run it again"}
HINT = {
    1: "tb etb copy $SRC $WORK (or the dsync job, then tb etb copy to check)",
    2: "tb esgf-fix-tool deduplicate $WORK --only-under $WORK --dry-run --fixlog $RUN/deduplicate.preview.jsonl",
    3: "tb esgqa $QA_TESTS -o $RUN/qa_1 $WORK, then classify",
    4: "plan + apply on the last QA report",
    5: "cmip7repack -o on $WORK, then tb etb done SIMU fixed",
    6: "tb esgdrs make upgrade -p $PROJECT --root $DRS_ROOT --version $VERSION --link $WORK",
    7: "esgqa on $DRS_SIMU, classify --final, esgmapfile make, publication",
    8: "rm -r $SRC once the delay after publication is over",
}


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


def steps_of(simu: str, records: list[dict], runs: list[dict], log: Path, months: int, today: date) -> dict:
    """Each step of one simulation: state, detail and date; the current step; what to do next."""
    window = max((r["time"] for r in records if r["milestone"] == "reopened"), default="")
    reached = {}
    for r in sorted(records, key=lambda r: r["time"]):
        if r["milestone"] in MILESTONE_STEP and (r["milestone"] in PERMANENT or r["time"] >= window):
            reached[r["milestone"]] = r  # the last one
    work = reached.get("copied", {}).get("work")
    found = []  # (kind, run, in this window), oldest first
    for run in sorted(runs, key=lambda r: r.get("start", "")):
        if run.get("argv") and _names(run, simu, work):
            kind = run_kind(run["argv"], work)
            if kind and not kind[1]:
                found.append((kind[0], run, run.get("start", "") >= window))
    steps = {i: {"step": i, "name": STEPS[i - 1], "state": "todo", "detail": "", "date": ""} for i in STEPS_RANGE}

    def put(i, state, detail, source=None):
        when = (source or {}).get("start") or (source or {}).get("time") or ""
        steps[i].update(state=state, detail=detail, date=when[:10])

    def from_run(i, run, label, ok=None, fail=None):
        state = run_state(run, log)
        if state == "ok":
            put(i, *(ok or ("done", f"{label} (exit 0)")), run)
        elif state == "running":
            put(i, "running", f"{label} running since {run['start'][:16].replace('T', ' ')}", run)
        elif state == "dead":
            put(i, "dead", f"{label} stopped with no end record (killed?): run it again", run)
        else:
            put(i, "failed", fail or f"{label} exit {run.get('exit_code')}", run)

    def last(*kinds, everywhere=False):
        return next(((k, r) for k, r, inside in reversed(found) if k in kinds and (inside or everywhere)),
                    (None, None))

    # 1-2: once per simulation, whatever the window
    if "copied" in reached:
        put(1, "done", f"copied, {reached['copied'].get('files', '?')} files", reached["copied"])
    elif (run := last("copy", everywhere=True)[1]):
        from_run(1, run, "etb copy", fail="etb copy: files differ in WORK, see its output")
    if (run := last("deduplicate", everywhere=True)[1]):
        from_run(2, run, "deduplicate", fail="deduplicate exit 1: some pairs failed, both files kept "
                                             "(see $RUN/deduplicate.jsonl)")

    # 3-4: the QA -> fix loop, done when classify exits 0
    n = 0
    for kind, run, inside in found:
        if not inside or kind not in ("qa", "classify", "plan", "apply"):
            continue
        number = re.search(r"qa_(\d+)", " ".join(run["argv"]))
        state = run_state(run, log)
        if kind == "qa":
            n = int(number.group(1)) if number else n + 1
            from_run(3, run, f"QA iteration {n}", ok=("next", f"iteration {n}: QA done, run classify on qa_{n}"))
            if state == "ok" and steps[4]["state"] == "next":
                put(4, "todo", "")
        elif kind == "classify":
            n = int(number.group(1)) if number else n
            code = run.get("exit_code")
            if state in ("running", "dead"):
                from_run(3, run, f"classify qa_{n}")
            elif code == 0:
                put(3, "done", f"iteration {n}: classify exit 0 (clean)", run)
                if steps[4]["state"] != "done":
                    put(4, "done", "not needed", run)
            elif code == 10:
                put(3, "done", f"iteration {n}: classify exit 10 (known)", run)
                put(4, "next", f"fix iteration {n}: tb esgf-fix-tool plan $RUN/qa_{n}/qa_result_*[0-9a-f].json "
                               f"-o $RUN/fixplan_{n}.json, then apply", run)
            else:
                put(3, "failed", f"iteration {n}: classify exit {code}, {DECISIONS.get(code, 'error')}", run)
        elif kind == "plan" and state == "ok" and steps[4]["state"] == "next":
            put(4, "next", f"fix iteration {n}: fixplan written, run apply", run)
        elif kind == "apply":
            from_run(4, run, f"apply, fix iteration {n}",
                     fail=f"apply, fix iteration {n}: exit {run.get('exit_code')}, some files failed (fixlog)")
            if state == "ok":
                put(3, "next", f"QA iteration {n + 1}: tb esgqa $QA_TESTS -o $RUN/qa_{n + 1} $WORK", run)

    # 5-8: confirmed by milestones; the log says how far the step went before that
    if "fixed" in reached:
        put(5, "done", "milestone fixed", reached["fixed"])
    elif (run := last("repack")[1]):
        from_run(5, run, "cmip7repack", ok=("next", f"repacked: tb etb done {simu} fixed"))
    if "linked" in reached:
        put(6, "done", f"milestone linked {reached['linked'].get('note') or ''}".strip(), reached["linked"])
    elif (run := last("drs")[1]):
        if "--link" not in run["argv"]:
            put(6, "failed", "esgdrs make upgrade WITHOUT --link: the files were moved out of WORK", run)
        else:
            from_run(6, run, "esgdrs make upgrade --link",
                     ok=("next", f"linked: check stat -L -c %h = 2, then tb etb done {simu} linked"))
    if "published" in reached:
        put(7, "done", "milestone published", reached["published"])
    else:
        kind, run = last("final-qa", "final-classify", "mapfile")
        if kind == "final-qa":
            from_run(7, run, "final QA", ok=("next", "final QA done: classify --final on $RUN/qa_final"))
        elif kind == "final-classify" and run_state(run, log) == "ok":
            put(7, "next", "final gate passed: esgmapfile make", run)
        elif kind == "final-classify" and run.get("exit_code") not in (None, 0) and run.get("status") != "killed":
            code = run.get("exit_code")
            put(7, "failed", f"classify --final exit {code}, {DECISIONS.get(code, 'error')}", run)
        elif kind:
            from_run(7, run, "final QA" if kind != "mapfile" else "esgmapfile make",
                     ok=("next", f"mapfiles written: publish from the ESGF VM, then tb etb done {simu} published"))
    removable = None
    if "src-removed" in reached:
        put(8, "done", "milestone src-removed", reached["src-removed"])
    elif "published" in reached:
        removable = _add_months(date.fromisoformat(reached["published"]["time"][:10]), months)
        days = (removable - today).days
        if days <= 0:
            put(8, "next", f"rm -r {reached.get('copied', {}).get('src') or '$SRC'} possible since {removable}, "
                           f"then tb etb done {simu} src-removed")
        else:
            put(8, "waiting", f"rm -r the source possible from {removable} (in {days} days)")

    # a milestone vouches for the steps before it, even those the log does not show
    milestones = [m for m in reached if m != "src-removed"]
    top = max(milestones, key=MILESTONE_STEP.get, default=None)
    for i in range(1, MILESTONE_STEP[top] if top else 1):
        if steps[i]["state"] != "done":
            steps[i].update(state="skipped", detail=f"{steps[i]['detail'] or 'not seen in the log'} "
                                                    f"(milestone {top} recorded)")

    # something running or dead is what matters now; otherwise the first step not done
    active = [i for i in STEPS_RANGE if steps[i]["state"] in ("running", "dead")]
    current = active[-1] if active else next(
        (i for i in STEPS_RANGE if steps[i]["state"] not in ("done", "skipped")), None)
    for i in STEPS_RANGE:  # one ▶ only: the current step
        if i != current and steps[i]["state"] == "next":
            steps[i]["state"] = "todo"
    if current is None:
        category, next_action = "done", "nothing: done"
    else:
        if steps[current]["state"] == "todo":
            steps[current]["state"] = "next"
        category = "later" if steps[current]["state"] == "waiting" else "current"
        detail = steps[current]["detail"] or HINT[current].replace("SIMU", simu)
        next_action = f"step {current} {STEPS[current - 1]}: {detail}"
    latest = max(reached.values(), key=lambda r: r["time"], default={})
    return {"simu": simu, "category": category, "current": current, "next": next_action,
            "milestone": latest.get("milestone"), "since": latest.get("time", "")[:10],
            "src": reached.get("copied", {}).get("src"),
            "src_removable_from": removable.isoformat() if removable else None,
            "reopened": window[:10] or None, "steps": list(steps.values()),
            "warnings": missing_evidence([r for r in reached.values()], runs, window)}


STEPS_RANGE = range(1, len(STEPS) + 1)


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
    show_all: Annotated[bool, typer.Option("--all", help="every simulation, finished ones included")] = False,
    months: Annotated[int, typer.Option(help="delay after publication before the source can be removed")] = 2,
    as_json: Annotated[bool, typer.Option("--json", help="one JSON object per simulation")] = False,
):
    """Where each simulation stands, step by step, and what to do next.

    \b
    Default: one line per simulation that needs something now:   etb todo
    One simulation, every step with its date and evidence:        etb todo SIMU
    Steps: ✓ done  ▶ next  ⧗ running  † dead (no end record, no heartbeat)  ✗ failed or blocked
           – skipped (a later milestone was recorded)  … waiting (delay before rm SRC)  · not yet
    A step is read from the milestones and from the commands recorded by tb / etb run; previews
    (--dry-run, esgdrs make tree, cmip7repack -x) do not count. Warns when a milestone has no matching
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
        wanted = {"current", "later", "done"} if show_all else {"later"} if later else {"current"}
        hidden = [i for i in infos if i["category"] not in wanted]
        infos = [i for i in infos if i["category"] in wanted]
    if as_json:
        for info in infos:
            typer.echo(json.dumps(info))
        return
    if simu:
        info = infos[0]
        since = f"   (last milestone {info['milestone']} {info['since']})" if info["milestone"] else ""
        typer.echo(f"{simu}{since}" + (f"   reopened {info['reopened']}" if info["reopened"] else ""))
        for step in info["steps"]:
            typer.echo(f"  {SYMBOL[step['state']]} {step['step']} {step['name']:24} {step['date'] or '':10}  "
                       f"{step['detail']}".rstrip())
        typer.echo(f"  -> {info['next']}")
        for warning in info["warnings"]:
            typer.echo(f"  warning: {warning}")
        return
    if not infos:
        typer.echo("nothing to do now" if not later else "nothing waiting")
    for info in infos:
        strip = "".join(SYMBOL[step["state"]] for step in info["steps"])
        typer.echo(f"{info['simu']:32} {strip}  {info['next']}")
        for warning in info["warnings"]:
            typer.echo(f"{'':32} warning: {warning}")
    waiting = sum(i["category"] == "later" for i in hidden)
    finished = sum(i["category"] == "done" for i in hidden)
    if waiting or finished:
        typer.echo(f"(+ {waiting} waiting for the delay: etb todo --later; {finished} done: etb todo --all)")


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


if __name__ == "__main__":
    app()

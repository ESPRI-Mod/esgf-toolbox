"""Unit tests for toolbox/etb.py, run on the host: make unit."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

TOOLBOX = Path(__file__).resolve().parents[1] / "toolbox"
sys.path.insert(0, str(TOOLBOX))
import etb  # noqa: E402
import versions  # noqa: E402

runner = CliRunner()


@pytest.fixture
def manifest(tmp_path, monkeypatch):
    path = tmp_path / "VERSIONS.json"
    path.write_text(json.dumps({"build_id": "abc123def456", "build": {"variant": "slim", "commit": "c0ffee",
                                                                      "date": "2026-11-12T00:00:00Z"}}))
    monkeypatch.setattr(versions, "MANIFEST", path)
    return path


def records(log):
    return [json.loads(line) for line in log.read_text().splitlines()]


def run(log, *command, note=None):
    args = ["run", "--log", str(log)] + (["--note", note] if note else []) + ["--", *command]
    return runner.invoke(etb.app, args)


# --- etb run ---

def test_run_records_start_and_end_and_keeps_the_exit_code(tmp_path, manifest):
    log = tmp_path / "runs.jsonl"
    result = run(log, "sh", "-c", "exit 3", note="first try")
    assert result.exit_code == 3
    start, end = records(log)
    assert start["event"] == "start" and end["event"] == "end" and start["run_id"] == end["run_id"]
    assert start["argv"] == ["sh", "-c", "exit 3"] and start["note"] == "first try"
    assert start["cwd"] == os.getcwd() and start["uid"] == os.getuid() and start["host"]
    assert start["image"] == {"build_id": "abc123def456", "variant": "slim", "commit": "c0ffee",
                              "date": "2026-11-12T00:00:00Z"}
    assert end["status"] == "nonzero" and end["exit_code"] == 3 and end["duration_seconds"] >= 0


def test_successful_run(tmp_path, manifest):
    log = tmp_path / "runs.jsonl"
    assert run(log, "true").exit_code == 0
    assert records(log)[-1]["status"] == "ok"


def test_options_after_the_command_belong_to_the_command(tmp_path, manifest):
    log = tmp_path / "runs.jsonl"
    out = tmp_path / "out.txt"
    assert run(log, "sh", "-c", f'echo "$@" > {out}', "sh", "--log", "--note").exit_code == 0
    assert out.read_text().strip() == "--log --note"


def test_the_command_sees_the_run_id(tmp_path, manifest):
    log = tmp_path / "runs.jsonl"
    out = tmp_path / "id.txt"
    run(log, "sh", "-c", f"echo $ETB_RUN_ID > {out}")
    assert out.read_text().strip() == records(log)[0]["run_id"]


def test_provider_of_a_console_command():
    assert etb._provider(str(Path(sys.prefix) / "bin" / "pytest"))["package"] == "pytest"
    assert etb._provider("sh") is None


def test_missing_command_is_recorded_and_exits_127(tmp_path, manifest):
    log = tmp_path / "runs.jsonl"
    result = run(log, "no-such-command-anywhere")
    assert result.exit_code == 127
    assert records(log)[-1]["status"] == "not_started"


def test_unwritable_log_runs_nothing(tmp_path, manifest):
    marker = tmp_path / "ran"
    result = run(tmp_path / "missing-dir" / "runs.jsonl", "touch", str(marker))
    assert result.exit_code == 2 and "Nothing was run" in result.output
    assert not marker.exists()


def test_works_without_a_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(versions, "MANIFEST", tmp_path / "absent.json")
    log = tmp_path / "runs.jsonl"
    assert run(log, "true").exit_code == 0
    assert records(log)[0]["image"] is None


def test_log_path_from_environment(tmp_path, manifest, monkeypatch):
    log = tmp_path / "from-env.jsonl"
    monkeypatch.setenv("ETB_LOG", str(log))
    assert runner.invoke(etb.app, ["run", "--", "true"]).exit_code == 0
    assert log.is_file()


def test_sigterm_is_forwarded_and_the_run_still_gets_an_end_record(tmp_path, manifest):
    log = tmp_path / "runs.jsonl"
    proc = subprocess.Popen([sys.executable, str(TOOLBOX / "etb.py"), "run", "--log", str(log), "--",
                             "sleep", "30"], env={**os.environ, "ESGF_TOOLBOX_MANIFEST": str(manifest)})
    for _ in range(50):  # wait for the start record
        if log.is_file() and log.read_text():
            break
        time.sleep(0.1)
    time.sleep(0.2)
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=10) == 128 + signal.SIGTERM
    end = records(log)[-1]
    assert end["event"] == "end" and end["status"] == "killed" and end["exit_code"] == 128 + signal.SIGTERM


# --- etb log ---

def test_log_merges_runs_and_flags_runs_without_end(tmp_path, manifest):
    log = tmp_path / "runs.jsonl"
    run(log, "true")
    run(log, "sh", "-c", "exit 1")
    with log.open("a") as out:  # a run killed without notice (SIGKILL): start only
        out.write(json.dumps({"run_id": "deadbeef0000", "time": "2026-11-12T00:00:00Z", "event": "start",
                              "argv": ["esgqa", "/data"]}) + "\n")
    result = runner.invoke(etb.app, ["log", "--log", str(log), "--json"])
    runs = [json.loads(line) for line in result.output.splitlines()]
    assert [r["status"] for r in runs] == ["ok", "nonzero", "no_end_record"]
    assert "start" in runs[0] and "end" in runs[0] and "event" not in runs[0]

    text = runner.invoke(etb.app, ["log", "--log", str(log), "--last", "2"]).output.splitlines()
    assert len(text) == 2 and "exit 1" in text[0] and "no_end_record" in text[1] and "esgqa /data" in text[1]


def test_log_without_file_exits_1(tmp_path):
    result = runner.invoke(etb.app, ["log", "--log", str(tmp_path / "none.jsonl")])
    assert result.exit_code == 1


# --- etb versions ---

def test_versions_delegates_to_the_manifest(manifest, monkeypatch):
    calls = []
    monkeypatch.setattr(versions, "main", lambda argv: calls.append(argv) or 0)
    for args in ([], ["--json"], ["--live"]):
        assert runner.invoke(etb.app, ["versions", *args]).exit_code == 0
    assert calls == [[], ["--json"], ["--live"]]


# --- etb copy / done / todo (workflow milestones) ---

def simulation(root):
    """A small simulation tree with ATM/SRF folders, a nested file and a symlink."""
    for rel, content in (("ATM/Output/ta.nc", b"a" * 10), ("SRF/Output/snc.nc", b"s" * 20), ("readme.txt", b"r")):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(content)
    (root / "link.nc").symlink_to(root / "ATM/Output/ta.nc")
    return root


def milestones(log):
    return [r for r in records(log) if r["event"] == "milestone"]


def test_copy_copies_the_tree_and_records_copied(tmp_path, manifest):
    src, work, log = simulation(tmp_path / "src"), tmp_path / "work" / "SIM01", tmp_path / "runs.jsonl"
    result = runner.invoke(etb.app, ["copy", str(src), str(work), "--log", str(log)])
    assert result.exit_code == 0, result.output
    assert (work / "ATM/Output/ta.nc").read_bytes() == b"a" * 10 and (work / "readme.txt").exists()
    assert not (work / "link.nc").exists() and "1 symlink(s) skipped" in result.output
    assert not list(work.rglob(".*etb-partial"))
    assert "3 file(s) to copy" in result.output and "100.0%" in result.output and "3/3 files" in result.output
    (m,) = milestones(log)
    assert m["simu"] == "SIM01" and m["milestone"] == "copied" and m["src"] == str(src.resolve())
    assert m["files"] == 3 and m["bytes"] == 31 and m["image"]["build_id"] == "abc123def456"


def test_copy_again_keeps_what_is_there_and_says_it_was_done(tmp_path, manifest):
    src, work, log = simulation(tmp_path / "src"), tmp_path / "work" / "SIM01", tmp_path / "runs.jsonl"
    runner.invoke(etb.app, ["copy", str(src), str(work), "--log", str(log)])
    (work / "readme.txt").unlink()
    result = runner.invoke(etb.app, ["copy", str(src), str(work), "--log", str(log)])
    assert result.exit_code == 0 and "was already copied on" in result.output
    assert "copied 1 file(s), 2 already there" in result.output and (work / "readme.txt").exists()


def test_copy_reports_a_different_file_and_records_nothing(tmp_path, manifest):
    src, work, log = simulation(tmp_path / "src"), tmp_path / "work" / "SIM01", tmp_path / "runs.jsonl"
    (work / "ATM/Output").mkdir(parents=True)
    (work / "ATM/Output/ta.nc").write_bytes(b"truncated")
    result = runner.invoke(etb.app, ["copy", str(src), str(work), "--log", str(log)])
    assert result.exit_code == 1 and "different size in WORK, left alone: ATM/Output/ta.nc" in result.output
    assert (work / "ATM/Output/ta.nc").read_bytes() == b"truncated"
    assert not log.exists() or not milestones(log)


def test_copy_dry_run_copies_nothing(tmp_path, manifest):
    src, work, log = simulation(tmp_path / "src"), tmp_path / "work" / "SIM01", tmp_path / "runs.jsonl"
    result = runner.invoke(etb.app, ["copy", str(src), str(work), "--log", str(log), "--dry-run"])
    assert result.exit_code == 0 and "would copy 3 file(s)" in result.output
    assert not work.exists() and not log.exists()


def test_copy_refuses_nested_directories(tmp_path, manifest):
    src = simulation(tmp_path / "src")
    result = runner.invoke(etb.app, ["copy", str(src), str(src / "copy"), "--log", str(tmp_path / "l.jsonl")])
    assert result.exit_code == 2 and "must not contain each other" in result.output


def test_done_records_milestones_and_warns_about_skipped_ones(tmp_path, manifest):
    log = tmp_path / "runs.jsonl"
    result = runner.invoke(etb.app, ["done", "SIM01", "linked", "--log", str(log), "--note", "v20261113"])
    assert result.exit_code == 0 and "no copied, fixed milestone yet" in result.output
    (m,) = milestones(log)
    assert m["milestone"] == "linked" and m["note"] == "v20261113"


def test_done_rejects_unknown_milestones_and_copied(tmp_path, manifest):
    log = tmp_path / "runs.jsonl"
    for name in ("publishd", "copied"):
        assert runner.invoke(etb.app, ["done", "SIM01", name, "--log", str(log)]).exit_code == 2
    assert not log.exists()


def milestone_line(log, simu, milestone, day, **extra):
    with log.open("a") as out:
        out.write(json.dumps({"event": "milestone", "time": f"{day}T10:00:00+00:00", "simu": simu,
                              "milestone": milestone, "user": "op", **extra}) + "\n")


def test_todo_shows_each_simulation_and_the_source_removal_date(tmp_path, monkeypatch):
    log = tmp_path / "runs.jsonl"
    milestone_line(log, "SIM01", "copied", "2026-11-01", src="/ccc/work/src/SIM01")
    milestone_line(log, "SIM01", "fixed", "2026-11-05")
    milestone_line(log, "SIM01", "linked", "2026-11-06")
    milestone_line(log, "SIM01", "published", "2026-11-10")
    milestone_line(log, "SIM02", "copied", "2026-11-12", src="/ccc/work/src/SIM02")
    milestone_line(log, "SIM03", "published", "2026-08-31")
    monkeypatch.setattr(etb, "_today", lambda: etb.date(2026, 11, 13))

    def lines(*args):  # warning lines are indented (no commands in this log)
        output = runner.invoke(etb.app, ["todo", "--log", str(log), *args]).output
        return [line for line in output.splitlines() if not line.startswith(" ")]
    out = lines()  # what needs something now: SIM01 waits for the delay, so it is not listed
    assert len(out) == 3 and "1 waiting for the delay" in out[2]
    assert "SIM02" in out[0] and "step 2 deduplicate" in out[0]
    assert "SIM03" in out[1] and "possible since 2026-10-31" in out[1]   # Aug 31 + 2 months: end of October
    (waiting,) = lines("--later")
    assert "SIM01" in waiting and "from 2027-01-10 (in 58 days)" in waiting
    assert len(lines("--all")) == 3
    info = json.loads(runner.invoke(etb.app, ["todo", "SIM01", "--log", str(log), "--json"]).output)
    assert info["src_removable_from"] == "2027-01-10" and info["src"] == "/ccc/work/src/SIM01"


def test_todo_with_nothing_recorded(tmp_path):
    log = tmp_path / "runs.jsonl"
    assert "no milestone" in runner.invoke(etb.app, ["todo", "--log", str(log)]).output
    milestone_line(log, "SIM01", "copied", "2026-11-01")
    assert "nothing recorded yet" in runner.invoke(etb.app, ["todo", "SIM09", "--log", str(log)]).output


def test_run_log_ignores_milestones(tmp_path, manifest):
    log = tmp_path / "runs.jsonl"
    run(log, "true")
    milestone_line(log, "SIM01", "copied", "2026-11-01")
    runs = [json.loads(line) for line in runner.invoke(etb.app, ["log", "--log", str(log), "--json"]).output.splitlines()]
    assert len(runs) == 1 and runs[0]["status"] == "ok"


def test_user_comes_from_etb_user_when_set(tmp_path, manifest, monkeypatch):
    monkeypatch.setenv("ETB_USER", "ltroussellier")
    log = tmp_path / "runs.jsonl"
    runner.invoke(etb.app, ["done", "SIM01", "fixed", "--log", str(log)])
    run(log, "true")
    assert {r["user"] for r in records(log) if "user" in r} == {"ltroussellier"}


def run_line(log, day, argv, exit_code=0, run_id=None):
    run_id = run_id or f"r{abs(hash((day, tuple(argv)))) % 10**10:010d}"
    with log.open("a") as out:
        out.write(json.dumps({"run_id": run_id, "time": f"{day}T09:00:00+00:00", "event": "start", "argv": argv}) + "\n")
        out.write(json.dumps({"run_id": run_id, "time": f"{day}T09:30:00+00:00", "event": "end",
                              "status": "ok" if exit_code == 0 else "nonzero", "exit_code": exit_code}) + "\n")


def todo_json(log, simu):
    return json.loads(runner.invoke(etb.app, ["todo", simu, "--log", str(log), "--json"]).output)


def test_todo_has_no_warning_when_each_milestone_has_its_command(tmp_path):
    log, work = tmp_path / "runs.jsonl", "/ccc/work/cmip7/work/SIM01"
    milestone_line(log, "SIM01", "copied", "2026-11-01", src="/ccc/src/SIM01", work=work)
    run_line(log, "2026-11-02", ["esgf-fix-tool", "apply", "p.json", "--only-under", work])
    milestone_line(log, "SIM01", "fixed", "2026-11-03")
    run_line(log, "2026-11-04", ["/app/.venv/bin/esgdrs", "make", "upgrade", "-p", "cmip7", "--link", work])
    milestone_line(log, "SIM01", "linked", "2026-11-05")
    run_line(log, "2026-11-06", ["esgmapfile", "make", "-p", "cmip7", "--directory", "/ccc/drs/MIP-DRS7"])
    milestone_line(log, "SIM01", "published", "2026-11-07")
    assert todo_json(log, "SIM01")["warnings"] == []


def test_todo_warns_about_milestones_without_their_command(tmp_path):
    log, work = tmp_path / "runs.jsonl", "/ccc/work/cmip7/work/SIM01"
    milestone_line(log, "SIM01", "copied", "2026-11-01", src="/ccc/src/SIM01", work=work)
    run_line(log, "2026-11-02", ["esgf-fix-tool", "apply", "p.json", "--only-under", work, "--dry-run"])  # dry run
    milestone_line(log, "SIM01", "fixed", "2026-11-03")
    run_line(log, "2026-11-04", ["esgdrs", "make", "upgrade", "-p", "cmip7", work])                       # no --link
    run_line(log, "2026-11-04", ["esgdrs", "make", "upgrade", "-p", "cmip7", "--link", "/other/SIM02"])   # other simu
    milestone_line(log, "SIM01", "linked", "2026-11-05")
    run_line(log, "2026-11-06", ["esgmapfile", "make", "-p", "cmip7"], exit_code=1)                       # failed
    milestone_line(log, "SIM01", "published", "2026-11-07")
    run_line(log, "2026-11-08", ["esgmapfile", "make", "-p", "cmip7"])                                     # too late
    assert todo_json(log, "SIM01")["warnings"] == [
        f"fixed without a successful esgf-fix-tool apply on {work} in the log",
        f"linked without a successful esgdrs make upgrade --link on {work} in the log",
        "published without a successful esgmapfile make in the log",
    ]
    text = runner.invoke(etb.app, ["todo", "--log", str(log), "--all"]).output
    assert text.count("warning:") == 3


def test_todo_warnings_end_to_end_with_etb_run(tmp_path, manifest):
    """etb run records the commands; a fake esgdrs stands for the real one."""
    log, work = tmp_path / "runs.jsonl", tmp_path / "work" / "SIM01"
    src = simulation(tmp_path / "src")
    runner.invoke(etb.app, ["copy", str(src), str(work), "--log", str(log)])
    runner.invoke(etb.app, ["done", "SIM01", "fixed", "--log", str(log)])
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    (bin_dir / "esgdrs").write_text("#!/bin/sh\nexit 0\n"); (bin_dir / "esgdrs").chmod(0o755)
    run(log, str(bin_dir / "esgdrs"), "make", "upgrade", "-p", "cmip7", "--link", str(work))
    runner.invoke(etb.app, ["done", "SIM01", "linked", "--log", str(log)])
    assert todo_json(log, "SIM01")["warnings"] == [
        f"fixed without a successful esgf-fix-tool apply on {work.resolve()} in the log"]


# --- etb todo, step by step ---

WORK = "/data/work/SIM01"
RUN_DIR = "/data/work/_runs/SIM01"


def copied(log, day="2026-11-01"):
    milestone_line(log, "SIM01", "copied", day, src="/data/src/SIM01", work=WORK, files=3)


def steps(log, simu="SIM01"):
    info = todo_json(log, simu)
    return info, {s["step"]: s for s in info["steps"]}


def test_todo_follows_the_qa_fix_loop_and_ignores_previews(tmp_path):
    log = tmp_path / "runs.jsonl"
    copied(log)
    run_line(log, "2026-11-02", ["esgf-fix-tool", "deduplicate", WORK, "--only-under", WORK, "--dry-run"])
    info, s = steps(log)
    assert s[1]["state"] == "done" and s[2]["state"] == "next" and info["current"] == 2   # a dry run is a preview
    run_line(log, "2026-11-02", ["esgf-fix-tool", "deduplicate", WORK, "--only-under", WORK])
    run_line(log, "2026-11-03", ["esgqa", "-t", "cf:1.11", "-o", f"{RUN_DIR}/qa_1", WORK])
    info, s = steps(log)
    assert s[2]["state"] == "done" and s[3]["state"] == "next" and "classify on qa_1" in s[3]["detail"]
    run_line(log, "2026-11-03", ["esgf-fix-tool", "classify", f"{RUN_DIR}/qa_1/qa_result_x.json"], exit_code=10)
    info, s = steps(log)
    assert s[3]["state"] == "done" and s[4]["state"] == "next" and "fixplan_1" in info["next"]
    run_line(log, "2026-11-04", ["esgf-fix-tool", "plan", f"{RUN_DIR}/qa_1/qa_result_x.json", "-o", "p.json"])
    run_line(log, "2026-11-04", ["esgf-fix-tool", "apply", "p.json", "--only-under", WORK, "--dry-run"])
    info, s = steps(log)
    assert s[4]["state"] == "next" and "run apply" in s[4]["detail"]
    run_line(log, "2026-11-05", ["esgf-fix-tool", "apply", "p.json", "--only-under", WORK])
    info, s = steps(log)
    assert s[4]["state"] == "done" and s[3]["state"] == "next" and "qa_2" in info["next"]
    run_line(log, "2026-11-06", ["esgqa", "-o", f"{RUN_DIR}/qa_2", WORK])
    run_line(log, "2026-11-06", ["esgf-fix-tool", "classify", f"{RUN_DIR}/qa_2/qa_result_y.json"])
    info, s = steps(log)
    assert s[3]["detail"] == "iteration 2: classify exit 0 (clean)" and s[4]["state"] == "done"
    assert info["current"] == 5 and info["category"] == "current"
    run_line(log, "2026-11-07", ["sh", "-c", "find $1 | xargs -0 cmip7repack -x", "_", WORK])  # preview
    assert steps(log)[1][5] | {"date": ""} == {"step": 5, "name": "repack", "state": "next", "detail": "", "date": ""}
    run_line(log, "2026-11-07", ["sh", "-c", "find $1 | xargs -0 cmip7repack -o", "_", WORK])
    assert "tb etb done SIM01 fixed" in todo_json(log, "SIM01")["next"]


def test_todo_shows_a_blocking_decision_and_a_move_without_link(tmp_path):
    log = tmp_path / "runs.jsonl"
    copied(log)
    run_line(log, "2026-11-03", ["esgqa", "-o", f"{RUN_DIR}/qa_1", WORK])
    run_line(log, "2026-11-03", ["esgf-fix-tool", "classify", f"{RUN_DIR}/qa_1/r.json"], exit_code=20)
    _, s = steps(log)
    assert s[3]["state"] == "failed" and "new_rule" in s[3]["detail"]
    milestone_line(log, "SIM01", "fixed", "2026-11-05")
    run_line(log, "2026-11-06", ["esgdrs", "make", "upgrade", "-p", "cmip7", WORK])
    _, s = steps(log)
    assert s[3]["state"] == "skipped" and "milestone fixed recorded" in s[3]["detail"]   # the operator decided
    assert s[6]["state"] == "failed" and "WITHOUT --link" in s[6]["detail"]


def test_todo_ignores_other_simulations(tmp_path):
    log = tmp_path / "runs.jsonl"
    copied(log)
    run_line(log, "2026-11-02", ["esgf-fix-tool", "deduplicate", "/data/work/SIM011", "--only-under", "/data/work"])
    assert steps(log)[1][2]["state"] == "next"   # SIM011 is not SIM01


def unfinished_run(log, argv, run_id, *, heartbeat_age=None, **extra):
    with log.open("a") as out:
        out.write(json.dumps({"run_id": run_id, "time": "2026-11-03T09:00:00+00:00", "event": "start",
                              "argv": argv, **extra}) + "\n")
    if heartbeat_age is not None:
        beat = etb._alive_dir(log) / run_id
        beat.parent.mkdir(exist_ok=True)
        beat.touch()
        os.utime(beat, (time.time() - heartbeat_age,) * 2)


@pytest.mark.parametrize("age,state", [(30, "running"), (3600, "dead"), (None, "dead")])
def test_todo_tells_running_from_dead_by_the_heartbeat(tmp_path, age, state):
    log = tmp_path / "runs.jsonl"
    copied(log)
    unfinished_run(log, ["esgqa", "-o", f"{RUN_DIR}/qa_1", WORK], "abc", heartbeat_age=age)
    info, s = steps(log)
    assert s[3]["state"] == state and info["current"] == 3
    strip = runner.invoke(etb.app, ["todo", "--log", str(log)]).output.split()[1]
    assert strip == "✓·" + etb.SYMBOL[state] + "·····"   # running or dead comes first; step 2 not run yet


def test_todo_shows_a_copy_in_progress(tmp_path):
    log = tmp_path / "runs.jsonl"
    unfinished_run(log, ["etb", "copy", "/data/src/SIM05", "/data/work/SIM05"], "cp1", heartbeat_age=10,
                   simu="SIM05")
    info, s = steps(log, "SIM05")
    assert s[1]["state"] == "running" and "etb copy running since 2026-11-03 09:00" in info["next"]


def test_published_waits_then_reopen_starts_again_from_qa(tmp_path, monkeypatch):
    log = tmp_path / "runs.jsonl"
    monkeypatch.setattr(etb, "_today", lambda: etb.date(2026, 11, 20))
    copied(log)
    run_line(log, "2026-11-02", ["esgf-fix-tool", "deduplicate", WORK, "--only-under", WORK])
    run_line(log, "2026-11-03", ["esgqa", "-o", f"{RUN_DIR}/qa_1", WORK])
    run_line(log, "2026-11-03", ["esgf-fix-tool", "classify", f"{RUN_DIR}/qa_1/r.json"])
    for milestone, day in (("fixed", "2026-11-04"), ("linked", "2026-11-05"), ("published", "2026-11-06")):
        milestone_line(log, "SIM01", milestone, day)
    info, s = steps(log)
    assert info["category"] == "later" and s[8]["state"] == "waiting" and s[7]["state"] == "done"
    result = runner.invoke(etb.app, ["reopen", "SIM01", "--log", str(log), "--note", "v2: comment"])
    assert result.exit_code == 0 and "reopened" in result.output
    info, s = steps(log)
    assert info["category"] == "current" and info["current"] == 3 and info["reopened"]
    assert [s[i]["state"] for i in range(1, 9)] == ["done", "done", "next"] + ["todo"] * 5
    assert runner.invoke(etb.app, ["reopen", "SIM09", "--log", str(log)]).exit_code == 2


def test_todo_detail_lists_every_step(tmp_path):
    log = tmp_path / "runs.jsonl"
    copied(log)
    out = runner.invoke(etb.app, ["todo", "SIM01", "--log", str(log)]).output.splitlines()
    assert out[0].startswith("SIM01") and "last milestone copied 2026-11-01" in out[0]
    assert out[1].split()[:3] == ["✓", "1", "copy"] and "copied, 3 files" in out[1]
    assert out[2].split()[:3] == ["▶", "2", "deduplicate"]
    assert out[9].startswith("  -> step 2 deduplicate: tb esgf-fix-tool deduplicate")


def test_copy_and_run_are_recorded_with_a_heartbeat_removed_at_the_end(tmp_path, manifest):
    src, work, log = simulation(tmp_path / "src"), tmp_path / "work" / "SIM01", tmp_path / "runs.jsonl"
    runner.invoke(etb.app, ["copy", str(src), str(work), "--log", str(log)])
    run(log, "true")
    out = runner.invoke(etb.app, ["log", "--log", str(log), "--json"]).output.splitlines()
    runs = [json.loads(line) for line in out]
    assert [r["argv"][:2] for r in runs] == [["etb", "copy"], ["true"]] and {r["status"] for r in runs} == {"ok"}
    assert runs[0]["simu"] == "SIM01"
    assert etb._alive_dir(log).is_dir() and not list(etb._alive_dir(log).iterdir())


# --- etb abandon / etb restart ---

def layout(tmp_path):
    """SRC, WORK and RUN of SIM01 on disk, copied on 2026-11-01, with a QA iteration and logs in RUN."""
    src, work, run_dir = tmp_path / "src" / "SIM01", tmp_path / "work" / "SIM01", tmp_path / "work" / "_runs" / "SIM01"
    for d in (src, work, run_dir / "qa_1"):
        d.mkdir(parents=True)
    for f in (src / "a.nc", work / "a.nc", run_dir / "qa_1" / "qa_result_x.json", run_dir / "fixplan_1.json",
              run_dir / "fixlog.jsonl", run_dir / "deduplicate.jsonl"):
        f.write_text("x")
    log = tmp_path / "work" / "etb-runs.jsonl"
    milestone_line(log, "SIM01", "copied", "2026-11-01", src=str(src), work=str(work), files=1)
    return log, src, work, run_dir


def cli(log, *args):
    return runner.invoke(etb.app, [*args, "--log", str(log)])


def test_abandon_hides_the_simulation_and_keeps_its_files_without_delete(tmp_path):
    log, _src, work, _run_dir = layout(tmp_path)
    result = cli(log, "abandon", "SIM01", "--note", "wrong simulation")
    assert result.exit_code == 0 and "its files are still there" in result.output and work.exists()
    assert "SIM01" not in cli(log, "todo").output and "1 done or abandoned" in cli(log, "todo").output
    info = todo_json(log, "SIM01")
    assert info["category"] == "abandoned" and info["next"].endswith("wrong simulation")
    assert "ABANDONED" in cli(log, "todo", "SIM01").output and "SIM01" in cli(log, "todo", "--all").output


def test_abandon_delete_removes_work_and_run_never_src(tmp_path):
    log, src, work, run_dir = layout(tmp_path)
    result = cli(log, "abandon", "SIM01", "--delete")
    assert result.exit_code == 0, result.output
    assert not work.exists() and not run_dir.exists() and (src / "a.nc").exists()
    assert milestones(log)[-1]["removed"] == [str(work), str(run_dir)]


def test_abandon_delete_is_refused_once_linked_or_outside_the_simulation(tmp_path):
    log, src, work, _run_dir = layout(tmp_path)
    milestone_line(log, "SIM01", "linked", "2026-11-05")
    assert cli(log, "abandon", "SIM01", "--delete").exit_code == 2 and work.exists()
    assert milestones(log)[-1]["milestone"] == "linked"   # nothing recorded
    other = tmp_path / "x"
    milestone_line(other, "SIM02", "copied", "2026-11-01", src=str(src), work=str(tmp_path / "work" / "OTHER"))
    result = cli(other, "abandon", "SIM02", "--delete")
    assert result.exit_code == 2 and "its name is not SIM02" in result.output


def test_restart_from_the_copy_removes_work_and_run_and_starts_again(tmp_path):
    log, src, work, run_dir = layout(tmp_path)
    run_line(log, "2026-11-02", ["esgf-fix-tool", "deduplicate", str(work), "--only-under", str(work)])
    result = cli(log, "restart", "SIM01", "--from", "1", "--delete", "--note", "bad copy")
    assert result.exit_code == 0 and f"next: etb copy {src} {work}" in result.output
    assert not work.exists() and not run_dir.exists() and src.exists()
    info, s = steps(log)
    assert [s[i]["state"] for i in (1, 2)] == ["next", "todo"] and info["reset"].startswith("restarted from step 1")
    milestone_line(log, "SIM01", "copied", "2026-12-01", src=str(src), work=str(work), files=1)
    assert steps(log)[1][1]["state"] == "done"


def test_restart_from_qa_keeps_work_and_the_deduplication(tmp_path):
    log, _src, work, run_dir = layout(tmp_path)
    run_line(log, "2026-11-02", ["esgf-fix-tool", "deduplicate", str(work), "--only-under", str(work)])
    run_line(log, "2026-11-03", ["esgqa", "-o", f"{run_dir}/qa_1", str(work)])
    run_line(log, "2026-11-03", ["esgf-fix-tool", "classify", f"{run_dir}/qa_1/r.json"], exit_code=10)
    run_line(log, "2026-11-04", ["esgf-fix-tool", "apply", "p.json", "--only-under", str(work)])
    result = cli(log, "restart", "SIM01", "--from", "3", "--delete")
    assert result.exit_code == 0 and "fixes already applied to WORK are kept" in result.output
    assert work.exists() and (run_dir / "deduplicate.jsonl").exists()
    assert not any((run_dir / n).exists() for n in ("qa_1", "fixplan_1.json", "fixlog.jsonl"))
    _, s = steps(log)
    assert [s[i]["state"] for i in (1, 2, 3, 4)] == ["done", "done", "next", "todo"]


def test_restart_is_refused_once_linked_and_brings_an_abandoned_simulation_back(tmp_path):
    log, _src, _work, _run_dir = layout(tmp_path)
    assert cli(log, "restart", "SIM01", "--from", "2").exit_code == 2
    cli(log, "abandon", "SIM01")
    cli(log, "restart", "SIM01", "--from", "3")
    assert todo_json(log, "SIM01")["category"] == "current"
    milestone_line(log, "SIM01", "linked", "2099-01-01")   # after the restart
    result = cli(log, "restart", "SIM01", "--from", "3")
    assert result.exit_code == 2 and "use etb reopen" in result.output
    assert cli(log, "abandon", "SIM09").exit_code == 2

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
    assert "SIM03" in out[1] and "removed since 2026-10-31" in out[1]   # Aug 31 + 2 months: end of October
    (waiting,) = lines("--later")
    assert "SIM01" in waiting and "from 2027-01-10 (in 58 days)" in waiting
    assert len(lines("--all")) == 3
    info = json.loads(runner.invoke(etb.app, ["todo", "SIM01", "--log", str(log), "--json"]).output)
    assert info["src_removable_from"] == "2027-01-10" and info["src"] == "/ccc/work/src/SIM01"


def test_todo_with_nothing_recorded(tmp_path):
    log = tmp_path / "runs.jsonl"
    assert "no milestone" in runner.invoke(etb.app, ["todo", "--log", str(log)]).output
    milestone_line(log, "SIM01", "copied", "2026-11-01")
    output = runner.invoke(etb.app, ["todo", "SIM09", "--log", str(log)]).output
    assert "▶ 1 copy" in output and "tb etb copy $SRC $WORK --only $COPY_ONLY" in output


def test_todo_new_simulation_suggests_the_copy_job(tmp_path, monkeypatch):
    log = tmp_path / "runs.jsonl"   # not even created yet
    monkeypatch.setenv("ETB_JOB_COPY", "SIMU={simu} ccc_msub -r dsync-{simu} copy-dsync.sh")
    output = runner.invoke(etb.app, ["todo", "SIM09", "--log", str(log)]).output
    assert "     SIMU=SIM09 ccc_msub -r dsync-SIM09 copy-dsync.sh\n" in output


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
    assert info["command"] == f"tb esgf-fix-tool deduplicate {WORK} --only-under {WORK} --fixlog {RUN_DIR}/deduplicate.jsonl"
    run_line(log, "2026-11-02", ["esgf-fix-tool", "deduplicate", WORK, "--only-under", WORK])
    run_line(log, "2026-11-03", ["esgqa", "-t", "cf:1.11", "-o", f"{RUN_DIR}/qa_1", WORK])
    info, s = steps(log)
    assert s[2]["state"] == "done" and s[3]["state"] == "next" and "now classify" in s[3]["detail"]
    assert info["command"] == f"tb esgf-fix-tool classify {RUN_DIR}/qa_1/qa_result_*[0-9a-f].json"
    run_line(log, "2026-11-03", ["esgf-fix-tool", "classify", f"{RUN_DIR}/qa_1/qa_result_x.json"], exit_code=10)
    info, s = steps(log)
    assert s[3]["state"] == "done" and s[4]["state"] == "next"
    assert info["command"] == f"tb esgf-fix-tool plan {RUN_DIR}/qa_1/qa_result_*[0-9a-f].json -o {RUN_DIR}/fixplan_1.json"
    run_line(log, "2026-11-04", ["esgf-fix-tool", "plan", f"{RUN_DIR}/qa_1/qa_result_x.json", "-o", "p.json"])
    info, s = steps(log)
    assert s[4]["state"] == "next" and "--dry-run" in info["command"] and info["command"].startswith(
        f"tb esgf-fix-tool apply p.json --only-under {WORK} --workers 8")
    run_line(log, "2026-11-04", ["esgf-fix-tool", "apply", "p.json", "--only-under", WORK, "--dry-run"])
    info, s = steps(log)
    assert "dry run done" in s[4]["detail"] and info["command"].endswith(f"--fixlog {RUN_DIR}/fixlog.jsonl")
    run_line(log, "2026-11-05", ["esgf-fix-tool", "apply", "p.json", "--only-under", WORK])
    info, s = steps(log)
    assert s[4]["state"] == "done" and s[3]["state"] == "next"
    assert info["command"] == f"tb esgqa -t wcrp_cmip7:1.0 -t cf:1.11 -o {RUN_DIR}/qa_2 {WORK}"
    run_line(log, "2026-11-06", ["esgqa", "-o", f"{RUN_DIR}/qa_2", WORK])
    assert todo_json(log, "SIM01")["command"].endswith(f"--previous {RUN_DIR}/qa_1/qa_result_*[0-9a-f].json")
    run_line(log, "2026-11-06", ["esgf-fix-tool", "classify", f"{RUN_DIR}/qa_2/qa_result_y.json"])
    info, s = steps(log)
    assert s[3]["detail"] == "iteration 2: classify exit 0 (clean)" and s[4]["state"] == "done"
    assert info["current"] == 5 and info["category"] == "current"
    assert "cmip7repack -x' _ " + WORK in info["command"]
    run_line(log, "2026-11-07", ["sh", "-c", "find $1 | xargs -0 cmip7repack -x", "_", WORK])  # preview
    info, s = steps(log)
    assert s[5]["state"] == "next" and "cmip7repack -o' _ " + WORK in info["command"]
    run_line(log, "2026-11-07", ["sh", "-c", "find $1 | xargs -0 cmip7repack -o", "_", WORK])
    assert todo_json(log, "SIM01")["command"] == "tb etb done SIM01 fixed"


def test_the_real_repack_as_a_job_when_the_site_has_one(tmp_path, monkeypatch):
    log = tmp_path / "runs.jsonl"
    monkeypatch.setenv("ETB_JOB_REPACK", "SIMU={simu} ccc_msub /s/repack-job.sh")
    copied(log)
    run_line(log, "2026-11-02", ["esgf-fix-tool", "deduplicate", WORK, "--only-under", WORK])
    run_line(log, "2026-11-03", ["esgqa", "-o", f"{RUN_DIR}/qa_1", WORK])
    run_line(log, "2026-11-03", ["esgf-fix-tool", "classify", f"{RUN_DIR}/qa_1/r.json"])
    info, _ = steps(log)
    assert info["current"] == 5 and "cmip7repack -x' _ " + WORK in info["command"]  # the preview stays light
    run_line(log, "2026-11-04", ["sh", "-c", "find $1 | xargs -0 cmip7repack -x", "_", WORK])
    assert todo_json(log, "SIM01")["command"] == "SIMU=SIM01 ccc_msub /s/repack-job.sh"
    real = ["sh", "-c", "find $1 | xargs -0 -n 20 -P $2 cmip7repack -o", "_", WORK, "16"]
    run_line(log, "2026-11-05", real, exit_code=1)  # some files failed: the job again
    info, s = steps(log)
    assert s[5]["state"] == "failed" and info["command"] == "SIMU=SIM01 ccc_msub /s/repack-job.sh"
    run_line(log, "2026-11-06", real)
    assert todo_json(log, "SIM01")["command"] == "tb etb done SIM01 fixed"



def repack_shard(log, day, k, total, *, preview=False, run_id, exit_code=0, ended=True, heartbeat_age=None):
    """One shard of repack-job.sh as recorded: sh -c 'xargs ... cmip7repack "$3" < "$1"' _ LIST CORES -o|-x"""
    lst = f"{RUN_DIR}/repack{'-x' if preview else ''}.list.shard-{k}of{total}"
    argv = ["sh", "-c", 'xargs -d "\\n" -n 1 -P "$2" cmip7repack "$3" < "$1"', "_", lst, "16",
            "-x" if preview else "-o"]
    if ended:
        run_line(log, day, argv, exit_code=exit_code, run_id=run_id)
    else:
        unfinished_run(log, argv, run_id, heartbeat_age=heartbeat_age)


def clean_qa(log, monkeypatch):
    monkeypatch.setenv("ETB_JOB_REPACK", "SIMU={simu} ccc_msub /s/repack-job.sh")
    monkeypatch.setenv("ETB_JOB_REPACK_SHARDS", "SIMU={simu} SHARDS=auto bash /s/repack-job.sh")
    copied(log)
    run_line(log, "2026-11-02", ["esgf-fix-tool", "deduplicate", WORK, "--only-under", WORK])
    run_line(log, "2026-11-03", ["esgqa", "-o", f"{RUN_DIR}/qa_1", WORK])
    run_line(log, "2026-11-03", ["esgf-fix-tool", "classify", f"{RUN_DIR}/qa_1/r.json"])


def test_the_repack_preview_and_real_run_over_shards(tmp_path, monkeypatch):
    log = tmp_path / "runs.jsonl"
    clean_qa(log, monkeypatch)
    info, s = steps(log)
    assert "preview first (-x), as jobs" in s[5]["detail"]
    assert info["command"] == "DRYRUN=1 SIMU=SIM01 SHARDS=auto bash /s/repack-job.sh"
    repack_shard(log, "2026-11-04", 1, 2, preview=True, run_id="x1")
    repack_shard(log, "2026-11-04", 2, 2, preview=True, run_id="x2", ended=False, heartbeat_age=10)
    info, s = steps(log)
    assert s[5]["state"] == "running" and "cmip7repack preview (-x), shard 2/2 running" in s[5]["detail"]
    repack_shard(log, "2026-11-04", 2, 2, preview=True, run_id="x2b")
    info, s = steps(log)
    assert "preview done: now for real" in s[5]["detail"]
    assert info["command"] == "SIMU=SIM01 SHARDS=auto bash /s/repack-job.sh"
    repack_shard(log, "2026-11-05", 1, 2, run_id="o1")
    info, s = steps(log)   # shard 2 not run yet: the milestone must wait
    assert s[5]["state"] == "next" and "shard 2/2 still to run" in s[5]["detail"]
    assert info["command"] == "SHARD=2/2 RESUME=1 SIMU=SIM01 ccc_msub /s/repack-job.sh"
    repack_shard(log, "2026-11-05", 2, 2, run_id="o2", exit_code=1)   # some files failed
    info, s = steps(log)
    assert s[5]["state"] == "failed" and "shard 2/2 exit 1" in s[5]["detail"]
    assert info["command"] == "SHARD=2/2 RESUME=1 SIMU=SIM01 ccc_msub /s/repack-job.sh"
    repack_shard(log, "2026-11-06", 2, 2, run_id="o2b")
    info, s = steps(log)
    assert "repacked (2 shards): record the milestone" in s[5]["detail"]
    assert info["command"] == "tb etb done SIM01 fixed"


def test_a_killed_repack_preview_shard_is_run_again(tmp_path, monkeypatch):
    log = tmp_path / "runs.jsonl"
    clean_qa(log, monkeypatch)
    repack_shard(log, "2026-11-04", 1, 3, preview=True, run_id="x1", ended=False, heartbeat_age=3600)
    repack_shard(log, "2026-11-04", 2, 3, preview=True, run_id="x2")
    info, s = steps(log)
    assert s[5]["state"] == "dead" and "shard 1/3 stopped" in s[5]["detail"]
    assert info["command"] == "DRYRUN=1 SHARD=1/3 SIMU=SIM01 ccc_msub /s/repack-job.sh"


JOBS = {"ETB_JOB_QA": "SIMU={simu} ccc_msub /s/qa-job.sh", "ETB_JOB_APPLY": "SIMU={simu} ccc_msub /s/apply-job.sh",
        "ETB_JOB_APPLY_SHARDS": "SIMU={simu} SHARDS=auto bash /s/apply-job.sh",
        "ETB_JOB_REPACK": "SIMU={simu} ccc_msub /s/repack-job.sh",
        "ETB_JOB_REPACK_SHARDS": "SIMU={simu} SHARDS=auto bash /s/repack-job.sh"}


def test_each_job_comes_with_the_same_step_without_a_job(tmp_path, monkeypatch):
    log = tmp_path / "runs.jsonl"
    for name, value in JOBS.items():
        monkeypatch.setenv(name, value)
    copied(log)
    run_line(log, "2026-11-02", ["esgf-fix-tool", "deduplicate", WORK, "--only-under", WORK])
    info = todo_json(log, "SIM01")
    assert info["command"] == "SIMU=SIM01 ccc_msub /s/qa-job.sh"
    assert info["alternative"] == (f"tb esgqa -t wcrp_cmip7:1.0 -t cf:1.11 -o {RUN_DIR}/qa_1 {WORK} && "
                                   f"tb esgf-fix-tool classify {RUN_DIR}/qa_1/qa_result_*[0-9a-f].json")
    run_line(log, "2026-11-03", ["esgqa", "-o", f"{RUN_DIR}/qa_1", WORK])
    run_line(log, "2026-11-03", ["esgf-fix-tool", "classify", f"{RUN_DIR}/qa_1/r.json"], exit_code=10)
    info = todo_json(log, "SIM01")
    assert info["command"] == "DRYRUN=1 SIMU=SIM01 SHARDS=auto bash /s/apply-job.sh"
    assert info["alternative"] == (
        f"tb esgf-fix-tool plan {RUN_DIR}/qa_1/qa_result_*[0-9a-f].json -o {RUN_DIR}/fixplan_1.json && "
        f"tb esgf-fix-tool apply {RUN_DIR}/fixplan_1.json --only-under {WORK} --workers 8 --dry-run "
        f"--fixlog {RUN_DIR}/fixlog.dry-run.jsonl")
    assert not info["go_on"]
    lines = [line.strip() for line in runner.invoke(etb.app, ["todo", "SIM01", "--log", str(log)]).output.splitlines()]
    # each command under its title, the usual one last
    assert lines[-4:] == ["without a job (a short step, on the login node):", info["alternative"],
                          "next, if everything is normal:", info["command"]]


def test_a_blocked_qa_offers_to_go_on(tmp_path, monkeypatch):
    log = tmp_path / "runs.jsonl"
    for name, value in JOBS.items():
        monkeypatch.setenv(name, value)
    copied(log)
    run_line(log, "2026-11-02", ["esgf-fix-tool", "deduplicate", WORK, "--only-under", WORK])
    run_line(log, "2026-11-03", ["esgqa", "-o", f"{RUN_DIR}/qa_3", WORK])
    run_line(log, "2026-11-03", ["esgf-fix-tool", "classify", f"{RUN_DIR}/qa_3/r.json"], exit_code=20)
    info, s = steps(log)
    assert s[3]["state"] == "failed"
    assert info["go_on"] == "DRYRUN=1 SIMU=SIM01 SHARDS=auto bash /s/repack-job.sh"
    lines = [line.strip() for line in runner.invoke(etb.app, ["todo", "SIM01", "--log", str(log)]).output.splitlines()]
    # going on first, the same step without a job second, the next command last
    assert lines[-6:] == ["if you decide to go on (your call):", "DRYRUN=1 SIMU=SIM01 SHARDS=auto bash /s/repack-job.sh",
                          "without a job (a short step, on the login node):", info["alternative"],
                          "next, if everything is normal:", info["command"]]
    # once going on: the preview recorded, the QA still ✗, the real repack next
    run_line(log, "2026-11-04", ["sh", "-c", "find $1 | xargs -0 cmip7repack -x", "_", WORK])
    info, s = steps(log)
    assert s[3]["state"] == "failed" and info["current"] == 5 and not info["go_on"]
    assert info["command"] == "SIMU=SIM01 SHARDS=auto bash /s/repack-job.sh"
    assert info["alternative"].startswith("tb sh -c 'find")


def test_a_running_step_gets_no_suggestion_at_all(tmp_path, monkeypatch):
    log = tmp_path / "runs.jsonl"
    for name, value in JOBS.items():
        monkeypatch.setenv(name, value)
    plan = sharded_iteration(tmp_path, log)
    # a blocked QA (exit 20) whose known fixes are being applied: no "go on", no command, IN PROGRESS
    run_line(log, "2026-11-02", ["esgf-fix-tool", "classify", f"{RUN_DIR}/qa_1/r.json"], exit_code=20)
    shard_run(log, tmp_path, plan, 1, 2, ["/f0.nc"], run_id="s1", ended=False, heartbeat_age=10)
    info, s = steps(log)
    assert s[4]["state"] == "running" and info["next"].startswith("IN PROGRESS — step 4 fix: apply")
    assert not info["command"] and not info["alternative"] and not info["go_on"]
    output = runner.invoke(etb.app, ["todo", "SIM01", "--log", str(log)]).output
    assert "IN PROGRESS" in output and "go on" not in output and "repack-job" not in output


def test_a_line_separates_the_report_from_what_to_do(tmp_path):
    log = tmp_path / "runs.jsonl"
    copied(log)
    lines = runner.invoke(etb.app, ["todo", "SIM01", "--log", str(log)]).output.splitlines()
    arrow = next(i for i, line in enumerate(lines) if line.startswith("  -> "))
    assert set(lines[arrow - 1]) == {"-"} and len(lines[arrow - 1]) >= 40
    assert lines[arrow - 2].lstrip().startswith(("·", "✓", "▶")) and "10 rm SRC" in lines[arrow - 2]

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
    assert strip == "✓·" + etb.SYMBOL[state] + "·······"   # running or dead comes first; step 2 not run yet


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
    assert info["category"] == "later" and s[10]["state"] == "waiting" and s[9]["state"] == "done"
    result = runner.invoke(etb.app, ["reopen", "SIM01", "--log", str(log), "--note", "v2: comment"])
    assert result.exit_code == 0 and "reopened" in result.output
    info, s = steps(log)
    assert info["category"] == "current" and info["current"] == 3 and info["reopened"]
    assert [s[i]["state"] for i in range(1, 11)] == ["done", "done", "next"] + ["todo"] * 7
    assert runner.invoke(etb.app, ["reopen", "SIM09", "--log", str(log)]).exit_code == 2


def test_todo_detail_lists_every_step(tmp_path):
    log = tmp_path / "runs.jsonl"
    copied(log)
    out = runner.invoke(etb.app, ["todo", "SIM01", "--log", str(log)]).output.splitlines()
    assert out[0].startswith("SIM01") and "last milestone copied 2026-11-01" in out[0]
    assert out[1].split()[:3] == ["✓", "1", "copy"] and "copied, 3 files" in out[1]
    assert out[2].split()[:3] == ["▶", "2", "deduplicate"]
    assert set(out[11]) == {"-"}  # the report above, what to do below
    assert out[12] == "  -> step 2 deduplicate: dry run first"
    assert out[13] == (f"     tb esgf-fix-tool deduplicate {WORK} --only-under {WORK} --dry-run "
                       f"--fixlog {RUN_DIR}/deduplicate.preview.jsonl")


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


def test_copy_only_copies_the_named_sub_folders(tmp_path, manifest):
    src, work, log = simulation(tmp_path / "src"), tmp_path / "work" / "SIM01", tmp_path / "runs.jsonl"
    (src / "MONITORING").mkdir()
    (src / "MONITORING" / "m.nc").write_bytes(b"m" * 5)
    result = runner.invoke(etb.app, ["copy", str(src), str(work), "--only", "ATM", "--log", str(log)])
    assert result.exit_code == 0, result.output
    assert (work / "ATM/Output/ta.nc").exists() and not (work / "SRF").exists() and not (work / "MONITORING").exists()
    assert "copied 1 file(s)" in result.output and "in SRC/ATM" in result.output
    (m,) = milestones(log)
    assert m["only"] == ["ATM"] and m["files"] == 1 and m["work"] == str(work.resolve())
    again = runner.invoke(etb.app, ["copy", str(src), str(work), "--only", "ATM", "--log", str(log)])
    assert "copied 0 file(s), 1 already there" in again.output   # MONITORING is not missing: not asked for
    runs = [json.loads(line) for line in runner.invoke(etb.app, ["log", "--log", str(log), "--json"]).output.splitlines()]
    assert runs[0]["argv"][-2:] == ["--only", "ATM"]


@pytest.mark.parametrize("part", ["NOPE", "..", "ATM/Output"])
def test_copy_only_refuses_anything_but_a_sub_folder(tmp_path, part):
    src = simulation(tmp_path / "src")
    result = runner.invoke(etb.app, ["copy", str(src), str(tmp_path / "w" / "SIM01"), "--only", part,
                                     "--log", str(tmp_path / "l.jsonl")])
    assert result.exit_code == 2 and "not a sub-folder" in result.output


def test_todo_gives_the_command_of_every_later_step(tmp_path, monkeypatch):
    log = tmp_path / "runs.jsonl"
    monkeypatch.setattr(etb, "_today", lambda: etb.date(2027, 3, 1))
    copied(log)
    milestone_line(log, "SIM01", "fixed", "2026-11-05")

    def command():
        return todo_json(log, "SIM01")["command"]
    assert command() == f"tb esgdrs make tree -p $PROJECT --root $DRS_ROOT {WORK}"
    run_line(log, "2026-11-06", ["esgdrs", "make", "tree", "-p", "cmip7", "--root", "/drs", WORK])
    assert command() == f"tb esgdrs make upgrade -p $PROJECT --root $DRS_ROOT --version $VERSION --link {WORK}"
    run_line(log, "2026-11-06", ["esgdrs", "make", "upgrade", "-p", "cmip7", "--link", WORK])
    assert command() == "tb etb done SIM01 linked --note v$VERSION"
    milestone_line(log, "SIM01", "linked", "2026-11-07")
    assert command() == f"tb esgqa -t wcrp_cmip7:1.0 -t cf:1.11 -b /files/ -o {RUN_DIR}/qa_final $DRS_SIMU"
    run_line(log, "2026-11-08", ["esgqa", "-o", f"{RUN_DIR}/qa_final", "/drs/MIP-DRS7/x"])
    assert command() == f"tb esgf-fix-tool classify {RUN_DIR}/qa_final/qa_result_*[0-9a-f].json --final"
    run_line(log, "2026-11-08", ["esgf-fix-tool", "classify", f"{RUN_DIR}/qa_final/r.json", "--final"], exit_code=20)
    assert command().startswith("tb0 esgf-fix-tool classify") and command().endswith("--final --show 30")
    run_line(log, "2026-11-09", ["esgf-fix-tool", "classify", f"{RUN_DIR}/qa_final/r.json", "--final"])
    assert command() == f"tb esgmapfile make -p $PROJECT --directory $DRS_SIMU --outdir {WORK}/Mapfiles --max-processes 8"
    assert todo_json(log, "SIM01")["current"] == 8
    run_line(log, "2026-11-09", ["esgmapfile", "make", "--directory", "/drs/x", "--outdir", f"{WORK}/Mapfiles"])
    info = todo_json(log, "SIM01")
    assert info["current"] == 9 and info["command"] == "tb etb done SIM01 published"
    assert "publish from the ESGF VM with " + f"{WORK}/Mapfiles/*.map" in info["next"]
    milestone_line(log, "SIM01", "published", "2026-11-10")
    assert command() == "rm -r /data/src/SIM01 && tb etb done SIM01 src-removed"


def test_todo_replays_a_dead_or_failed_command_and_points_to_the_failures(tmp_path):
    log = tmp_path / "runs.jsonl"
    copied(log)
    argv = ["/app/.venv/bin/esgqa", "-t", "cf:1.11", "-o", f"{RUN_DIR}/qa_1", WORK]
    unfinished_run(log, argv, "dead1", heartbeat_age=3600)
    assert todo_json(log, "SIM01")["command"] == f"tb esgqa -t cf:1.11 -o {RUN_DIR}/qa_1 {WORK}"
    run_line(log, "2026-11-04", ["esgf-fix-tool", "deduplicate", WORK, "--only-under", WORK], exit_code=1)
    _, s = steps(log)
    assert s[2]["state"] == "failed" and s[2]["command"].startswith("grep") and "deduplicate.jsonl" in s[2]["command"]


def test_todo_suggests_the_site_jobs_for_heavy_steps_when_configured(tmp_path, monkeypatch):
    log = tmp_path / "runs.jsonl"
    monkeypatch.setenv("ETB_JOB_COPY", "SIMU={simu} ccc_msub -r dsync-{simu} /s/copy-dsync.sh")
    monkeypatch.setenv("ETB_JOB_QA", "SIMU={simu} ccc_msub -r qa-{simu} /s/qa-job.sh")
    unfinished_run(log, ["etb", "copy", "/data/src/SIM05", "/data/work/SIM05"], "cp1", simu="SIM05")  # dead
    milestone_line(log, "SIM06", "fixed", "2026-11-01")   # a known simulation with no copy recorded
    copied(log)
    assert todo_json(log, "SIM01")["command"].startswith(f"tb esgf-fix-tool deduplicate {WORK}")  # not heavy
    run_line(log, "2026-11-02", ["esgf-fix-tool", "deduplicate", WORK, "--only-under", WORK])
    assert todo_json(log, "SIM01")["command"] == "SIMU=SIM01 ccc_msub -r qa-SIM01 /s/qa-job.sh"
    unfinished_run(log, ["esgqa", "-o", f"{RUN_DIR}/qa_1", WORK], "qa1", heartbeat_age=3600)   # dead: the job resumes
    info = todo_json(log, "SIM01")
    assert info["current"] == 3 and info["command"] == "SIMU=SIM01 ccc_msub -r qa-SIM01 /s/qa-job.sh"
    run_line(log, "2026-11-05", ["esgf-fix-tool", "apply", "p.json", "--only-under", WORK])
    assert todo_json(log, "SIM01")["command"] == "SIMU=SIM01 ccc_msub -r qa-SIM01 /s/qa-job.sh"   # QA N+1


def test_todo_suggests_the_copy_job_alone(tmp_path, monkeypatch):
    log = tmp_path / "runs.jsonl"
    milestone_line(log, "SIM07", "abandoned", "2026-11-01")
    milestone_line(log, "SIM07", "restarted", "2026-11-02", from_step=1)
    assert todo_json(log, "SIM07")["command"] == "tb etb copy $SRC $WORK --only $COPY_ONLY"
    monkeypatch.setenv("ETB_JOB_COPY", "SIMU={simu} ccc_msub copy-dsync.sh")
    assert todo_json(log, "SIM07")["command"] == "SIMU=SIM07 ccc_msub copy-dsync.sh"  # it runs the check itself


def plan_and_fixlog(tmp_path, run_id, done=2, other=1, total=3):
    plan, fixlog = tmp_path / "fixplan_1.json", tmp_path / "fixlog.jsonl"
    plan.write_text(json.dumps({"files": {f"/f{i}.nc": [] for i in range(total)}}))
    lines = [{"etb_run_id": run_id, "file": f"/f{i}.nc", "status": "failed" if i == 0 else "done"} for i in range(done)]
    lines += [{"etb_run_id": "older", "file": "/f2.nc", "status": "done"} for _ in range(other)]   # not this run
    fixlog.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return str(plan), str(fixlog)


@pytest.mark.parametrize("age,state", [(30, "running"), (3600, "dead")])
def test_todo_shows_how_far_a_running_or_dead_apply_got(tmp_path, age, state):
    log = tmp_path / "runs.jsonl"
    copied(log)
    plan, fixlog = plan_and_fixlog(tmp_path, "ap1")
    unfinished_run(log, ["esgf-fix-tool", "apply", plan, "--only-under", WORK, "--fixlog", fixlog], "ap1",
                   heartbeat_age=age)
    info, s = steps(log)
    assert s[4]["state"] == state and info["current"] == 4
    assert "2/3 files (67%), 1 failed" in s[4]["detail"]
    assert ("left" in s[4]["detail"]) == (state == "running")


def test_todo_shows_a_running_dry_run_with_its_progress(tmp_path):
    log = tmp_path / "runs.jsonl"
    copied(log)
    run_line(log, "2026-11-02", ["esgf-fix-tool", "deduplicate", WORK, "--only-under", WORK])
    run_line(log, "2026-11-02", ["esgqa", "-o", f"{RUN_DIR}/qa_1", WORK])
    run_line(log, "2026-11-02", ["esgf-fix-tool", "classify", f"{RUN_DIR}/qa_1/r.json"], exit_code=10)
    plan, fixlog = plan_and_fixlog(tmp_path, "dry1", done=1, other=0)
    run_line(log, "2026-11-02", ["esgf-fix-tool", "plan", f"{RUN_DIR}/qa_1/r.json", "-o", plan])   # before the dry run
    unfinished_run(log, ["esgf-fix-tool", "apply", plan, "--only-under", WORK, "--dry-run", "--fixlog", fixlog],
                   "dry1", heartbeat_age=10)
    _, s = steps(log)
    assert s[4]["state"] == "running" and "dry run running" in s[4]["detail"] and "1/3 files" in s[4]["detail"]


def test_todo_suggests_the_apply_job_dry_run_then_for_real(tmp_path, monkeypatch):
    log = tmp_path / "runs.jsonl"
    job = "SIMU={simu} ccc_msub -r apply-{simu} /s/apply-job.sh"
    monkeypatch.setenv("ETB_JOB_APPLY", job)
    copied(log)
    run_line(log, "2026-11-02", ["esgf-fix-tool", "deduplicate", WORK, "--only-under", WORK])
    run_line(log, "2026-11-03", ["esgqa", "-o", f"{RUN_DIR}/qa_1", WORK])
    run_line(log, "2026-11-03", ["esgf-fix-tool", "classify", f"{RUN_DIR}/qa_1/r.json"], exit_code=10)
    real = job.replace("{simu}", "SIM01")
    assert todo_json(log, "SIM01")["command"] == "DRYRUN=1 " + real   # the job writes the plan itself
    run_line(log, "2026-11-04", ["esgf-fix-tool", "plan", f"{RUN_DIR}/qa_1/r.json", "-o", f"{RUN_DIR}/fixplan_1.json"])
    assert todo_json(log, "SIM01")["command"] == "DRYRUN=1 " + real
    run_line(log, "2026-11-04", ["esgf-fix-tool", "apply", f"{RUN_DIR}/fixplan_1.json", "--only-under", WORK,
                                 "--dry-run"])
    assert todo_json(log, "SIM01")["command"] == real
    unfinished_run(log, ["esgf-fix-tool", "apply", f"{RUN_DIR}/fixplan_1.json", "--only-under", WORK], "ap2",
                   heartbeat_age=3600)
    info = todo_json(log, "SIM01")
    assert info["current"] == 4 and info["command"] == "RESUME=1 " + real   # dead: resume with the job


def sharded_iteration(tmp_path, log, total=4):
    """SIM01 at fix iteration 1 (classify exit 10), with a plan of `total` files."""
    copied(log)
    run_line(log, "2026-11-02", ["esgf-fix-tool", "deduplicate", WORK, "--only-under", WORK])
    run_line(log, "2026-11-02", ["esgqa", "-o", f"{RUN_DIR}/qa_1", WORK])
    run_line(log, "2026-11-02", ["esgf-fix-tool", "classify", f"{RUN_DIR}/qa_1/r.json"], exit_code=10)
    plan = tmp_path / "fixplan_1.json"
    plan.write_text(json.dumps({"files": {f"/f{i}.nc": [] for i in range(total)}}))
    return str(plan)


def shard_run(log, tmp_path, plan, k, total, files, *, run_id, exit_code=0, ended=True, heartbeat_age=None):
    fixlog = tmp_path / f"fixlog.shard-{k}.jsonl"
    with fixlog.open("a") as out:
        for f in files:
            out.write(json.dumps({"etb_run_id": run_id, "file": f, "status": "done"}) + "\n")
    argv = ["esgf-fix-tool", "apply", plan, "--only-under", WORK, "--shard", f"{k}/{total}", "--fixlog", str(fixlog)]
    if ended:
        run_line(log, "2026-11-03", argv, exit_code=exit_code, run_id=run_id)
    else:
        unfinished_run(log, argv, run_id, heartbeat_age=heartbeat_age)


def test_todo_waits_for_every_shard_then_suggests_the_next_qa(tmp_path, monkeypatch):
    log = tmp_path / "runs.jsonl"
    monkeypatch.setenv("ETB_JOB_APPLY", "SIMU={simu} ccc_msub /s/apply-job.sh")
    plan = sharded_iteration(tmp_path, log)
    shard_run(log, tmp_path, plan, 1, 2, ["/f0.nc", "/f2.nc"], run_id="s1")
    info, s = steps(log)
    assert s[4]["state"] == "next" and "shard 2 of 2 still to run: 2/4 files (50%)" in s[4]["detail"]
    assert info["command"] == "SHARD=2/2 RESUME=1 SIMU=SIM01 ccc_msub /s/apply-job.sh"
    shard_run(log, tmp_path, plan, 2, 2, ["/f1.nc"], run_id="s2", ended=False, heartbeat_age=10)
    info, s = steps(log)
    assert s[4]["state"] == "running" and "shard 2/2 running" in s[4]["detail"] and "3/4 files (75%)" in s[4]["detail"]
    shard_run(log, tmp_path, plan, 2, 2, ["/f3.nc"], run_id="s2b")
    info, s = steps(log)
    assert s[4]["state"] == "done" and "4/4 files (100%)" in s[4]["detail"] and s[3]["state"] == "next"
    assert info["command"].startswith("tb esgqa -t wcrp_cmip7:1.0 -t cf:1.11 -o " + f"{RUN_DIR}/qa_2")



def dry_shard(log, tmp_path, plan, k, total, files, *, run_id, exit_code=0, ended=True, heartbeat_age=None, failed=()):
    fixlog = tmp_path / f"fixlog.dry-run.shard-{k}.jsonl"
    with fixlog.open("a") as out:
        for f in files:
            out.write(json.dumps({"etb_run_id": run_id, "file": f, "dry_run": True,
                                  "status": "failed" if f in failed else "done"}) + "\n")
    argv = ["esgf-fix-tool", "apply", plan, "--only-under", WORK, "--workers", "16", "--shard", f"{k}/{total}",
            "--dry-run", "--fixlog", str(fixlog)]
    if ended:
        run_line(log, "2026-11-03", argv, exit_code=exit_code, run_id=run_id)
    else:
        unfinished_run(log, argv, run_id, heartbeat_age=heartbeat_age)


SHARDS_JOB = "SIMU={simu} SHARDS=auto bash /s/apply-job.sh"


def test_todo_suggests_the_shard_launcher_for_the_dry_run_and_the_real_apply(tmp_path, monkeypatch):
    log = tmp_path / "runs.jsonl"
    monkeypatch.setenv("ETB_JOB_APPLY", "SIMU={simu} ccc_msub /s/apply-job.sh")
    monkeypatch.setenv("ETB_JOB_APPLY_SHARDS", SHARDS_JOB)
    plan = sharded_iteration(tmp_path, log)
    info, s = steps(log)
    assert "plan + dry run, as jobs" in s[4]["detail"]
    assert info["command"] == "DRYRUN=1 SIMU=SIM01 SHARDS=auto bash /s/apply-job.sh"
    run_line(log, "2026-11-03", ["esgf-fix-tool", "plan", f"{RUN_DIR}/qa_1/r.json", "-o", plan])
    dry_shard(log, tmp_path, plan, 1, 2, ["/f0.nc", "/f2.nc"], run_id="d1")
    dry_shard(log, tmp_path, plan, 2, 2, ["/f1.nc", "/f3.nc"], run_id="d2")
    info, s = steps(log)
    assert s[4]["state"] == "next" and "dry run done, now for real" in s[4]["detail"]
    assert info["command"] == "SIMU=SIM01 SHARDS=auto bash /s/apply-job.sh"


def test_todo_waits_for_every_dry_run_shard(tmp_path, monkeypatch):
    log = tmp_path / "runs.jsonl"
    monkeypatch.setenv("ETB_JOB_APPLY", "SIMU={simu} ccc_msub /s/apply-job.sh")
    monkeypatch.setenv("ETB_JOB_APPLY_SHARDS", SHARDS_JOB)
    plan = sharded_iteration(tmp_path, log)
    run_line(log, "2026-11-03", ["esgf-fix-tool", "plan", f"{RUN_DIR}/qa_1/r.json", "-o", plan])
    dry_shard(log, tmp_path, plan, 1, 2, ["/f0.nc", "/f2.nc"], run_id="d1")
    dry_shard(log, tmp_path, plan, 2, 2, ["/f1.nc"], run_id="d2", ended=False, heartbeat_age=10)
    info, s = steps(log)
    assert s[4]["state"] == "running" and "apply dry run (2/2 shards) running" in s[4]["detail"]
    assert "3/4 files (75%)" in s[4]["detail"]
    # the second shard killed: run that shard's dry run again
    dry_shard(log, tmp_path, plan, 2, 2, [], run_id="d2b", ended=False, heartbeat_age=3600)
    info, s = steps(log)
    assert s[4]["state"] == "dead" and "apply dry run, shard 2/2 stopped" in s[4]["detail"]
    assert info["command"] == "DRYRUN=1 SHARD=2/2 SIMU=SIM01 ccc_msub /s/apply-job.sh"
    # done, with files that would fail (exit 1): on to the real apply
    dry_shard(log, tmp_path, plan, 2, 2, ["/f1.nc", "/f3.nc"], run_id="d2c", exit_code=1, failed={"/f3.nc"})
    info, s = steps(log)
    assert s[4]["state"] == "next" and "some files would fail (4/4 files (100%), 1 failed)" in s[4]["detail"]
    assert info["command"] == "SIMU=SIM01 SHARDS=auto bash /s/apply-job.sh"


def test_a_missing_dry_run_shard_is_still_to_run(tmp_path, monkeypatch):
    log = tmp_path / "runs.jsonl"
    monkeypatch.setenv("ETB_JOB_APPLY", "SIMU={simu} ccc_msub /s/apply-job.sh")
    plan = sharded_iteration(tmp_path, log)
    run_line(log, "2026-11-03", ["esgf-fix-tool", "plan", f"{RUN_DIR}/qa_1/r.json", "-o", plan])
    dry_shard(log, tmp_path, plan, 1, 3, ["/f0.nc"], run_id="d1")
    info, s = steps(log)
    assert s[4]["state"] == "next" and "apply dry run, shard 2/3 still to run" in s[4]["detail"]
    assert info["command"] == "DRYRUN=1 SHARD=2/3 SIMU=SIM01 ccc_msub /s/apply-job.sh"

def test_todo_resumes_a_dead_shard_or_a_dead_whole_apply(tmp_path):
    log = tmp_path / "runs.jsonl"
    plan = sharded_iteration(tmp_path, log)
    shard_run(log, tmp_path, plan, 1, 2, ["/f0.nc"], run_id="s1", ended=False, heartbeat_age=3600)
    shard_run(log, tmp_path, plan, 2, 2, ["/f1.nc", "/f3.nc"], run_id="s2")
    info, s = steps(log)
    assert s[4]["state"] == "dead" and "shard 1/2 stopped" in s[4]["detail"]
    assert info["command"] == (f"tb esgf-fix-tool apply {plan} --only-under {WORK} --workers 8 --shard 1/2 "
                               f"--resume '{RUN_DIR}/fixlog*.jsonl' --fixlog {RUN_DIR}/fixlog.shard-1.jsonl")
    other = tmp_path / "other"
    other.mkdir()
    log2 = other / "runs.jsonl"
    plan2 = sharded_iteration(other, log2)
    unfinished_run(log2, ["esgf-fix-tool", "apply", plan2, "--only-under", WORK], "w1", heartbeat_age=3600)
    info, s = steps(log2)
    assert s[4]["state"] == "dead" and "resume it" in s[4]["detail"]
    assert f"--resume '{RUN_DIR}/fixlog*.jsonl'" in info["command"]
    run_line(log2, "2026-11-04", ["esgf-fix-tool", "apply", plan2, "--only-under", WORK, "--resume", "x.jsonl"])
    assert steps(log2)[1][4]["state"] == "done"   # the resume finished the plan


def classified(tmp_path, log, code, summary=None):
    """SIM01 after QA 1 and a classify exit `code`, with its esgf-fix-tool summary next to the report."""
    copied(log)
    run_line(log, "2026-11-02", ["esgf-fix-tool", "deduplicate", WORK, "--only-under", WORK])
    report = tmp_path / "_runs" / "SIM01" / "qa_1" / "qa_result_20261119-1203_abc.json"   # names SIM01, as $RUN does
    report.parent.mkdir(parents=True)
    report.write_text("{}")
    if summary is not None:
        report.with_name("qa_result_20261119-1203_abc.classify.json").write_text(json.dumps(summary))
    run_line(log, "2026-11-03", ["esgqa", "-o", f"{RUN_DIR}/qa_1", WORK])
    run_line(log, "2026-11-03", ["esgf-fix-tool", "classify", str(report)], exit_code=code)


SUMMARY = {"blocking": {"findings": 8625, "files": 3013, "fix": 960, "unknown": 7619, "fix_missing": 30,
                        "upstream": 16}, "known_checker_errors": {"E001": 31}, "unknown_checker_errors": 0}


def test_the_qa_step_shows_the_classify_summary(tmp_path):
    log = tmp_path / "runs.jsonl"
    classified(tmp_path, log, 10, SUMMARY)
    _, s = steps(log)
    assert s[3]["detail"] == ("iteration 1: classify exit 10 (known) — fixable 960/8625 · rules or fixes needed "
                              "7649/8625 · upstream 16 · known checker errors 31")


@pytest.mark.parametrize("code", [20, 40])
def test_a_blocked_qa_moves_on_to_the_known_fixes(tmp_path, code):
    log = tmp_path / "runs.jsonl"
    classified(tmp_path, log, code, dict(SUMMARY, unknown_checker_errors=2) if code == 40 else SUMMARY)
    info, s = steps(log)
    assert s[3]["state"] == "failed" and s[4]["state"] == "next" and info["current"] == 4
    assert "meanwhile, the fixable findings" in s[4]["detail"]
    assert info["command"] == f"tb esgf-fix-tool plan {RUN_DIR}/qa_1/qa_result_*[0-9a-f].json -o {RUN_DIR}/fixplan_1.json"
    if code == 40:
        assert "checker crashed on 2 files" in s[3]["detail"] and "run it again" not in s[3]["detail"]


def test_no_change_stays_on_the_qa_step(tmp_path):
    log = tmp_path / "runs.jsonl"
    classified(tmp_path, log, 30)          # the fixes don't work: no point running them again
    info, s = steps(log)
    assert info["current"] == 3 and s[4]["state"] == "todo" and info["command"].startswith("tb0 esgf-fix-tool classify")


def test_shards_that_ended_with_failed_files_finish_the_iteration(tmp_path):
    log = tmp_path / "runs.jsonl"
    plan = sharded_iteration(tmp_path, log)
    shard_run(log, tmp_path, plan, 1, 2, ["/f0.nc", "/f2.nc"], run_id="s1", exit_code=1)   # a file failed
    shard_run(log, tmp_path, plan, 2, 2, ["/f1.nc", "/f3.nc"], run_id="s2")
    info, s = steps(log)
    assert s[4]["state"] == "done" and "some files failed, they come back in QA" in s[4]["detail"]
    assert s[3]["state"] == "next" and info["command"].startswith(f"tb esgqa -t wcrp_cmip7:1.0 -t cf:1.11 -o {RUN_DIR}/qa_2")


def test_a_shard_that_stopped_with_another_error_is_still_flagged(tmp_path):
    log = tmp_path / "runs.jsonl"
    plan = sharded_iteration(tmp_path, log)
    shard_run(log, tmp_path, plan, 1, 2, [], run_id="s1", exit_code=2)                     # e.g. a bad option
    shard_run(log, tmp_path, plan, 2, 2, ["/f1.nc", "/f3.nc"], run_id="s2")
    _, s = steps(log)
    assert s[4]["state"] == "failed" and "shard 1/2 exit 2" in s[4]["detail"]


def test_a_dry_run_with_failing_files_moves_on_to_the_real_run(tmp_path):
    log = tmp_path / "runs.jsonl"
    plan = sharded_iteration(tmp_path, log)
    run_line(log, "2026-11-03", ["esgf-fix-tool", "plan", f"{RUN_DIR}/qa_1/r.json", "-o", plan])
    fixlog = tmp_path / "fixlog.dry-run.jsonl"
    fixlog.write_text("".join(json.dumps({"etb_run_id": "d1", "file": f"/f{i}.nc", "status": "failed" if i < 2
                                          else "done", "dry_run": True}) + "\n" for i in range(4)))
    run_line(log, "2026-11-03", ["esgf-fix-tool", "apply", plan, "--only-under", WORK, "--dry-run",
                                 "--fixlog", str(fixlog)], exit_code=1, run_id="d1")
    info, s = steps(log)
    assert s[4]["state"] == "next" and "dry run done, some files would fail (4/4 files (100%), 2 failed)" in s[4]["detail"]
    assert "--dry-run" not in info["command"] and info["command"].startswith(f"tb esgf-fix-tool apply {plan}")


def test_the_final_qa_as_a_job_when_the_site_has_one(tmp_path, monkeypatch):
    log, work = tmp_path / "runs.jsonl", tmp_path / "work" / "SIM01"
    work.mkdir(parents=True)
    monkeypatch.setenv("ETB_JOB_FINAL_QA", "SIMU={simu} FINAL=1 ccc_msub /s/qa-job.sh")
    milestone_line(log, "SIM01", "copied", "2026-11-01", src="/data/src/SIM01", work=str(work), files=1)
    for milestone, day in (("fixed", "2026-11-02"), ("linked", "2026-11-03")):
        milestone_line(log, "SIM01", milestone, day)
    info, s = steps(log)
    assert info["current"] == 7 and info["command"] == "SIMU=SIM01 FINAL=1 ccc_msub /s/qa-job.sh"
    final = ["esgqa", "-o", f"{tmp_path}/work/_runs/SIM01/qa_final", "/drs/MIP-DRS7/x"]
    run_line(log, "2026-11-04", final, exit_code=1)  # cut or crashed: the job again
    info, s = steps(log)
    assert s[7]["state"] == "failed" and info["command"] == "SIMU=SIM01 FINAL=1 ccc_msub /s/qa-job.sh"
    run_line(log, "2026-11-05", final)
    assert todo_json(log, "SIM01")["command"].endswith("--final")


def test_mapfiles_after_the_final_gate_as_a_job_and_counted(tmp_path, monkeypatch):
    log, work = tmp_path / "runs.jsonl", tmp_path / "work" / "SIM01"
    (work / "Mapfiles").mkdir(parents=True)
    monkeypatch.setenv("ETB_JOB_MAPFILE", "SIMU={simu} ccc_msub /s/mapfile-job.sh")
    milestone_line(log, "SIM01", "copied", "2026-11-01", src="/data/src/SIM01", work=str(work), files=1)
    for milestone, day in (("fixed", "2026-11-02"), ("linked", "2026-11-03")):
        milestone_line(log, "SIM01", milestone, day)
    run_line(log, "2026-11-04", ["esgqa", "-o", f"{tmp_path}/work/_runs/SIM01/qa_final", "/drs/MIP-DRS7/x"])
    run_line(log, "2026-11-04", ["esgf-fix-tool", "classify", f"{tmp_path}/work/_runs/SIM01/qa_final/r.json",
                                 "--final"], exit_code=20)
    info, s = steps(log)
    assert s[7]["state"] == "failed" and info["current"] == 7 and info["command"].endswith("--final --show 30")
    run_line(log, "2026-11-05", ["esgf-fix-tool", "classify", f"{tmp_path}/work/_runs/SIM01/qa_final/r.json", "--final"])
    info, s = steps(log)
    assert s[7]["state"] == "done" and info["current"] == 8
    assert info["command"] == "SIMU=SIM01 ccc_msub /s/mapfile-job.sh"
    for name in ("a.v20261120.map", "b.v20261120.map"):
        (work / "Mapfiles" / name).write_text("x")
    run_line(log, "2026-11-06", ["esgmapfile", "make", "-p", "cmip7", "--outdir", str(work / "Mapfiles")])
    info, s = steps(log)
    assert s[8]["state"] == "done" and s[8]["detail"] == f"2 mapfiles in {work}/Mapfiles" and info["current"] == 9

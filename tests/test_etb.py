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

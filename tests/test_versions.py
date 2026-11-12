"""Unit tests for toolbox/versions.py (esgf-toolbox-versions), run on the host: make unit."""

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "toolbox" / "versions.py"
spec = importlib.util.spec_from_file_location("versions", SCRIPT)
versions = importlib.util.module_from_spec(spec)
spec.loader.exec_module(versions)

CF_TABLE = """<?xml version="1.0"?>
   <standard_name_table>
   <version_number>95</version_number>
   <last_modified>2026-09-16T04:56:46Z</last_modified>
   <entry id="air_temperature"></entry>
"""


@pytest.fixture
def image(tmp_path, monkeypatch):
    """A fake image layout: esgvoc databases, a CF table and a manifest path, all under tmp_path."""
    dbs = tmp_path / "esgvoc" / "dbs"
    dbs.mkdir(parents=True)
    for project, ver in (("cmip7", "2.5.6"), ("universe", "3.4.7")):
        (dbs / f"{project}.active.json").write_text(
            json.dumps({"active": ver, "source": "registry", "checksum": f"sum-{project}"}))
    cf = tmp_path / "cf.xml"
    cf.write_text(CF_TABLE)
    monkeypatch.setenv("ESGVOC_HOME", str(tmp_path / "esgvoc"))
    monkeypatch.setenv("CF_STANDARD_NAME_TABLE", str(cf))
    monkeypatch.setattr(versions, "MANIFEST", tmp_path / "VERSIONS.json")
    # pytest is installed wherever these tests run; the other name never is.
    monkeypatch.setattr(versions, "TOOLS", ["pytest", "not-a-real-package"])
    return tmp_path


def collect(monkeypatch, **build):
    for key, value in build.items():
        monkeypatch.setenv(key, value)
    assert versions.main(["collect"]) == 0
    return json.loads(versions.MANIFEST.read_text())


# --- what is recorded ---

def test_contents_records_tools_vocabularies_and_cf_table(image):
    recorded = versions.contents()
    assert recorded["tools"] == {"pytest": pytest.__version__, "not-a-real-package": None}
    assert recorded["vocabularies"] == {
        "cmip7": {"version": "2.5.6", "source": "registry", "checksum": "sum-cmip7"},
        "universe": {"version": "3.4.7", "source": "registry", "checksum": "sum-universe"}}
    assert recorded["cf_standard_name_table"] == {"version": "95", "last_modified": "2026-09-16T04:56:46Z"}
    assert recorded["python_packages"]["pytest"] == pytest.__version__
    assert list(recorded["python_packages"]) == sorted(recorded["python_packages"])


def test_missing_cf_table_and_vocabularies_are_empty(image, monkeypatch):
    monkeypatch.setenv("CF_STANDARD_NAME_TABLE", str(image / "missing.xml"))
    monkeypatch.setenv("ESGVOC_HOME", str(image / "missing"))
    recorded = versions.contents()
    assert recorded["cf_standard_name_table"] == {}
    assert recorded["vocabularies"] == {}


def test_system_without_dpkg_does_not_crash(image, monkeypatch):
    def no_dpkg(*args, **kwargs):
        raise FileNotFoundError("dpkg-query")
    monkeypatch.setattr(versions.subprocess, "run", no_dpkg)
    system = versions.contents()["system"]
    assert system["packages"] == {name: None for name in versions.SYSTEM_PACKAGES}
    assert system["python"]


# --- build_id ---

def test_build_id_ignores_build_metadata(image, monkeypatch):
    first = collect(monkeypatch, BUILD_VARIANT="slim", BUILD_COMMIT="aaa", BUILD_DATE="2026-01-01T00:00:00Z")
    second = collect(monkeypatch, BUILD_VARIANT="latest", BUILD_COMMIT="bbb", BUILD_DATE="2026-02-02T00:00:00Z")
    assert first["build_id"] == second["build_id"]
    assert second["build"] == {"variant": "latest", "commit": "bbb", "date": "2026-02-02T00:00:00Z"}


def test_build_id_changes_when_a_version_changes(image, monkeypatch):
    before = collect(monkeypatch)["build_id"]
    (image / "esgvoc" / "dbs" / "cmip7.active.json").write_text(
        json.dumps({"active": "2.5.7", "source": "registry", "checksum": "new"}))
    assert collect(monkeypatch)["build_id"] != before


def test_build_date_defaults_to_now_when_not_given(image, monkeypatch):
    monkeypatch.delenv("BUILD_DATE", raising=False)
    assert collect(monkeypatch)["build"]["date"].startswith("20")


# --- commands ---

def test_show_prints_a_summary(image, monkeypatch, capsys):
    manifest = collect(monkeypatch, BUILD_VARIANT="slim", BUILD_COMMIT="abc1234")
    capsys.readouterr()
    assert versions.main([]) == 0
    out = capsys.readouterr().out
    assert f"build_id {manifest['build_id']}" in out and "variant slim" in out and "commit abc1234" in out
    assert "not-a-real-package   not installed" in out
    assert "cmip7                2.5.6  (registry)" in out
    assert "version 95" in out


def test_json_prints_the_manifest(image, monkeypatch, capsys):
    manifest = collect(monkeypatch)
    capsys.readouterr()
    assert versions.main(["--json"]) == 0
    assert json.loads(capsys.readouterr().out) == manifest


def test_live_without_drift_exits_0(image, monkeypatch, capsys):
    manifest = collect(monkeypatch)
    capsys.readouterr()
    assert versions.main(["--live"]) == 0
    assert f"No drift: build_id {manifest['build_id']}" in capsys.readouterr().out


def test_live_reports_changed_added_and_removed_entries(image, monkeypatch, capsys):
    collect(monkeypatch)
    dbs = image / "esgvoc" / "dbs"
    (dbs / "cmip7.active.json").write_text(json.dumps({"active": "9.9.9", "source": "registry",
                                                       "checksum": "sum-cmip7"}))
    (dbs / "universe.active.json").unlink()
    (dbs / "cmip6.active.json").write_text(json.dumps({"active": "2.1.2", "source": "registry",
                                                       "checksum": "x"}))
    capsys.readouterr()
    assert versions.main(["--live"]) == 1
    out = capsys.readouterr().out.splitlines()
    assert "vocabularies.cmip7.version: 2.5.6 -> 9.9.9" in out
    assert "vocabularies.universe.version: 3.4.7 -> None" in out
    assert "vocabularies.cmip6.version: None -> 2.1.2" in out


def test_missing_manifest_exits_1(image, capsys):
    assert versions.main([]) == 1
    assert "No manifest" in capsys.readouterr().err


@pytest.mark.parametrize("args", [["--nope"], ["collect", "extra"], ["--json", "--live"]])
def test_unknown_arguments_exit_2_with_usage(image, capsys, args):
    assert versions.main(args) == 2
    assert "esgf-toolbox-versions" in capsys.readouterr().err

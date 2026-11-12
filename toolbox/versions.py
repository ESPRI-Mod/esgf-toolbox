#!/usr/bin/env python3
"""Versions of everything inside the esgf-toolbox image.

    esgf-toolbox-versions            show the build-time manifest
    esgf-toolbox-versions --json     print the manifest as JSON
    esgf-toolbox-versions --live     compare the manifest with what is installed now (exit 1 on drift)
    esgf-toolbox-versions collect    write the manifest (run once, at image build time)

The image installs the latest release of every tool when it is built. The manifest records what that build got.
build_id is a hash of the recorded contents only (not the date or commit): same contents, same build_id.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, distributions, version
from pathlib import Path

MANIFEST = Path(os.environ.get("ESGF_TOOLBOX_MANIFEST", "/app/VERSIONS.json"))
TOOLS = ["esgvoc", "esgprep", "compliance-checker", "cc-plugin-wcrp", "cmip7-repack", "esgf-qa", "pyudunits2",
         "netCDF4"]
SYSTEM_PACKAGES = ["libnetcdf22", "libhdf5-310", "libudunits2-0", "hdf5-tools"]
SECTIONS = ["tools", "vocabularies", "cf_standard_name_table", "system", "python_packages"]


def _tool_version(name: str) -> str | None:
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def _vocabularies() -> dict:
    dbs = Path(os.environ.get("ESGVOC_HOME", "/app/esgvoc_data")) / "dbs"
    found = {}
    for path in sorted(dbs.glob("*.active.json")):
        data = json.loads(path.read_text())
        found[path.name.removesuffix(".active.json")] = {
            "version": data.get("active"), "source": data.get("source"), "checksum": data.get("checksum")}
    return found


def _cf_table() -> dict:
    path = Path(os.environ.get("CF_STANDARD_NAME_TABLE", "/opt/cf/cf-standard-name-table.xml"))
    if not path.is_file():
        return {}
    head = path.read_text(errors="replace")[:2000]
    fields = {key: re.search(f"<{key}>([^<]*)</{key}>", head) for key in ("version_number", "last_modified")}
    return {"version": fields["version_number"] and fields["version_number"][1],
            "last_modified": fields["last_modified"] and fields["last_modified"][1]}


def _system() -> dict:
    try:
        result = subprocess.run(["dpkg-query", "-W", "-f", "${Package} ${Version}\n", *SYSTEM_PACKAGES],
                                capture_output=True, text=True, check=False)
        output = result.stdout
    except FileNotFoundError:  # not a Debian system
        output = ""
    packages = dict(line.split(" ", 1) for line in output.splitlines() if " " in line)
    debian = Path("/etc/debian_version")
    return {"debian": debian.read_text().strip() if debian.is_file() else None,
            "python": platform.python_version(),
            "packages": {name: packages.get(name) for name in SYSTEM_PACKAGES}}


def contents() -> dict:
    """What is installed now, in the manifest's layout (without build metadata)."""
    return {
        "tools": {name: _tool_version(name) for name in TOOLS},
        "vocabularies": _vocabularies(),
        "cf_standard_name_table": _cf_table(),
        "system": _system(),
        "python_packages": dict(sorted((d.metadata["Name"].lower(), d.version) for d in distributions())),
    }


def build_id(recorded: dict) -> str:
    canonical = json.dumps({key: recorded[key] for key in SECTIONS}, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:12]


def collect() -> dict:
    recorded = contents()
    return {
        "build_id": build_id(recorded),
        "build": {"variant": os.environ.get("BUILD_VARIANT") or None,
                  "commit": os.environ.get("BUILD_COMMIT") or None,
                  "date": os.environ.get("BUILD_DATE") or datetime.now(UTC).isoformat(timespec="seconds")},
        **recorded,
    }


def show(manifest: dict) -> None:
    build = manifest["build"]
    print(f"esgf-toolbox  build_id {manifest['build_id']}  variant {build['variant']}  "
          f"commit {build['commit']}  built {build['date']}")
    print("\nTools")
    for name, ver in manifest["tools"].items():
        print(f"  {name:20} {ver or 'not installed'}")
    print("\nControlled vocabularies (esgvoc)")
    for name, voc in manifest["vocabularies"].items():
        print(f"  {name:20} {voc['version']}  ({voc['source']})")
    cf = manifest["cf_standard_name_table"]
    print(f"\nCF standard name table  version {cf.get('version')}  ({cf.get('last_modified')})")
    system = manifest["system"]
    print(f"\nSystem  Debian {system['debian']}, Python {system['python']}")
    for name, ver in system["packages"].items():
        print(f"  {name:20} {ver}")
    print(f"\n{len(manifest['python_packages'])} Python packages in total: --json for the full list")


def drift(manifest: dict, now: dict) -> list[str]:
    """Human-readable differences between the manifest and the current installation."""
    def flat(tree, prefix=""):
        for key, value in tree.items():
            if isinstance(value, dict):
                yield from flat(value, f"{prefix}{key}.")
            else:
                yield f"{prefix}{key}", value

    before = dict(flat({key: manifest[key] for key in SECTIONS}))
    after = dict(flat(now))
    return [f"{key}: {before.get(key)} -> {after.get(key)}"
            for key in sorted(before.keys() | after.keys()) if before.get(key) != after.get(key)]


def main(argv: list[str]) -> int:
    if argv == ["collect"]:
        MANIFEST.write_text(json.dumps(collect(), indent=1) + "\n")
        print(f"build_id {json.loads(MANIFEST.read_text())['build_id']} -> {MANIFEST}")
        return 0
    if argv not in ([], ["--json"], ["--live"]):
        print(__doc__, file=sys.stderr)
        return 2
    if not MANIFEST.is_file():
        print(f"No manifest at {MANIFEST}: was this image built with 'esgf-toolbox-versions collect'?",
              file=sys.stderr)
        return 1
    manifest = json.loads(MANIFEST.read_text())
    if argv == ["--json"]:
        print(json.dumps(manifest, indent=1))
    elif argv == ["--live"]:
        changes = drift(manifest, contents())
        print("\n".join(changes) if changes else f"No drift: build_id {manifest['build_id']} still matches.")
        return 1 if changes else 0
    else:
        show(manifest)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

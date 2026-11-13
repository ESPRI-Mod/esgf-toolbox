# esgf-toolbox

Docker image bundling ESGF climate data tools for pre-publication workflows. Designed for offline or air-gapped environments (HPC clusters) where installing Python packages is not practical.

## Tools included

| Tool | Purpose |
|---|---|
| [compliance-checker](https://github.com/ioos/compliance-checker) + [cc-plugin-wcrp](https://github.com/WCRP-CMIP/cc-plugin-wcrp) | QA/QC validation of netCDF files against CMIP7, CMIP6, CORDEX-CMIP6 standards |
| [esgprep](https://github.com/ESGF/esgf-prepare) (esgdrs + esgmapfile) | Organize files into DRS directory trees and generate ESGF mapfiles |
| [cmip7-repack](https://github.com/WCRP-CMIP/cmip7-repack) | Rechunk and compress CMIP7 netCDF4 files to meet packing requirements |
| [esgvoc](https://github.com/ESPRI-Mod/esgvoc) | ESGF controlled vocabularies (pre-downloaded for offline use) |
| [esgf-qa](https://pypi.org/project/esgf-qa/) (esgqa) | Run compliance-checker QA over a whole directory tree, with parallel runs and resumable results |
| [esgf-fix-tool](https://github.com/ESPRI-Mod/esgf-fix-tool) | Fix CMIP7 files from an esgqa report with reviewed rules (classify → plan → apply) |

Pre-installed vocabulary snapshots: `universe@latest`, `cmip6@latest`, `cmip6plus@latest`, `cmip7@latest`, `cordex-cmip6@latest`.

esgf-fix-tool is installed from the wheel of its latest [GitHub release](https://github.com/ESPRI-Mod/esgf-fix-tool/releases)
(`make build ESGF_FIX_TOOL_VERSION=v0.1.0` to choose one).

The latest CF Standard Name Table is downloaded at build time to `/opt/cf/cf-standard-name-table.xml` (`$CF_STANDARD_NAME_TABLE`).

## Image variants

A single `Dockerfile` builds both variants as separate targets, so they always ship the same tools and vocabularies.

| Variant | Target | Compressed (`make save`) | Uncompressed (`docker images`) | Description |
|---|---|---|---|---|
| `latest` | `latest` (default) | ~161 MB | ~549 MB | Includes the `uv` package manager. Use this if you need to install additional Python packages inside the container. |
| `slim` | `slim` | ~141 MB | ~498 MB | No `uv`, no `__pycache__`, no type stubs. Use this for production deployment. |

Both variants drop the test suites bundled inside Python packages. Tools are available directly on `$PATH` via the activated venv.

Build either with:

```bash
make build                  # latest
make build VARIANT=slim     # slim
```

`VARIANT` also applies to `make test`, `make run` and `make save`.

## What's inside a build

Every build installs the **latest** release of each tool, vocabulary and the CF table; nothing is pinned. Docker
reuses cached layers from earlier builds, though: a plain `make build` only re-resolves what changed (for example a
new esgf-fix-tool release). Use `make build FRESH=1` to rebuild every layer and get the latest of everything.

The build records what it got in `/app/VERSIONS.json`:

```bash
make versions                                     # from the host
esgf-toolbox-versions                             # inside the container
esgf-toolbox-versions --json                      # full manifest, including every Python package
esgf-toolbox-versions --live                      # compare with what is installed now (exit 1 on drift)
```

```text
esgf-toolbox  build_id 03c6932c11fe  variant slim  commit 5341c26  built 2026-11-12T10:29:13Z

Tools
  esgvoc               7.2.0
  cc-plugin-wcrp       2.4.0b3
  ...
Controlled vocabularies (esgvoc)
  cmip7                2.5.6  (registry)
  ...
CF standard name table  version 95  (2026-09-16T04:56:46Z)
```

The manifest covers the main tools, all Python packages, the esgvoc vocabularies (version and checksum), the CF
standard name table, and the system netCDF/HDF5/udunits libraries. `build_id` is a hash of those contents only:
two builds with the same `build_id` contain the same versions, and a new `build_id` means something changed. The
git commit and build date are recorded alongside, and as OCI labels (`docker inspect`).

## Run log (`etb`)

`etb` keeps a record of what was run with the image, to be able to tell later what was done, with which versions:

```bash
etb run --note "QA iteration 1" -- esgqa -o qa_out /data      # run any tool through etb
etb run -- esgf-fix-tool classify qa_out/qa_result_*.json
etb log                                                        # what was run here
etb log --json                                                 # one JSON object per run
etb versions                                                   # same as esgf-toolbox-versions
```

```text
e758fd3fa608  2026-11-12T11:06:37+00:00  nonzero       exit 20      1s  build b869d957a5e5  esgf-fix-tool classify ...
e97fa5e52128  2026-11-12T11:06:38+00:00  ok            exit 0       2s  build b869d957a5e5  esgdrs --help
```

Each run appends to `./etb-runs.jsonl` in the working directory (`--log PATH` or `$ETB_LOG` to change it; it must be
on a mounted volume to outlive the container). A run records its command line, working directory, user, host, the
package and version providing the command, the image `build_id` (which links to `etb versions`), timings and exit
code. A `start` record is written before the command, so a run killed by a job time limit still leaves a trace
(`no_end_record`). `etb run` exits with the command's own exit code, so scripts can keep using it, and passes
`ETB_RUN_ID` to the command. If the log cannot be written, nothing is run.

### Where each simulation stands (`etb copy`, `etb done`, `etb todo`)

The same log records workflow milestones, so the team knows what was already done for each simulation and when
its source can be removed. Point `$ETB_LOG` at one shared file in the work directory.

```bash
etb copy $SRC $WORK                 # copy a simulation to the shared work directory -> milestone "copied"
etb done $SIMU fixed                # QA clean
etb done $SIMU linked               # hard-linked into the DRS (esgdrs --link)
etb done $SIMU published
etb todo                            # every simulation: last milestone, next action, source removal date
etb done $SIMU src-removed          # after rm -r $SRC
```

```text
SIM01      published    2026-11-13   -> rm -r the source possible from 2027-01-13 (in 61 days)
SIM02      copied       2026-11-12   -> deduplicate ATM/SRF, then QA -> fix loop until classify exits 0 (steps 2-5)
```

`etb copy` is safe to run again: a file already in WORK with the same size is kept, one with a different size is
reported and left alone (exit 1, no milestone), and each file is written under a temporary name then renamed, so an
interrupted copy never leaves a truncated file. Running it again on a copied simulation says who copied it and when.
`etb todo --months N` changes the delay after publication (default 2 months). Records name the user from
`$ETB_USER` when set (useful in containers, where the image sets `USER=root`).

## Quick start

```bash
# Build the image
make build

# Check the manifest and etb, run the test pipeline (QA/QC + esgdrs + esgmapfile), then the operator workflow
make test

# Unit tests of the toolbox scripts, on the host (no image needed; also run by CI on every PR)
make unit

# Interactive shell with your data mounted at /data
make run DATA=/path/to/netcdf/files

# Inside the container:
compliance-checker -t wcrp_cmip7 /data/*.nc
esgdrs make list -p cmip7 /data/
esgdrs make upgrade -p cmip7 --root /output /data/
esgmapfile make -p cmip7 --directory /output/MIP-DRS7/ --outdir /output/mapfiles
```

## Workflow

The typical pre-publication pipeline is:

```
incoming netCDF files
    |
    v
1. QA/QC (compliance-checker)     -- validate against project standards
    |
    v
2. Repack (cmip7repack)           -- CMIP7 only: rechunk + compress
    |
    v
3. DRS tree (esgdrs)              -- organize into standard directory structure
    |
    v
4. Mapfiles (esgmapfile)          -- generate publication mapfiles
    |
    v
ready for ESGF publication
```

Supported projects: **CMIP7**, **CMIP6**, **CORDEX-CMIP6**.

## Export for cluster deployment

```bash
# Export the image as a compressed tar archive
make save                   # produces esgf-toolbox.tar.gz
make save VARIANT=slim      # produces esgf-toolbox-slim.tar.gz
```

`make save` also writes the build's manifest and lockfile next to the archive (`esgf-toolbox.versions.json`,
`esgf-toolbox.uv.lock`). Keep them with any image you deploy: they record exactly what the image contains, and the
lockfile is what a future build would need to reproduce it.

Then transfer to your cluster and import using your container runtime:

```bash
# Singularity / Apptainer
singularity build esgf-toolbox.sif docker-archive:esgf-toolbox.tar.gz

# pcocc-rs
pcocc-rs image import docker-archive:esgf-toolbox.tar.gz esgf-toolbox

# Podman
podman load -i esgf-toolbox.tar.gz
```

### Running on TGCC (pcocc-rs)

Put these two functions in `~/.bashrc` (`IMG` = the name given to `pcocc-rs image import`). `tb` runs a command in the
image **and records it in the shared run log**; `tb0` runs it without recording, for help pages and quick looks.
`etb` subcommands (`todo`, `done`, `copy`, `log`, `versions`) are never wrapped: they write their own records.

```bash
IMG=esgf-toolbox-20261113
ETB_SHARED_LOG=/ccc/work/cont003/cmip7/cmip7/work/etb-runs.jsonl

tb0() {
    pcocc-rs run --env PATH=/app/.venv/bin:/usr/local/bin:/usr/bin:/bin \
        --env ETB_LOG="$ETB_SHARED_LOG" --env ETB_USER="$USER" "$IMG" -- "$@"
}
tb() {
    if [ "$1" = etb ]; then tb0 "$@"; else tb0 etb run -- "$@"; fi
}
```

```bash
tb esgqa -t wcrp_cmip7:1.0 -t cf:1.11 -o $RUN/qa_1 $WORK     # recorded
tb0 esgf-fix-tool apply --help                              # not recorded
tb etb todo                                                 # where each simulation stands
```

`etb todo` warns when a milestone has no matching command in the log, e.g. `linked` with no successful
`esgdrs make upgrade --link` on that simulation: a command run outside `tb` (or `etb run`) leaves a gap there.

## Test files

### Golden files

The `tests/golden/` directory contains small synthetic netCDF files (one per project, ~40-55 KB each) subsetted from real data. They are used by `make test` to validate the full pipeline inside the container.

To regenerate them from real source files in `test_data/`:

```bash
docker run --rm \
  -v $(pwd)/test_data:/test_data \
  -v $(pwd)/tests:/tests \
  esgf-toolbox python /tests/generate_golden.py
```

### Workflow test

`tests/run_workflow.sh` (part of `make test`) runs the whole operator workflow on a copy of the CMIP7 golden file,
offline, every step through `etb` as an operator would, and checks each one:

| Step | Checked |
|---|---|
| `etb copy`, twice | identical copy; the second run says it was already copied and copies nothing |
| `deduplicate --dry-run` | nothing to delete in the golden file |
| QA → `plan` → `apply` → QA | every finding with an implemented fix is gone, no new finding; the fixlog has the tool version, rules digest and the `etb_run_id` of the `apply` run |
| `cmip7repack -o` | files repacked in place, no `*_cmip7repack` copy |
| `esgdrs make upgrade --link` | `v<VERSION>/` symlinks to `files/d<VERSION>/`, `latest` → `v<VERSION>`, the DRS file **is** the WORK file (2 links) |
| `esgmapfile` | one mapfile for the version |
| `etb done` …, `etb todo` | published, source removable 2 months later; done after `src-removed` |
| new version | `apply` refuses the hard-linked file; with `--break-hardlinks`, WORK gets the fix and the published DRS file is unchanged |

The golden file has findings esgf-fix-tool can fix (e.g. `history` on the data variable) and some it cannot
(repacking, review rules), so the QA decision stays `new_rule`; the test checks that what can be fixed is fixed.

### Broken variants

The `tests/broken/` directory contains intentionally broken netCDF files to test failure paths and understand how the tools behave with bad input. Generate them with:

```bash
uvx --from netCDF4 python tests/generate_broken.py
```

Key finding: **esgdrs cross-references the filename and global attributes**. When one is wrong, it falls back to the other. QA/QC (compliance-checker) is the real gatekeeper — it catches issues in all cases.

| Category | QA/QC | esgdrs | Example |
|---|---|---|---|
| Wrong attribute only | Catches it | **Succeeds** (uses filename) | `source_id` attr set to `FAKE-MODEL-999`, filename unchanged |
| Wrong filename only | Catches it | **Succeeds** (uses attributes) | File renamed with wrong `source_id`, attribute unchanged |
| Both wrong | Catches it | **Fails** (SKIPPED) | Both filename and attribute have wrong `source_id` |
| Corrupted file | Crashes | **Fails** (InvalidNetCDF) | Truncated file, non-netCDF file |

## Known limitations

- **CORDEX compliance-checker requires network access**: the `wcrp_cordex_cmip6` checker downloads CMOR tables from GitHub at runtime via `pooch`. This will not work on air-gapped clusters. The esgdrs and esgmapfile steps work fully offline.
- **Vocabulary updates**: to get the latest controlled vocabularies, rebuild the image (`make build`). The esgvoc snapshots are baked into the image at build time; `esgf-toolbox-versions` shows which ones.

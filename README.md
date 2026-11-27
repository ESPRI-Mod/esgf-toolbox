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

### Where each simulation stands (`etb copy`, `etb done`, `etb todo`, `etb reopen`)

The same log records workflow milestones, so the team knows what was already done for each simulation and when
its source can be removed. Point `$ETB_LOG` at one shared file in the work directory.

```bash
etb copy $SRC $WORK                 # copy a simulation to the shared work directory -> milestone "copied"
etb copy $SRC $WORK --only CMIP7    # only SRC/CMIP7 -> WORK/CMIP7 (not MONITORING/...); may be repeated
etb done $SIMU fixed                # QA clean, repacked
etb done $SIMU linked               # hard-linked into the DRS (esgdrs --link)
etb done $SIMU published
etb done $SIMU src-removed          # after rm -r $SRC
etb todo                            # one line per simulation that needs something now
etb todo $SIMU                      # that simulation, step by step
etb todo --later                    # published, waiting for the delay before rm SRC
etb todo --all                      # every simulation, finished ones included
etb reopen $SIMU --note "v2: ..."   # a new version: steps 3-10 start again from now
etb abandon $SIMU [--delete]        # drop it (wrong simulation...); --delete removes WORK and RUN
etb restart $SIMU --from 1|3        # start again from the copy (1) or from QA (3)
```

```text
CM6-ESMCO2-FT-esm-pi.01          ✓✓✗✓······  step 3 QA: iteration 2: classify exit 20, new_rule: rule or review needed, PR on esgf-fix-tool
                                 tb0 esgf-fix-tool classify /ccc/work/.../_runs/CM6-ESMCO2-FT-esm-pi.01/qa_2/qa_result_*[0-9a-f].json --show 30
CM6-ESMCO2-FT-hist.02            ✓✓▶·······  step 3 QA: QA iteration 1 (big simulation: the qa-job.sh job)
                                 tb esgqa -t wcrp_cmip7:1.0 -t cf:1.11 -o /ccc/work/.../_runs/CM6-ESMCO2-FT-hist.02/qa_1 /ccc/work/.../CM6-ESMCO2-FT-hist.02
(+ 1 waiting for the delay: etb todo --later; 0 done or abandoned: etb todo --all)
```

Under each simulation is the command to run next, ready to copy-paste. It names that simulation's own SRC, WORK
and RUN (`<work dir>/_runs/SIMU`), since the shell's `$WORK` may be another simulation's, writes out the QA tests
(`-t wcrp_cmip7:1.0 -t cf:1.11`, right even without `etb-env.sh`), and uses `$PROJECT`, `$DRS_ROOT`, `$VERSION`,
`$DRS_SIMU`, `$COPY_ONLY` from `etb-env.sh`. A preview moves it on to the
real command (deduplicate and apply `--dry-run`, `cmip7repack -x`, `esgdrs make tree`). After a failed or dead
run, it is that run again, or a look at what failed (`classify --show 30`, the failed lines of the log).

With esgf-fix-tool >= 0.8.0, the QA step also shows what `classify` said, from the summary it leaves next to the
report (`<report>.classify.json`), the blocking findings by what the rules say:
`8625 blocking: rule + fix 960 · no rule 7619 · rule, no fix 30 · review 12 · upstream 16 · known checker errors 31`.
"Rule + fix" only means a rule has a fix for the message: whether the fix goes through on the files, only the dry
run of the fix step tells. When the decision is `new_rule` (20), `blocked_upstream` (21) or `checker_error` (40),
the QA step stays ✗ but the next action is the fix step: what the tool knows how to fix can go on meanwhile (not
after `no_change` (30): the fixes did not work).

Once the dry run is done, the fix step says what it found per file of the plan, with the main reasons for refusing:
`75 files: would change 12 · nothing to change 0 · refused 63 (45 Unreviewed climatological time semantics · …)`.
When nothing would change (every known fix refused on its files), it does not suggest the real apply: the step is ✗,
"a decision or a rule is needed", with `classify --show 30` to see what is left.

Heavy steps can be suggested as jobs instead, so they don't run on a login node: when `$ETB_JOB_QA`,
`$ETB_JOB_APPLY` or `$ETB_JOB_COPY` is set (inside the image), `etb todo` suggests it, with `{simu}` replaced by
the simulation name:

- `ETB_JOB_QA`: QA on WORK (iteration N, N+1, or resuming a dead one);
- `ETB_JOB_APPLY`: the fix step, `DRYRUN=1 <job>` first (the job writes the plan if needed), then `<job>`;
  also after a dead apply;
- `ETB_JOB_APPLY_SHARDS`: the same over several jobs, suggested instead of `ETB_JOB_APPLY` for the dry run and
  the real apply (e.g. `SIMU={simu} SHARDS=auto bash /path/apply-job.sh`, run on the login node: it writes the
  plan if needed and submits the shards). A dry run, like a real apply, is done when all its shards are; a shard
  that was killed, failed or never ran is suggested again on its own (`DRYRUN=1 SHARD=k/K <ETB_JOB_APPLY>`);
- `ETB_JOB_COPY`: the copy; the job ends with the `etb copy` check, which records the milestone `copied`.

A site's environment script sets them, e.g. `ETB_JOB_QA='SIMU={simu} ccc_msub -r qa-{simu} /path/qa-job.sh'`.

When there is more than one way to go on, `etb todo` puts each command under its title, the usual one last (the
line to copy when all is normal):

```text
  -> step 4 fix: fix iteration 1: meanwhile, the fixable findings: plan + dry run, as jobs
     if you decide to go on (your call):
       DRYRUN=1 SIMU=… SHARDS=auto bash …/repack-job.sh
     without a job (a short step, on the login node):
       tb esgf-fix-tool plan …/qa_1/qa_result_*[0-9a-f].json -o …/fixplan_1.json && tb esgf-fix-tool apply … --dry-run …
     next, if everything is normal:
       DRYRUN=1 SIMU=… SHARDS=auto bash …/apply-job.sh
```

"Without a job": the same step on the login node, for a short step (a few files: a job and its cores would take
longer to get than the step itself). "If you decide to go on": when the QA is blocked (classify exit 20, 21, 40…)
and the operator decides the rest can wait (files held out of WORK, an upstream finding), the next step. Step 3 stays
✗ as a reminder; once something of steps 5-8 is in the log, `etb todo` follows from there.

While `apply` runs (or after it stopped), the fix step shows how far it got, e.g. `1240/3033 files (41%),
3 failed, ~2.3 h left`: the files of its plan, against the lines of its fixlog that carry its own `etb_run_id`
(so a resubmitted run does not count the lines of an earlier one). Same for a dry run. Once it is done, what
went well and what went wrong, file by file: `3034 files: changed 2980 · nothing to change 40 · failed 14 (14
Unreviewed cell_methods for temperature …): the failed files come back in the next QA`. A run that ended non-zero
while no file failed in the fixlogs is said so (`— but 1 run ended with exit 1, no failed file in the fixlogs: see
the job logs`), rather than "some files failed".

With esgf-fix-tool >= 0.7.0, a plan can be finished with `apply --resume` or split over K jobs with `--shard k/K`.
`etb todo` takes every apply run of the fix iteration together: the progress adds up the shards and resumes
(the latest record of a file wins); the step is done once the whole plan ran, or every shard of the latest split
did; until then it suggests the missing shard, or resuming the dead one (`--shard k/K --resume
'<RUN>/fixlog*.jsonl'`, or `SHARD=k/K RESUME=1 <job>` with `$ETB_JOB_APPLY`). Shards skip the automatic ATM/SRF
deduplication: step 2 covers it.

```text
$ etb todo CM6-ESMCO2-FT-esm-pi.01
CM6-ESMCO2-FT-esm-pi.01   (last milestone copied 2026-11-02)
  ✓ 1 copy                     2026-11-02  copied, 900 files
  ✓ 2 deduplicate              2026-11-03  deduplicate (exit 0)
  ✗ 3 QA                       2026-11-06  iteration 2: classify exit 20, new_rule: rule or review needed, PR on esgf-fix-tool
  ✓ 4 fix                      2026-11-05  apply, fix iteration 1 (exit 0)
  · 5 repack
  · 6 DRS
  · 7 final QA
  · 8 mapfiles
  · 9 publication
  · 10 rm SRC
  -> step 3 QA: iteration 2: classify exit 20, new_rule: rule or review needed, PR on esgf-fix-tool
     tb0 esgf-fix-tool classify /ccc/work/.../_runs/CM6-ESMCO2-FT-esm-pi.01/qa_2/qa_result_*[0-9a-f].json --show 30
```

`✓` done, `▶` next, `⧗` running, `†` dead, `✗` failed or blocked, `–` skipped (a later milestone was recorded
anyway), `…` waiting for the delay, `·` not yet. The steps are read from the milestones and from the commands
recorded in the log: 1 copy, 2 deduplicate, 3-4 the QA -> fix loop (iteration number, `classify` decision; done when `classify`
exits 0), 5 repack (the real run as `$ETB_JOB_REPACK` when set, files in parallel; with `$ETB_JOB_REPACK_SHARDS`, e.g. `SIMU={simu} SHARDS=auto bash /path/repack-job.sh`, the preview `DRYRUN=1 …` and the real run over several jobs, followed together: the milestone only once all K shards are done), 6 esgdrs, 7 the final QA gate on the version directories of the DRS (`esgqa -b /files/`; `classify --final` exits 0; `$ETB_JOB_FINAL_QA` for a job), 8 the mapfiles (one per dataset, flat in
`<WORK>/Mapfiles`, for the publication; `$ETB_JOB_MAPFILE` for a job), 9 publication, 10 rm SRC. Previews never count (`--dry-run`, `esgdrs make tree`,
`cmip7repack -x`). A command belongs to a simulation when it names its WORK directory or a path with the
simulation name as a whole component (`$RUN/qa_1/...`). Something running or dead comes first in the next action.

Running or dead: `etb run` and `etb copy` touch a heartbeat file (`.etb-runs.jsonl.alive/<run_id>`, next to the
log) every minute while they run, and remove it at the end. A run with no end record and a heartbeat less than
5 minutes old is running; otherwise it is dead (killed without notice, e.g. at a job time limit): run it again.

`etb abandon` and `etb restart` clean up a simulation, for example after finding the wrong one was copied, or
data that must not be published. The log is append-only, so they record a milestone instead of erasing anything:

```bash
etb abandon $SIMU --note "why"            # etb todo no longer lists it (--all still does); files kept
etb abandon $SIMU --delete                # also remove its WORK and RUN (<work dir>/_runs/SIMU, or --run DIR)
etb restart $SIMU --from 1 --delete       # start again from the copy: WORK and RUN removed, then etb copy again
etb restart $SIMU --from 3 --delete       # start again from QA: qa_*, fixplan_*, fixlog* removed, WORK kept
```

Steps 2-5 change WORK in place, so going back before them means a fresh copy (`--from 1`); `--from 3` keeps
fixes already applied (it warns). SRC is never touched; a directory is only removed if it is named after the
simulation and is not, or does not contain, the source. Both refuse `--delete` once the simulation was linked into
the DRS, and `restart` refuses a linked simulation altogether: for a new version, use `etb reopen`.

`etb reopen` is for a new version of a published simulation (a fix with `apply --break-hardlinks`): from then on,
steps 3-10 only count what happens after it, so the old QA, DRS and publication no longer show as done. copy,
deduplicate and `src-removed` are kept.

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

`etb todo` reads the steps from what `tb` recorded, and warns when a milestone has no matching command in the log, e.g. `linked` with no successful
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
| `etb todo` along the way | a dry run does not count; the QA step shows the `classify` decision; `etb copy` is recorded as a run, its heartbeat removed |
| `etb done` …, `etb todo` | published, source removable 2 months later |
| `etb reopen`, new version | steps 3-10 to do again, copy kept; after `rm`, `src-removed` stays done |
| `etb abandon SIM02 --delete` | its WORK removed, hidden from `etb todo`, shown as abandoned by `--all`, the source untouched |
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

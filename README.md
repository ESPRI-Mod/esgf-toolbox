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

Pre-installed vocabulary snapshots: `universe@latest`, `cmip6@latest`, `cmip6plus@latest`, `cmip7@latest`, `cordex-cmip6@latest`.

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

Every build installs the **latest** release of each tool, vocabulary and the CF table; nothing is pinned. The build
records what it got in `/app/VERSIONS.json`:

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

## Quick start

```bash
# Build the image
make build

# Check the version manifest, then run the test pipeline (QA/QC + esgdrs + esgmapfile) on golden files
make test

# Unit tests of the toolbox scripts, on the host (no image needed)
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

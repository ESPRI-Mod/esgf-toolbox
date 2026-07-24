# esgf-toolbox

Docker image bundling ESGF climate data tools for pre-publication workflows. Designed for offline or air-gapped environments (HPC clusters) where installing Python packages is not practical.

## Tools included

| Tool | Purpose |
|---|---|
| [compliance-checker](https://github.com/ioos/compliance-checker) + [cc-plugin-wcrp](https://github.com/WCRP-CMIP/cc-plugin-wcrp) | QA/QC validation of netCDF files against CMIP7, CMIP6, CORDEX-CMIP6 standards |
| [esgprep](https://github.com/ESGF/esgf-prepare) (esgdrs + esgmapfile) | Organize files into DRS directory trees and generate ESGF mapfiles |
| [cmip7-repack](https://github.com/WCRP-CMIP/cmip7-repack) | Rechunk and compress CMIP7 netCDF4 files to meet packing requirements |
| [esgvoc](https://github.com/ESPRI-Mod/esgvoc) | ESGF controlled vocabularies (pre-downloaded for offline use) |

Pre-installed vocabulary snapshots: `cmip7@latest`, `cmip6@latest`, `cordex-cmip6@latest`, `universe@latest`.

## Image variants

Two Dockerfiles are provided:

| Variant | Dockerfile | Compressed | Description |
|---|---|---|---|
| `latest` | `Dockerfile` | ~164 MB | Full image with `uv` package manager and all build-time dependencies (sympy, mpmath, etc.). Use this if you need to install additional Python packages inside the container. |
| `slim` | `Dockerfile.slim` | ~128 MB | Stripped-down runtime image. No `uv`, no `__pycache__`, no test suites, sympy/mpmath removed. Tools are available directly on `$PATH` via an activated venv. Use this for production deployment. |

Both variants include the same ESGF tools and pre-downloaded vocabularies.

Build either with:

```bash
# latest (default)
make build

# slim
docker build -f Dockerfile.slim -t esgf-toolbox:slim .
```

## Quick start

```bash
# Build the image
make build

# Run the test pipeline (QA/QC + esgdrs + esgmapfile) on golden files
make test

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
make save
# produces esgf-toolbox.tar.gz
```

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
- **Vocabulary updates**: to get the latest controlled vocabularies, rebuild the image (`make build`). The esgvoc snapshots are baked into the image at build time.

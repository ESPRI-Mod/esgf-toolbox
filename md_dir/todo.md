# TODO

## Done

- [x] Build Docker image with all tools (esgvoc, esgprep, cc-plugin-wcrp, cmip7-repack, pyudunits2, HDF5 CLI)
- [x] Multi-stage build + slim variant
- [x] Makefile (build, test, run, save)
- [x] Test pipeline with golden files (CMIP6, CMIP7, CORDEX-CMIP6) — validated locally and on TGCC
- [x] Synthetic golden test files in repo
- [x] `--offline` flag for air-gapped cluster (skips CORDEX QA/QC)
- [x] Fix ESGVOC_HOME for pcocc-rs (non-root user)
- [x] Push to GitHub (ESPRI-Mod/esgf-toolbox)

- [x] Broken test variants (12 files) testing esgdrs fallback behavior:
  - Wrong attribute only → esgdrs succeeds (falls back to filename)
  - Wrong filename only → esgdrs succeeds (falls back to attributes)
  - Both wrong → esgdrs fails
  - Corrupted files → esgdrs fails

## Remaining

- [ ] Publishing step on ESGF VM (separate from cluster) — mapfile path rewriting (TGCC → VM), STAC publisher, dry-run validation against esgvoc JSON schema
- [ ] Flag CORDEX offline issue to cc-plugin-wcrp maintainers (pooch downloads CMOR tables at runtime, no offline fallback)

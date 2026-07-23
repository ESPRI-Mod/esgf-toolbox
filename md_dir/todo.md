# TODO

- [ ] Once the image is validated, add convenience build/transfer tooling (Makefile or shell script) with:
  - `build` — `docker build`
  - `run` — run image locally with mounted data directory for testing
  - `transfer` — `docker save` + `scp` to cluster + print `pcocc-rs image import` command

- [ ] Add cmip7_repack + HDF5 CLI tools to the Dockerfile
- [ ] Test pipeline with real data (CMIP6, CMIP7, CORDEX-CMIP6) — discover what works/fails
- [ ] Once we know what "golden" looks like, create small synthetic test files (subset: tiny grid, 1-2 timesteps)
- [ ] Build broken variants from golden files to test failure paths (QA/QC, repack, esgdrs, esgmapfile)
- [ ] Publishing step NOT on cluster — separate VM handles that
- [ ] CORDEX compliance-checker downloads CMOR tables from GitHub at runtime (via pooch) — will not work on the air-gapped cluster. Needs an offline mode in cc-plugin-wcrp (skip download when tables already exist locally, or add --offline flag). Flag to plugin maintainers.

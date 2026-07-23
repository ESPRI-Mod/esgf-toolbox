IMAGE_NAME    := esgf-toolbox
ARCHIVE       := $(IMAGE_NAME).tar.gz

.PHONY: build test run save help

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  make %-10s %s\n", $$1, $$2}'

build: ## Build the Docker image
	docker build -t $(IMAGE_NAME) .

test: ## Run golden file pipeline (QA/QC + esgdrs + esgmapfile) in container
	@docker run --rm -v $(CURDIR)/tests/golden:/data $(IMAGE_NAME) bash -c '\
		set -e && \
		mkdir -p /tmp/drs /tmp/mapfiles && \
		for proj_dir in /data/*/; do \
			proj=$$(basename $$proj_dir); \
			case $$proj in \
				CMIP7)        p=cmip7;        cc=wcrp_cmip7;        drs_top=MIP-DRS7 ;; \
				CMIP6)        p=cmip6;        cc=wcrp_cmip6;        drs_top=CMIP6 ;; \
				CORDEX-CMIP6) p=cordex-cmip6; cc=wcrp_cordex_cmip6; drs_top=CORDEX-CMIP6 ;; \
				*) echo "Unknown project: $$proj"; continue ;; \
			esac; \
			echo "=== $$proj: QA/QC ===" && \
			compliance-checker -t $$cc $$proj_dir/*.nc || true && \
			echo "=== $$proj: esgdrs ===" && \
			cp -r $$proj_dir /tmp/$${proj}_in && \
			esgdrs make upgrade -p $$p --root /tmp/drs /tmp/$${proj}_in/ && \
			echo "=== $$proj: esgmapfile ===" && \
			esgmapfile make -p $$p --outdir /tmp/mapfiles --no-checksum --directory /tmp/drs/$$drs_top; \
		done && \
		echo "=== Generated mapfiles ===" && \
		cat /tmp/mapfiles/*.map'

run: ## Run interactive shell with data: make run DATA=/path/to/files
ifndef DATA
	$(error DATA is required. Usage: make run DATA=/path/to/files)
endif
	docker run --rm -it -v $(DATA):/data $(IMAGE_NAME) bash

save: ## Export image to compressed tar archive
	docker save $(IMAGE_NAME) | gzip > $(ARCHIVE)
	@echo "Saved to $(ARCHIVE) ($$(du -h $(ARCHIVE) | cut -f1))"

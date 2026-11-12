IMAGE_NAME    := esgf-toolbox
VARIANT       ?= latest
IMAGE         := $(IMAGE_NAME):$(VARIANT)
ARCHIVE_BASE  := $(IMAGE_NAME)$(if $(filter-out latest,$(VARIANT)),-$(VARIANT))
ARCHIVE       := $(ARCHIVE_BASE).tar.gz
BUILD_COMMIT  := $(shell git describe --always --dirty 2>/dev/null || echo unknown)
BUILD_DATE    := $(shell date -u +%Y-%m-%dT%H:%M:%SZ)

.PHONY: build test run save versions help

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  make %-10s %s\n", $$1, $$2}'

build: ## Build the Docker image (VARIANT=latest|slim)
	docker build --target $(VARIANT) -t $(IMAGE) \
		--build-arg BUILD_COMMIT=$(BUILD_COMMIT) --build-arg BUILD_DATE=$(BUILD_DATE) .

versions: ## Show what the image contains (tools, vocabularies, CF table, system)
	docker run --rm $(IMAGE) esgf-toolbox-versions

test: ## Run golden file pipeline (QA/QC + esgdrs + esgmapfile) in container
	docker run --rm -v $(CURDIR)/tests:/tests -v $(CURDIR)/tests/golden:/data $(IMAGE) bash /tests/run_pipeline.sh

run: ## Run interactive shell with data: make run DATA=/path/to/files
ifndef DATA
	$(error DATA is required. Usage: make run DATA=/path/to/files)
endif
	docker run --rm -it -v $(DATA):/data $(IMAGE) bash

save: ## Export image to compressed tar archive, with its version manifest and uv.lock
	docker save $(IMAGE) | gzip > $(ARCHIVE)
	docker run --rm $(IMAGE) cat /app/VERSIONS.json > $(ARCHIVE_BASE).versions.json
	docker run --rm $(IMAGE) cat /app/uv.lock > $(ARCHIVE_BASE).uv.lock
	@echo "Saved to $(ARCHIVE) ($$(du -h $(ARCHIVE) | cut -f1))"

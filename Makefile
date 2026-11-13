IMAGE_NAME    := esgf-toolbox
VARIANT       ?= latest
IMAGE         := $(IMAGE_NAME):$(VARIANT)
ARCHIVE_BASE  := $(IMAGE_NAME)$(if $(filter-out latest,$(VARIANT)),-$(VARIANT))
ARCHIVE       := $(ARCHIVE_BASE).tar.gz
BUILD_COMMIT  := $(shell git describe --always --dirty 2>/dev/null || echo unknown)
BUILD_DATE    := $(shell date -u +%Y-%m-%dT%H:%M:%SZ)
AS_USER       := --user $(shell id -u):$(shell id -g)
# FRESH=1: rebuild every layer and pull the base image, so every tool, vocabulary and the CF table is
# re-resolved to its latest version (by default Docker reuses cached layers from earlier builds)
FRESH         ?=
FRESH_FLAGS   := $(if $(filter 1,$(FRESH)),--no-cache --pull)
# Latest esgf-fix-tool release tag (override: make build ESGF_FIX_TOOL_VERSION=v0.1.0)
ESGF_FIX_TOOL_VERSION ?= $(shell curl -sf https://api.github.com/repos/ESPRI-Mod/esgf-fix-tool/releases/latest \
	| python3 -c 'import json, sys; print(json.load(sys.stdin)["tag_name"])' 2>/dev/null || echo latest)

.PHONY: build test unit run save versions help

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  make %-10s %s\n", $$1, $$2}'

build: ## Build the Docker image (VARIANT=latest|slim, FRESH=1 to ignore the cache and get the latest of everything)
	docker build --target $(VARIANT) -t $(IMAGE) $(FRESH_FLAGS) \
		--build-arg BUILD_COMMIT=$(BUILD_COMMIT) --build-arg BUILD_DATE=$(BUILD_DATE) \
		--build-arg ESGF_FIX_TOOL_VERSION=$(ESGF_FIX_TOOL_VERSION) .

versions: ## Show what the image contains (tools, vocabularies, CF table, system)
	docker run --rm $(IMAGE) esgf-toolbox-versions

test: ## In the container, as your own user (like pcocc-rs): manifest, etb, golden pipeline, QA -> fix -> QA
	docker run --rm $(AS_USER) $(IMAGE) etb versions --live
	docker run --rm $(AS_USER) $(IMAGE) sh -c 'cd /tmp && etb run -- true && etb log'
	docker run --rm $(AS_USER) $(IMAGE) sh -c 'echo "{\"fail\": {}}" > /tmp/qa_result_empty.json && esgf-fix-tool classify /tmp/qa_result_empty.json'
	docker run --rm $(AS_USER) -v $(CURDIR)/tests:/tests -v $(CURDIR)/tests/golden:/data $(IMAGE) bash /tests/run_pipeline.sh
	docker run --rm $(AS_USER) --network none -v $(CURDIR)/tests:/tests:ro -v $(CURDIR)/tests/golden:/data:ro \
		$(IMAGE) bash /tests/run_qa_fix_loop.sh

unit: ## Unit tests of the toolbox scripts, on the host (no image needed)
	uvx --python 3.12 --with typer pytest -q tests/test_versions.py tests/test_etb.py

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

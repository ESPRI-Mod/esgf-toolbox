IMAGE_NAME    := esgf-toolbox
VARIANT       ?= latest
IMAGE         := $(IMAGE_NAME):$(VARIANT)
ARCHIVE       := $(IMAGE_NAME)$(if $(filter-out latest,$(VARIANT)),-$(VARIANT)).tar.gz

.PHONY: build test run save help

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  make %-10s %s\n", $$1, $$2}'

build: ## Build the Docker image (VARIANT=latest|slim)
	docker build --target $(VARIANT) -t $(IMAGE) .

test: ## Run golden file pipeline (QA/QC + esgdrs + esgmapfile) in container
	docker run --rm -v $(CURDIR)/tests:/tests -v $(CURDIR)/tests/golden:/data $(IMAGE) bash /tests/run_pipeline.sh

run: ## Run interactive shell with data: make run DATA=/path/to/files
ifndef DATA
	$(error DATA is required. Usage: make run DATA=/path/to/files)
endif
	docker run --rm -it -v $(DATA):/data $(IMAGE) bash

save: ## Export image to compressed tar archive
	docker save $(IMAGE) | gzip > $(ARCHIVE)
	@echo "Saved to $(ARCHIVE) ($$(du -h $(ARCHIVE) | cut -f1))"

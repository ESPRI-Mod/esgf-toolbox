IMAGE_NAME    := esgf-toolbox
ARCHIVE       := $(IMAGE_NAME).tar.gz

.PHONY: build test run save help

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  make %-10s %s\n", $$1, $$2}'

build: ## Build the Docker image
	docker build -t $(IMAGE_NAME) .

test: ## Run golden file pipeline (QA/QC + esgdrs + esgmapfile) in container
	docker run --rm -v $(CURDIR)/tests:/tests -v $(CURDIR)/tests/golden:/data $(IMAGE_NAME) bash /tests/run_pipeline.sh

run: ## Run interactive shell with data: make run DATA=/path/to/files
ifndef DATA
	$(error DATA is required. Usage: make run DATA=/path/to/files)
endif
	docker run --rm -it -v $(DATA):/data $(IMAGE_NAME) bash

save: ## Export image to compressed tar archive
	docker save $(IMAGE_NAME):latest | gzip > $(ARCHIVE)
	@echo "Saved to $(ARCHIVE) ($$(du -h $(ARCHIVE) | cut -f1))"

# runpod_testbed — testbed control
#
# Run from anywhere:  make -C runpod_testbed <target>
# The scripts run from the repo root (imports resolve as runpod_testbed.*,
# state lands in ./data), so every recipe cd's there first.
#
#   make test                       unit tests (no Runpod, no spend)
#   make image                      build-linux + docker build the cache image
#   make up                         provision 3 pods + deploy 3 Flash endpoints
#   make dry-run CACHE_URL=http://<pod>:8000
#   make scrape RUNID=<id>          poll pod metrics -> parquet (foreground)
#   make drive  RUNID=<id>          run the workload
#   make report RUNID=<id>          build the report
#   make down   RUNID=<id>          tear everything down (MANDATORY after a run)
#
# RUNID is the timestamp `make up` prints — post-up targets need it, e.g.
#   make -C runpod_testbed drive RUNID=20260919-120000
# Override the image tag with IMAGE=... (match your config.toml `cache_image`).

SHELL := /bin/bash
.ONESHELL:
.DEFAULT_GOAL := help

ROOT     := $(abspath $(CURDIR)/..)
CONFIG   ?= runpod_testbed/config.toml
CONFIG_EXAMPLE := runpod_testbed/config.example.toml
IMAGE    ?= xet-cache-testbed:latest
STATE     = data/state-$(RUNID).json
RUN      := uv run --with runpod

.PHONY: help setup test image up dry-run scrape drive report down _needs-runid _needs-cacheurl

help:
	@echo "runpod_testbed — targets (run: make -C runpod_testbed <target>):"
	@echo "  make setup                     install flash CLI + scaffold config.toml (idempotent)"
	@echo "  make test                      unit tests (no Runpod, no spend)"
	@echo "  make image [IMAGE=...]         build-linux + docker build the cache image"
	@echo "  make up                        provision 3 pods + deploy 3 Flash endpoints"
	@echo "  make dry-run CACHE_URL=...     validate wiring against one pod (no Flash)"
	@echo "  make scrape RUNID=<id>         poll pod metrics -> parquet (foreground; & to bg)"
	@echo "  make drive  RUNID=<id>         run the multi-model workload"
	@echo "  make report RUNID=<id>         build latency + peering report"
	@echo "  make down   RUNID=<id>         tear down endpoints + pods (MANDATORY)"
	@echo ""
	@echo "RUNID is the timestamp 'make up' prints. Every run costs money until 'make down'."

# One-time, idempotent, non-destructive: installs the flash CLI if missing and
# scaffolds config.toml from the example without clobbering an existing one.
# The interactive `flash login` and the env exports can't be done from make —
# they're printed as the remaining manual steps.
setup:
	@if command -v flash >/dev/null 2>&1; then echo "flash CLI: present"; \
	else echo "flash CLI: installing via uv..."; uv tool install runpod-flash; fi
	@cd $(ROOT) && if [ -f $(CONFIG) ]; then echo "config: $(CONFIG) exists (left as-is)"; \
	else cp $(CONFIG_EXAMPLE) $(CONFIG) && echo "config: created $(CONFIG) from example — edit it"; fi
	@echo ""
	@echo "remaining manual steps (make can't do these for you):"
	@echo "  1. flash login                          # authenticate the flash CLI"
	@echo "  2. export RUNPOD_API_KEY=...            # runpod-python SDK + pod self-config"
	@echo "     export HF_TOKEN=...                  # throwaway/scoped HF token"
	@echo "     export SHIM_AUTH_TOKEN=...           # peer-channel bearer secret"
	@echo "  3. edit $(CONFIG)   # registry, cache_image, models, overlap"

test:
	@cd $(ROOT) && uv run --with pytest pytest runpod_testbed/tests -v

# build-linux (root Makefile) drops xetcache-linux-amd64 at the repo root, which
# the Dockerfile COPYs; then build the image from the repo-root context.
image:
	@cd $(ROOT) && $(MAKE) build-linux
	@cd $(ROOT) && docker build --platform linux/amd64 \
		-f runpod_testbed/provision/cache.Dockerfile -t $(IMAGE) .
	@echo "built image $(IMAGE) — push it and set config.toml cache_image to match"

up:
	@cd $(ROOT) && $(RUN) runpod_testbed/provision/up.py $(CONFIG)

dry-run: _needs-cacheurl
	@cd $(ROOT) && uv run --with huggingface_hub --with hf_xet \
		runpod_testbed/drive/run.py --dry-run $(CACHE_URL) dryrun

scrape: _needs-runid
	@cd $(ROOT) && $(RUN) --with pyarrow runpod_testbed/harvest/scrape.py $(STATE)

drive: _needs-runid
	@cd $(ROOT) && $(RUN) runpod_testbed/drive/run.py $(CONFIG) $(RUNID)

report: _needs-runid
	@cd $(ROOT) && uv run --with pyarrow --with matplotlib \
		runpod_testbed/harvest/report.py $(RUNID)

down: _needs-runid
	@cd $(ROOT) && $(RUN) runpod_testbed/provision/down.py $(STATE)

_needs-runid:
	@[ -n "$(RUNID)" ] || { echo "error: set RUNID=<timestamp printed by 'make up'>"; exit 1; }

_needs-cacheurl:
	@[ -n "$(CACHE_URL)" ] || { echo "error: set CACHE_URL=http://<pod-addr>:8000"; exit 1; }

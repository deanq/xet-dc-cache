# runpod_testbed — testbed control
#
# Run from anywhere:  make -C runpod_testbed <target>
# The scripts run from the repo root (imports resolve as runpod_testbed.*,
# state lands in ./data), so every recipe cd's there first.
#
#   make test                       unit tests (no Runpod, no spend)
#   make image                      build-linux + docker build the cache image
#   make up [MECHANISM=shim|volumecache|modelstore]   provision mechanism + baseline endpoint
#   make dry-run CACHE_URL=http://<pod>:8000
#   make scrape RUNID=<id>          poll pod metrics -> parquet (no-op for mechanisms without metrics)
#   make drive  RUNID=<id>          run the workload
#   make report RUNID=<id>          build data/report-<mechanism>-<id>.md
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
ENV_FILE ?= $(CURDIR)/.env
IMAGE    ?= xet-cache-testbed:latest
STATE     = data/state-$(RUNID).json
RUN      := uv run --with runpod

# Mechanism under test. Empty = the `mechanism` key in config.toml decides;
# set MECHANISM=volumecache|modelstore|shim to override for this invocation.
# Exported so config.load(mechanism_override=os.environ.get("MECHANISM")) sees it.
MECHANISM ?=
ifneq ($(MECHANISM),)
export MECHANISM
endif

# Source runpod_testbed/.env into the recipe shell. It holds the controller-side
# secrets (RUNPOD_API_KEY, HF_TOKEN, SHIM_AUTH_TOKEN) — uv run does NOT read it.
# Best-effort: absent file is fine (downstream validation fails loudly if a
# required var is actually missing).
LOADENV = { set -a; . "$(ENV_FILE)" 2>/dev/null || true; set +a; }

.PHONY: help setup test image up dry-run scrape drive report down demo demo-up demo-run \
        _preflight _needs-runid _needs-cacheurl _needs-lastrunid

help:
	@echo "runpod_testbed — targets (run: make -C runpod_testbed <target>):"
	@echo "  make setup                     install flash CLI + scaffold config.toml (idempotent)"
	@echo "  make test                      unit tests (no Runpod, no spend)"
	@echo "  make image [IMAGE=...]         build-linux + docker build the cache image"
	@echo ""
	@echo "  DEMO (recommended flow):"
	@echo "    make demo-up                 pre-flight + provision; run ~10 min before the demo"
	@echo "    make demo-run                run workload, print the report, then auto-teardown"
	@echo "    make demo                    demo-up + demo-run back-to-back (for rehearsal)"
	@echo ""
	@echo "  Manual pieces:"
	@echo "    make up [MECHANISM=x]        provision the mechanism under test + the baseline endpoint"
	@echo "    make dry-run CACHE_URL=...   validate wiring against one pod (no Flash)"
	@echo "    make scrape RUNID=<id>       poll pod metrics -> parquet (foreground; & to bg)"
	@echo "    make drive  RUNID=<id>       run the multi-model workload"
	@echo "    make report RUNID=<id>       build the shared timing report (+ shim peering sections)"
	@echo "    make down   RUNID=<id>       tear down endpoints + pods (MANDATORY)"
	@echo ""
	@echo "RUNID is the timestamp 'make up' prints (demo targets read it from data/last-runid)."
	@echo "Every run costs money until teardown; demo-run always tears down, even on failure."

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
	@cd $(ROOT) && uv run --with pytest --with pyarrow pytest runpod_testbed/tests -v

# build-linux (root Makefile) drops xetcache-linux-amd64 at the repo root, which
# the Dockerfile COPYs; then build the image from the repo-root context.
image:
	@cd $(ROOT) && $(MAKE) build-linux
	@cd $(ROOT) && docker build --platform linux/amd64 \
		-f runpod_testbed/provision/cache.Dockerfile -t $(IMAGE) .
	@echo "built image $(IMAGE) — push it and set config.toml cache_image to match"

up:
	@cd $(ROOT) && $(LOADENV) && $(RUN) python -m runpod_testbed.provision.up $(CONFIG)

dry-run: _needs-cacheurl
	@cd $(ROOT) && $(LOADENV) && uv run --with huggingface_hub --with hf_xet \
		python -m runpod_testbed.drive.run --dry-run $(CACHE_URL) dryrun

scrape: _needs-runid
	@cd $(ROOT) && $(LOADENV) && $(RUN) --with pyarrow python -m runpod_testbed.harvest.scrape $(STATE)

drive: _needs-runid
	@cd $(ROOT) && $(LOADENV) && $(RUN) python -m runpod_testbed.drive.run $(CONFIG) $(RUNID)

report: _needs-runid
	@cd $(ROOT) && uv run --with pyarrow --with matplotlib \
		python -m runpod_testbed.harvest.report $(RUNID)

down: _needs-runid
	@cd $(ROOT) && $(LOADENV) && $(RUN) python -m runpod_testbed.provision.down $(STATE)

# --- Demo orchestration ---------------------------------------------------
# Pre-flight fails loudly (missing secrets / bad config / orphaned resources)
# BEFORE spending money, so problems show up at your desk, not on stage.
_preflight:
	@cd $(ROOT) && $(LOADENV) && $(RUN) python -m runpod_testbed.provision.preflight

# Run ~10 min before the demo: check, provision, wait until healthy. up.py
# writes data/last-runid so demo-run needs no RUNID argument.
demo-up: _preflight
	@cd $(ROOT) && $(LOADENV) && $(RUN) python -m runpod_testbed.provision.up $(CONFIG)
	@echo ""
	@echo "demo fleet is UP. When the audience is ready:  make -C runpod_testbed demo-run"

# Run live: scrape (bg) -> drive -> stop scrape (flush) -> report -> print it.
# A trap tears the fleet down on EXIT — success or failure — so nothing is left
# billing even if a step fails mid-demo.
demo-run: _needs-lastrunid
	@cd $(ROOT) && $(LOADENV) && set -e; \
	RUNID=$$(cat data/last-runid); \
	echo "demo-run RUNID=$$RUNID"; \
	$(RUN) --with pyarrow python -m runpod_testbed.harvest.scrape data/state-$$RUNID.json & \
	SCRAPE_PID=$$!; \
	trap 'echo; echo "=== teardown ==="; kill -TERM $$SCRAPE_PID 2>/dev/null || true; wait $$SCRAPE_PID 2>/dev/null || true; $(RUN) python -m runpod_testbed.provision.down data/state-$$RUNID.json' EXIT; \
	sleep 2; \
	$(RUN) python -m runpod_testbed.drive.run $(CONFIG) $$RUNID; \
	kill -TERM $$SCRAPE_PID 2>/dev/null || true; wait $$SCRAPE_PID 2>/dev/null || true; \
	uv run --with pyarrow --with matplotlib python -m runpod_testbed.harvest.report $$RUNID; \
	echo; echo "========== DEMO REPORT =========="; cat data/report-*-$$RUNID.md; echo "================================="

# All-in-one for rehearsal (provision + run back-to-back).
demo:
	@$(MAKE) demo-up
	@$(MAKE) demo-run

_needs-runid:
	@[ -n "$(RUNID)" ] || { echo "error: set RUNID=<timestamp printed by 'make up'>"; exit 1; }

_needs-lastrunid:
	@[ -s $(ROOT)/data/last-runid ] || { echo "error: no data/last-runid — run 'make demo-up' first"; exit 1; }

_needs-cacheurl:
	@[ -n "$(CACHE_URL)" ] || { echo "error: set CACHE_URL=http://<pod-addr>:8000"; exit 1; }

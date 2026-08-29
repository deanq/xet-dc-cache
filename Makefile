# xet-dc-cache — shim control
#
#   make start     # start the cache in the background (survives closing the terminal)
#   make stop      # stop it
#   make restart   # stop + start
#   make status    # running? + healthz
#   make logs      # follow the log
#   make metrics   # current hit rate / bytes saved
#   make run       # run in the FOREGROUND (Ctrl-C to stop)
#   make test      # unit + regression tests
#   make clean-cache   # stop and wipe the cached xorbs
#
# Override any setting inline, e.g.:  make start PORT=9000 CACHE_DIR=/tmp/xet

SHELL := /bin/bash
.ONESHELL:
.DEFAULT_GOAL := help

PORT        ?= 8000
PUBLIC_HOST ?= localhost
PUBLIC_BASE ?= http://$(PUBLIC_HOST):$(PORT)
CACHE_DIR   ?= $(HOME)/.cache/xet-dc-cache
MAX_GIB     ?= 100
GO_DIR      := shim-go
BIN         := $(GO_DIR)/xetcache
LINUX_BIN   := xetcache-linux-amd64
LOG         ?= $(CACHE_DIR)/shim.log

.PHONY: help build build-linux start stop restart status logs metrics run test test-e2e clean-cache

help:
	@echo "xet-dc-cache — targets:"
	@echo "  make start        start in background   (PUBLIC_BASE=$(PUBLIC_BASE))"
	@echo "  make stop         stop"
	@echo "  make restart      stop + start"
	@echo "  make status       running? + healthz"
	@echo "  make logs         follow the log ($(LOG))"
	@echo "  make metrics      hit rate / bytes saved"
	@echo "  make build-linux  cross-compile $(LINUX_BIN) for DC deploy"
	@echo "  make run          run in FOREGROUND (Ctrl-C to stop)"
	@echo "  make test         unit + regression tests"
	@echo "  make test-e2e     cross-DC peering e2e (Docker; needs network)"
	@echo "  make clean-cache  stop + wipe $(CACHE_DIR)"
	@echo ""
	@echo "Client (other device):  export HF_ENDPOINT=$(PUBLIC_BASE)"

build:
	@cd $(GO_DIR) && go build -o xetcache . && echo "built $(BIN)"

# Cross-compile a static linux/amd64 binary for DC hosts (deploy/ + systemd).
# No cgo in this codebase, so CGO_ENABLED=0 yields a single portable artifact.
build-linux:
	@cd $(GO_DIR) && CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -o ../$(LINUX_BIN) . \
		&& echo "built $(LINUX_BIN) ($$(file ../$(LINUX_BIN) 2>/dev/null | cut -d, -f1-2 || echo linux/amd64))"

start: stop build
	@mkdir -p "$(CACHE_DIR)"
	@PUBLIC_BASE=$(PUBLIC_BASE) CACHE_DIR=$(CACHE_DIR) \
		XORB_CACHE_MAX_GIB=$(MAX_GIB) PORT=$(PORT) \
		nohup ./$(BIN) > "$(LOG)" 2>&1 < /dev/null & \
		disown
	@echo "starting shim (PORT=$(PORT), PUBLIC_BASE=$(PUBLIC_BASE), CACHE_DIR=$(CACHE_DIR))"
	@for i in $$(seq 1 20); do \
		curl -s -m1 http://127.0.0.1:$(PORT)/healthz >/dev/null 2>&1 && break || sleep 0.5; \
	done
	@$(MAKE) --no-print-directory status

stop:
	@pids=$$(lsof -ti tcp:$(PORT) 2>/dev/null); \
	if [ -n "$$pids" ]; then \
		kill $$pids 2>/dev/null; sleep 1; \
		left=$$(lsof -ti tcp:$(PORT) 2>/dev/null); \
		[ -n "$$left" ] && kill -9 $$left 2>/dev/null || true; \
		echo "stopped (pids $$(echo $$pids | tr '\n' ' '))"; \
	else echo "not running"; fi

restart: stop start

status:
	@if lsof -ti tcp:$(PORT) >/dev/null 2>&1; then \
		echo "shim: RUNNING on :$(PORT) (pid $$(lsof -ti tcp:$(PORT) | tr '\n' ' '))"; \
		curl -s -m3 http://127.0.0.1:$(PORT)/healthz && echo "  <- healthz" || echo "  (healthz not responding)"; \
	else echo "shim: stopped"; fi

logs:
	@touch "$(LOG)"; tail -f "$(LOG)"

metrics:
	@curl -s -m5 http://127.0.0.1:$(PORT)/metrics | python3 -m json.tool || echo "shim not reachable on :$(PORT)"

run: stop build
	@mkdir -p "$(CACHE_DIR)"
	@echo "running in foreground on :$(PORT) — Ctrl-C to stop"
	@PUBLIC_BASE=$(PUBLIC_BASE) CACHE_DIR=$(CACHE_DIR) \
		XORB_CACHE_MAX_GIB=$(MAX_GIB) PORT=$(PORT) ./$(BIN)

test:
	@cd $(GO_DIR) && go test ./...

# Cross-DC peering end-to-end: builds 3 peered shim containers and drives real
# hf_hub_downloads through them (peer warm hit + WAN fallback + resilience).
# Needs Docker + network. Override the model with SMOKE_REPO/SMOKE_REV/SMOKE_PATH.
test-e2e:
	@uv run deploy/e2e/run_e2e.py

clean-cache: stop
	@if [ -z "$(CACHE_DIR)" ]; then echo "CACHE_DIR empty; refusing"; exit 1; fi
	@rm -rf "$(CACHE_DIR)"/* && echo "cache cleared: $(CACHE_DIR)"

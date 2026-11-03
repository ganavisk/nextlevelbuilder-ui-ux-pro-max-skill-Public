VENV ?= .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

.PHONY: check-keys lock install up down db dev worker confirmations crm seed provision migrate revision test lint fmt dashboard-install dashboard-dev dashboard-build

# Regenerate the runtime dependency lock the production image installs from.
# Resolves apps/api's base dependencies in a throwaway venv, so dev tools
# never leak into the image.
lock:
	rm -rf .lockenv && python3 -m venv .lockenv
	.lockenv/bin/pip install -q --upgrade pip
	python3 -c "import tomllib; print('\n'.join(tomllib.load(open('apps/api/pyproject.toml','rb'))['project']['dependencies']))" > .lockenv/reqs.txt
	.lockenv/bin/pip install -q -r .lockenv/reqs.txt
	.lockenv/bin/pip freeze --exclude-editable | grep -vi '^receptionist' > apps/api/requirements.lock
	rm -rf .lockenv

install:
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -e "apps/api[dev]"

up:
	docker compose up --build -d

down:
	docker compose down

db:
	docker compose up -d postgres redis

dev:
	cd apps/api && ../../$(PY) -m uvicorn app.main:app --reload --port 8000

worker:
	cd apps/api && ../../$(PY) -m app.workers.post_call

# Separate process from `worker`: confirmations must keep draining even when
# post-call summarization is backed up behind a slow LLM.
confirmations:
	cd apps/api && ../../$(PY) -m app.workers.confirmations

# Separate again: a tenant's CRM being down must not stall confirmations.
crm:
	cd apps/api && ../../$(PY) -m app.workers.crm_sync

# Confirm every API key works before dialling in: one cheap read-only call per
# provider, keys never printed. Exits non-zero if a test call would fail.
check-keys:
	cd apps/api && ../../$(PY) -m app.preflight

# Seed a demo tenant from config/tenants/example-$(TENANT).yaml.
#   make seed                                      # dental
#   make seed TENANT=auto NUMBER=+15550100003      # auto repair shop
TENANT ?= dental
NUMBER ?= +15550100001
seed:
	cd apps/api && ../../$(PY) scripts/seed_demo.py ../../config/tenants/example-$(TENANT).yaml --number $(NUMBER)

# Connect a tenant's numbers to the voice vendor (first time), or force a full
# re-push. Dashboard changes are pushed automatically by the crm worker; re-run
# this after changes made outside the dashboard (YAML re-seed, prompt or tool
# code). Override: make provision SLUG=...
SLUG ?= bright-smile-dental
provision:
	cd apps/api && ../../$(PY) scripts/provision_voice.py --slug $(SLUG)

migrate:
	cd apps/api && ../../$(PY) -m alembic upgrade head

revision:
	cd apps/api && ../../$(PY) -m alembic revision -m "$(m)"

test:
	cd apps/api && ../../$(PY) -m pytest -q

lint:
	$(VENV)/bin/ruff check apps/api
	$(VENV)/bin/ruff format --check apps/api

fmt:
	$(VENV)/bin/ruff check --fix apps/api
	$(VENV)/bin/ruff format apps/api

dashboard-install:
	cd apps/dashboard && npm install

dashboard-dev:
	cd apps/dashboard && npm run dev

# `next build` also type-checks.
dashboard-build:
	cd apps/dashboard && npm run build

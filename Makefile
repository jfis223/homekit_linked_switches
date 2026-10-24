VENV := .venv
VENV_PY := $(VENV)/bin/python
PYTHON_VERSION := 3.14
RUFF := $(shell [ -x $(VENV)/bin/ruff ] && echo $(VENV)/bin/ruff || echo ruff)
PYTEST := $(shell [ -x $(VENV)/bin/pytest ] && echo $(VENV)/bin/pytest || echo pytest)

COMPONENT := custom_components/homekit
PATCH := patches/linked-switches.patch

# Where a Home Assistant config directory lives, for `make deploy`.
HA_CONFIG ?= /config

.PHONY: help install lint test check patch sync deploy clean

help:
	@echo "install  Create $(VENV) with uv and install the test dependencies"
	@echo "lint     Run ruff over the tests and scripts"
	@echo "test     Run the test suite"
	@echo "check    lint + test"
	@echo "patch    Regenerate $(PATCH) from the component against upstream"
	@echo "sync     Re-import upstream at HA_VERSION and re-apply $(PATCH)"
	@echo "deploy   Copy the component into HA_CONFIG/custom_components (default $(HA_CONFIG))"

install:
	uv venv --python $(PYTHON_VERSION) $(VENV)
	uv pip install --python $(VENV_PY) -r requirements-test.txt

lint:
	$(RUFF) check .

test:
	PYTHONSAFEPATH=1 $(PYTEST)

check: lint test

# The upstream version comes from the manifest, minus this repo's patch level.
patch:
	scripts/sync.sh --diff-only "$$(sed -n 's/.*"version": *"\([0-9]*\.[0-9]*\.[0-9]*\).*/\1/p' $(COMPONENT)/manifest.json)"

sync:
	@test -n "$(HA_VERSION)" || { echo "usage: make sync HA_VERSION=2026.11.0" >&2; exit 1; }
	scripts/sync.sh "$(HA_VERSION)"

# rsync --delete from a whole directory, so a file upstream removed does not
# linger in the install and keep loading.
deploy:
	@test -d "$(HA_CONFIG)" || { echo "No such directory: $(HA_CONFIG)" >&2; exit 1; }
	mkdir -p "$(HA_CONFIG)/custom_components/homekit"
	rsync -a --delete --exclude __pycache__ "$(COMPONENT)/" "$(HA_CONFIG)/custom_components/homekit/"

clean:
	rm -rf $(VENV) .pytest_cache .ruff_cache
	find . -name '__pycache__' -prune -not -path './.venv/*' -exec rm -rf {} +

VENV ?= .venv
PY := $(VENV)/bin/python

.PHONY: venv test e2e

$(PY): requirements.txt requirements-dev.txt
	uv venv -q --allow-existing $(VENV) || python3 -m venv $(VENV)
	uv pip install -q -p $(VENV) -r requirements-dev.txt || $(PY) -m pip install -q -r requirements-dev.txt
	@touch $(PY)

venv: $(PY)

## Unit tests (Shlink mocked, no network)
test: $(PY)
	$(PY) -m pytest

## End-to-end tests against a throwaway Shlink container bound to 127.0.0.1
e2e: $(PY)
	$(PY) -m pytest -m e2e

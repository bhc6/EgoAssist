.PHONY: install run test verify build clean

PY ?= python3
VENV ?= .venv
BIN = $(VENV)/bin

install:
	$(PY) -m venv $(VENV)
	$(BIN)/pip install -e ".[dev]"
	@test -f .env || cp .env.example .env

run:
	$(BIN)/egoassist --host 127.0.0.1 --port 8770

test:
	$(BIN)/pytest

verify:
	$(BIN)/python scripts/verify_egoassist.py

build:
	$(BIN)/python -m build

clean:
	rm -rf build dist .pytest_cache .egoassist **/__pycache__ *.egg-info

# zai_python_helper — developer tasks.
# Targets are thin shims; real logic lives in tools/ and pyproject.toml.

PYTHON ?= python3

.PHONY: install dev test lint docs docs-check clean

# Editable install with dev + docs extras (one-shot bootstrap for local work).
install:
	$(PYTHON) -m pip install -e ".[dev,docs]"

# Alias kept for muscle-memory; same as `install`.
dev: install

test:
	pytest -m "not e2e"

lint:
	ruff check .

# #19 — regenerate machine-readable docs from the current code.
# Source of truth = docstrings/type hints; output = docs/api/*.md + llms.txt.
# Idempotent + sorted so `git diff` is stable for the CI sync check.
docs:
	$(PYTHON) tools/gen_docs.py

# CI gate: docs must be regenerated and committed alongside code changes.
# Fails (non-zero) if the checked-in artifacts are out of sync with the code.
docs-check: docs
	@git diff --exit-code docs/ llms.txt \
		|| (echo "::error::Docs are stale. Run 'make docs' and commit docs/ + llms.txt." && exit 1)

clean:
	rm -rf build dist *.egg-info src/*.egg-info

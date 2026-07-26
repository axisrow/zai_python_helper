# zai_python_helper — developer tasks.
# Targets are thin shims; real logic lives in tools/ and pyproject.toml.

PYTHON ?= python3

.PHONY: install dev test lint docs docs-check human-docs-check clean

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

# #20 — human-docs i18n structure-sync gate. English (docs/en/) is the source
# of truth; ru/ and zh/ must contain the same set of .md files. Fails (non-zero)
# on any missing or extra file so the language selector never leads to a 404.
human-docs-check:
	$(PYTHON) tools/check_human_docs_structure.py

clean:
	rm -rf build dist *.egg-info src/*.egg-info

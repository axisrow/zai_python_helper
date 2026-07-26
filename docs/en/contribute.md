# Contribute

`zai_python_helper` is MIT-licensed and welcome to contributions. This page is
the short version of the dev setup; the [Architecture](../ARCHITECTURE.md) is
the long version of the design.

## Dev setup

```bash
git clone https://github.com/axisrow/zai_python_helper.git
cd zai_python_helper
pip install -e ".[dev,docs]"
```

`dev` = pytest, ruff, build tooling. `docs` = griffe + the mkdocs site tooling
([this site](https://axisrow.github.io/zai_python_helper/)).

## Run the tests

```bash
make test            # pytest, excluding e2e
pytest -m "not e2e"  # equivalent
```

End-to-end tests (marked `e2e`) are opt-in.

## Lint

```bash
make lint    # ruff check .
ruff check . # equivalent
```

## Keep the docs in sync

The API reference (`docs/api/zai_python_helper.md`) and `llms.txt` are
**auto-generated from the source** — docstrings and type hints are the source of
truth. CI fails if the checked-in docs drift from the code.

After you change a docstring or signature:

```bash
make docs          # regenerate docs/api/ + llms.txt
git add docs/ llms.txt
```

The `make docs` target is idempotent and sorted, so `git diff` stays clean.

## Preview this site locally

```bash
mkdocs serve
```

Open the printed URL (default <http://127.0.0.1:8000>). The
[language selector](https://github.com/ultrabug/mkdocs-static-i18n) switches
between English / Русский / 中文.

## Human-docs i18n rules

- **English is the source of truth** (`docs/human/en/`). Translate into
  `docs/human/ru/` and `docs/human/zh/`.
- **Structure is synchronized.** A CI check verifies `ru/` and `zh/` contain the
  same set of `.md` files as `en/`. If you add a page to `en/`, add the matching
  page (even a stub) to `ru/` and `zh/` in the same PR.
- **Don't blindly machine-translate.** A machine draft is an acceptable starting
  point, but mark anything a native speaker should review with
  `<!-- TODO(i18n): review -->`.

## Commit style

Use [Conventional Commits](https://www.conventionalcommits.org/) — `feat:`,
`fix:`, `docs:`, etc. Keep commits focused.

## Reporting issues

Open an issue on [GitHub](https://github.com/axisrow/zai_python_helper/issues).
For security-sensitive reports, prefer a private channel over a public issue.

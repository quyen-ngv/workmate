---
name: python-project-standard
description: Office standard for Python projects (layout, uv, pyproject, typing, pytest, ruff, README). Use when creating or changing Python code.
---

# Python project standard

## Layout
```
<project>/
  pyproject.toml        # [project] name, version, requires-python >=3.11, dependencies; [project.scripts] for CLIs
  README.md             # what it does, install, run, test, configuration
  src/<package>/        # importable code (__init__.py, __main__.py for `python -m <package>`)
  tests/                # pytest tests mirroring the package (test_<module>.py)
  .gitignore            # .venv/, __pycache__/, .pytest_cache/, dist/, *.egg-info, out/
```

## Commands (Windows-friendly)
- New project: `uv init --package <name>` (or write pyproject.toml by hand), then `uv add <dep>`,
  `uv add --dev pytest ruff`.
- Run: `uv run <script-name>` or `uv run python -m <package>`. Tests: `uv run pytest -q`. Lint: `uv run ruff check .`
- Never install into the system Python; never commit `.venv`. Do commit `uv.lock` (reproducible installs).

## Code
- Type hints on public functions; small functions; names that say what they do; no magic numbers.
- Validate inputs at the boundary (CLI args, files, HTTP) and fail with a clear message and non-zero exit code.
- Read paths and settings from arguments or environment variables, not hard-coded absolute paths.
- Use `pathlib`, `logging` (not print for diagnostics), UTF-8 for every file read/write.
- No secrets in code or logs.

## Tests
- Every behaviour in the acceptance criteria has a test; include edge and error cases.
- Tests are deterministic (no network, fixed seeds, tmp_path for files).
- Done = `uv run pytest -q` passes in a clean checkout, and the README commands work as written.

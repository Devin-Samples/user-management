# Agent notes

Commands for verifying changes to this repo.

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Tests

```bash
# All unit tests (no network)
pytest -m "not live"

# Live tests (require .env with valid DEVIN_API_KEY)
pytest -m live
```

## Lint

```bash
ruff check src tests
```

## Smoke test the CLI

```bash
user-management --help
user-management verify          # requires DEVIN_API_KEY
user-management doctor          # requires DEVIN_API_KEY
user-management github-sync --help
```

## Layout

- `src/user_management/core/` — shared sync Devin v3 API client + pydantic models + errors
- `src/user_management/bulk/` — CSV/XLSX-driven enterprise sync (ported from devin-bulk-manager)
- `src/user_management/github_sync/` — GitHub Team → Devin Org sync
- `src/user_management/doctor/` — diagnostic checks (Devin auth)
- `src/user_management/cli.py` — top-level argparse dispatcher

Everything goes through `core.client.DevinAPIClient`. There is no async code; everything uses `requests`.

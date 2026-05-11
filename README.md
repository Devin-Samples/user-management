# user-management

Unified tool for managing users and organizations in a Devin enterprise. Two modules under one CLI:

| Module | Source of truth | Docs |
|---|---|---|
| **`bulk`** | A CSV (or XLSX) you maintain | [docs/bulk.md](docs/bulk.md) |
| **`github-sync`** | GitHub Teams | [docs/github-sync.md](docs/github-sync.md) |
| **`doctor`** | _(diagnostics)_ | [docs/bulk.md#troubleshooting](docs/bulk.md#troubleshooting) |

Both modules share a single Devin v3 API client (`requests`-based, sync), one unified `.env`, and one CLI entry point. Pick the module that matches your workflow and follow its quickstart.

---

## Install

```bash
git clone https://github.com/Devin-Samples/user-management.git
cd user-management

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e .

cp .env.example .env
# Edit .env and fill in DEVIN_API_KEY, DEVIN_API_BASE_URL, and (for github-sync) GITHUB_TOKEN
```

## Verify your setup

```bash
user-management verify        # prints enterprise state from the Devin API
user-management doctor        # checks Devin auth, GitHub token scopes, git connection
```

## Next steps

- **Adding hundreds of users to an org from a spreadsheet?** → [docs/bulk.md](docs/bulk.md)
- **Keeping a Devin org in sync with a GitHub Team?** → [docs/github-sync.md](docs/github-sync.md)

---

## CLI overview

```
user-management bulk apply         --orgs-file F --users-file F [--dry-run]
user-management bulk pull          [--orgs-out F] [--users-out F]
user-management bulk gen-deepwiki  --emails emails.txt --output users.csv
user-management bulk verify

user-management github-sync run    --config config.yaml [--dry-run] [--verbose]
user-management github-sync check  --config config.yaml

user-management doctor [--check devin-auth | github-token | github-app | email-visibility | all]
user-management verify             # alias for `bulk verify`
```

## Project layout

```
user-management/
├── src/user_management/
│   ├── core/          # shared sync Devin v3 API client, pydantic models, errors
│   ├── bulk/          # CSV/XLSX-driven sync
│   ├── github_sync/   # GitHub Teams sync
│   ├── doctor/        # diagnostic checks
│   └── cli.py         # top-level dispatcher
├── examples/          # orgs.csv, users.csv, config.yaml, config-multi-org.yaml
├── docs/              # bulk.md, github-sync.md, design.md, secrets.md
└── tests/             # unit + live-marked integration tests
```

## License

MIT

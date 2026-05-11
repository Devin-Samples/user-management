# user-management

Bulk CSV/XLSX-driven user and organization management for a Devin enterprise.

The `bulk` module treats a CSV (or XLSX) that you maintain as the source of truth and reconciles a Devin enterprise against it. See [docs/bulk.md](docs/bulk.md) for the full workflow.

---

## Install

```bash
git clone https://github.com/Devin-Samples/user-management.git
cd user-management

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e .

cp .env.example .env
# Edit .env and fill in DEVIN_API_KEY and DEVIN_API_BASE_URL
```

## Verify your setup

```bash
user-management verify        # prints enterprise state from the Devin API
user-management doctor        # checks Devin auth
```

## Next steps

- **Adding hundreds of users to an org from a spreadsheet?** → [docs/bulk.md](docs/bulk.md)

---

## CLI overview

```
user-management bulk apply         --orgs-file F --users-file F [--dry-run]
user-management bulk pull          [--orgs-out F] [--users-out F]
user-management bulk gen-deepwiki  --emails emails.txt --output users.csv
user-management bulk verify

user-management doctor [--check devin-auth | all]
user-management verify             # alias for `bulk verify`
```

## Project layout

```
user-management/
├── src/user_management/
│   ├── core/          # shared sync Devin v3 API client, pydantic models, errors
│   ├── bulk/          # CSV/XLSX-driven sync
│   ├── doctor/        # diagnostic checks
│   └── cli.py         # top-level dispatcher
├── examples/          # orgs.csv, users.csv, deepwiki-users.csv
├── docs/              # bulk.md
└── tests/             # unit + live-marked integration tests
```

## License

MIT

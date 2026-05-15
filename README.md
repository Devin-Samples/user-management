# user-management

[Devin-Powered] User, org, and GitHub team management for a Devin enterprise.

- **Bulk sync** — CSV/XLSX-driven user and org management. See [docs/bulk.md](docs/bulk.md).
- **GitHub Team sync** — Automatically sync GitHub team membership and repo access to Devin orgs. See [docs/github-team-sync.md](docs/github-team-sync.md).

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
- **Syncing GitHub teams to Devin orgs?** → [docs/github-team-sync.md](docs/github-team-sync.md)

---

## CLI overview

```
user-management bulk apply         --orgs-file F --users-file F [--dry-run]
user-management bulk pull          [--orgs-out F] [--users-out F]
user-management bulk gen-deepwiki  --emails emails.txt --output users.csv
user-management bulk verify

user-management github-sync        --config config.yaml [--dry-run] [--verbose]

user-management doctor [--check devin-auth | all]
user-management verify             # alias for `bulk verify`
```

## GitHub Team Sync — Quick Start

The `github-sync` module discovers GitHub teams and synchronises their
membership and repository access to Devin organizations.

### 1. Set up secrets

| Secret | Description |
|--------|-------------|
| `DEVIN_API_TOKEN` | Devin enterprise service-user API key |
| `GITHUB_TOKEN_<ORG>` | GitHub PAT per org (`read:org`; `admin:org` for SAML/audit-log) |

### 2. Create a config file

```bash
cp src/user_management/github_sync/config.yaml.example config.yaml
# Edit config.yaml with your GitHub org names, token env vars, and email domains
```

### 3. Run the sync

```bash
# Preview changes (no API writes)
user-management github-sync --config config.yaml --dry-run --verbose

# Apply changes
user-management github-sync --config config.yaml --verbose
```

For full details, architecture, and the Devin agent skill, see
[docs/github-team-sync.md](docs/github-team-sync.md).

---

## Project layout

```
user-management/
├── src/user_management/
│   ├── core/          # shared Devin v3 API client, pydantic models, errors
│   ├── bulk/          # CSV/XLSX-driven sync
│   ├── github_sync/   # GitHub Team → Devin Org sync
│   ├── doctor/        # diagnostic checks
│   └── cli.py         # top-level dispatcher
├── .devin/skills/     # Devin agent skills (github-team-sync.md)
├── examples/          # orgs.csv, users.csv, deepwiki-users.csv
├── docs/              # bulk.md, github-team-sync.md
└── tests/             # unit + live-marked integration tests
```

## Disclaimer

This is a sample project intended for demonstration and educational purposes.
It is not an official Cognition product and is provided as-is without warranty.
Review and adapt the code to your own security and compliance requirements
before using in production.

## License

MIT

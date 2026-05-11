# Bulk

Bulk-manage users and organizations in your Devin enterprise. Add hundreds of users to an organization — with optional DeepWiki-only access — all from a simple CSV spreadsheet.

---

## Quickstart: Add Users to an Org with DeepWiki Access

### Step 1 — Install

```bash
git clone https://github.com/Devin-Samples/user-management.git
cd user-management
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
```

### Step 2 — Configure

You need an admin enterprise service user API key. Generate one at **Enterprise Settings → Service Users** on your enterprise URL (e.g. `https://your-org.devinenterprise.com/`).

Both `cog_...` (enterprise) and `sk-...` (cloud) keys are accepted.

```bash
cp .env.example .env
```

Edit `.env`:

```bash
DEVIN_API_KEY=cog_your_key_here
DEVIN_API_BASE_URL=https://your-org.devinenterprise.com/
ORG_NAME=YourOrgName
```

The `ORG_NAME` variable sets the default org for all CSV operations. You can override it per-row in the CSV if needed.

### Step 3 — Verify your setup

```bash
user-management verify
```

You should see your service user identity, available roles, existing orgs, and git connections. If you see `401 Unauthorized`, double-check your API key.

### Step 4 — Create the org (if it doesn't exist yet)

Create a file called `orgs.csv` (or use `examples/orgs.csv`):

```csv
org_name
YourOrgName
```

Preview, then apply:

```bash
user-management bulk apply --orgs-file orgs.csv --dry-run
user-management bulk apply --orgs-file orgs.csv
```

### Step 5 — Prepare your user list

Create `emails.txt` with one email per line:

```
firstname.lastname@example.com
another.user@example.com
...
```

Generate the CSV:

```bash
user-management bulk gen-deepwiki --emails emails.txt --output users.csv
```

This produces a `users.csv` where every user gets `account_member` enterprise role and `org_deepwiki` org role in your default org. The org name comes from your `ORG_NAME` env variable.

<details>
<summary>Or create users.csv manually</summary>

```csv
email,enterprise_role,org_name,org_role
firstname.lastname@example.com,account_member,YourOrgName,org_deepwiki
another.user@example.com,account_member,YourOrgName,org_deepwiki
```

If you leave `org_name` empty, it defaults to the `ORG_NAME` env variable.
</details>

### Step 6 — Preview and apply

```bash
user-management bulk apply --orgs-file orgs.csv --users-file users.csv --dry-run
user-management bulk apply --orgs-file orgs.csv --users-file users.csv
```

The tool invites users in batches of 100 and assigns each to your org with the `org_deepwiki` role.

### Step 7 — Verify

```bash
user-management verify
```

**That's it.** Your users now have DeepWiki access in your org.

---

## How It Works

Your CSV is the **source of truth**. Each time you run the tool, it:

1. Fetches the current enterprise state from the Devin API
2. Computes the diff between the API state and your CSV
3. Applies only the necessary changes (create/update/delete)
4. Reports results

Re-running the same CSV produces **zero changes** (idempotent).

### Two modes

| Mode | Has `action` column? | Behavior |
|------|---------------------|----------|
| **Sync** (recommended) | No | CSV = desired state. Tool diffs and applies only necessary changes. |
| **Legacy** | Yes | Each row has an explicit `add` / `remove` / `update` action. |

---

## CSV Format

### Organizations

```csv
org_name,cycle_acu_limit,session_acu_limit,repos
YourOrgName,5000,100,
```

| Column | Required | Description |
|--------|----------|-------------|
| `org_name` | Yes (or set `ORG_NAME` env var) | Organization name |
| `cycle_acu_limit` | No | Max ACUs per billing cycle |
| `session_acu_limit` | No | Max ACUs per session |
| `repos` | No | GitHub repos, semicolon-separated (`owner/repo1;owner/repo2`) |

### Users

```csv
email,enterprise_role,org_name,org_role
user1@example.com,account_member,YourOrgName,org_deepwiki
admin@example.com,account_admin,,
```

| Column | Required | Description |
|--------|----------|-------------|
| `email` | Yes | User's email address |
| `enterprise_role` | No | `account_admin` or `account_member` (default: `account_member`) |
| `org_name` | No | Org to assign user to (defaults to `ORG_NAME` env var) |
| `org_role` | No | `org_admin`, `org_member`, or `org_deepwiki` (default: `org_member`) |

**Org roles:**

| Role | Can run Devin? | Can use DeepWiki? | Can manage org? |
|------|---------------|-------------------|-----------------|
| `org_admin` | Yes | Yes | Yes |
| `org_member` | Yes | Yes | No |
| `org_deepwiki` | No | Yes | No |

Users without an `org_name` (and no `ORG_NAME` env var) are added to the enterprise but not assigned to any org.

---

## CLI Reference

```
user-management bulk apply [OPTIONS]
user-management bulk pull [OPTIONS]
user-management bulk gen-deepwiki [OPTIONS]
user-management bulk verify
```

| Flag | Description |
|------|-------------|
| `bulk verify` | Test API key and show enterprise state |
| `bulk pull` | Export current state to CSV files |
| `bulk apply --orgs-file PATH` | Path to orgs CSV or XLSX |
| `bulk apply --users-file PATH` | Path to users CSV or XLSX |
| `bulk apply --dry-run` | Preview changes without applying |
| `bulk apply --output PATH` | Write results to CSV |
| `bulk pull --orgs-out PATH` | Output path for orgs (default: `orgs-current.csv`) |
| `bulk pull --users-out PATH` | Output path for users (default: `users-current.csv`) |
| `bulk pull --include-unmanaged-orgs` | Include non-convention orgs in `pull` |
| `bulk gen-deepwiki --emails PATH` | Path to a file with one email per line |
| `bulk gen-deepwiki --org NAME` | Org name (defaults to `ORG_NAME` env var) |
| `bulk gen-deepwiki --output PATH` | Output CSV path |

---

## Common Recipes

### Export current state

```bash
user-management bulk pull
```

### Remove users

Delete their rows from `users.csv` and re-run.

### Update ACU limits

Edit values in `orgs.csv` and re-run.

### Override org per user

Set `org_name` per row to assign users to different orgs:

```csv
email,enterprise_role,org_name,org_role
alice@example.com,account_member,YourOrgName,org_deepwiki
bob@example.com,account_member,OtherOrg,org_member
```

---

## Safeguards

- **Dry-run mode**: Always preview before applying with `--dry-run`
- **Non-convention org protection**: Orgs without `/` in the name are never deleted by sync mode
- **Validation**: Checks email format, role names, ACU limits, and duplicates before any API calls
- **Idempotent**: Re-running produces zero changes if state matches

---

## Project Structure

```
src/user_management/bulk/
├── cli.py            # `user-management bulk` argparse + subcommand dispatch
├── sync.py           # BulkManager: diff/apply orchestration + sync execution
├── spreadsheet.py    # CSV/XLSX parsers + writers (with auto-detect)
├── deepwiki.py       # `gen-deepwiki` helper
└── templates/        # orgs.csv, users.csv, deepwiki-users.csv
```

The bulk module talks to the Devin API through the shared `user_management.core.client.DevinAPIClient`.

---

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `401 Unauthorized` | Bad or expired API key | Check `DEVIN_API_KEY` in `.env` |
| `403 Forbidden` | Missing permission | Ensure service user has required permissions |
| `404 Not Found` | Resource doesn't exist | Verify org/user exists |
| `409 Conflict` | User already in org | Skipped automatically |
| `422 Validation Error` | Invalid request body | Check CSV data |
| `429 Rate Limited` | Too many requests | Auto-retries with exponential backoff |

You can also run `user-management doctor` for a high-level health check across Devin auth, GitHub token scopes, and git connection presence.

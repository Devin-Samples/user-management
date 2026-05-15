# GitHub Team → Devin Org Sync

## Task

Run the GitHub Team → Devin Org sync using the `github-sync` module in this
repository.

## Prerequisites

The following **org-level secrets** must be configured in Devin before running:

| Secret Name | Description |
|-------------|-------------|
| `DEVIN_ENTERPRISE_ADMIN_TOKEN` | Devin API service-user key with `ManageAccountMembership` and `ManageGitIntegrations` permissions |
| `GITHUB_TOKEN_<ORG>` | One GitHub PAT per org with `read:org` scope (add `admin:org` for SAML/audit-log email resolution) |

## Steps

1. **Clone the repo and install dependencies**
   ```bash
   git clone https://github.com/Devin-Samples/user-management.git
   cd user-management
   pip install -e .
   ```

2. **Retrieve secrets and set environment variables**
   ```bash
   export DEVIN_API_TOKEN="$DEVIN_ENTERPRISE_ADMIN_TOKEN"
   ```
   List all available secrets and identify any `GITHUB_TOKEN_*` env vars.
   Each one corresponds to a GitHub org PAT.

   Check for an existing `sync-state.json` file — this is the consolidated
   state file that maps `{GitHub org} → {team_slug → {org_id, cached_org_name}}`.
   It prevents duplicate Devin orgs when orgs are renamed in the Devin UI.

3. **Create or update `config.yaml`**

   Copy the example config and edit for your GitHub orgs:
   ```bash
   cp src/user_management/github_sync/config.yaml.example config.yaml
   ```

   Update the `github_orgs` list with your org names, token env vars,
   skip patterns, and allowed email domains. See `config.yaml.example`
   for all available options.

4. **Run the sync**
   ```bash
   user-management github-sync --config config.yaml --verbose
   ```
   Add `--dry-run` to preview changes without making any API calls.

5. **Report findings for each org**

   After the sync run, report to the user:
   - **Orgs created**: Name and ID of each new Devin org
   - **Orgs matched by state file**: Orgs found via state file (indicating rename)
   - **Members synced**: Count of added, removed, skipped per team. Name any
     users that were auto-invited and the email used.
   - **Repo permissions synced**: Which repos were added to each org and which
     git connection was used
   - **Errors**: Any failures with full detail
   - **Undo instructions**: Provide API calls to reverse any changes

6. **Commit the updated state file** if it changed (new orgs created or
   org names updated).

## Secrets Needed

- `DEVIN_ENTERPRISE_ADMIN_TOKEN` — Devin API service-user key
  (export as `DEVIN_API_TOKEN`)
- One `GITHUB_TOKEN_*` env var per GitHub org (PAT with `read:org` scope;
  `admin:org` for SAML/audit log email resolution)

## Key Behaviors

- **State file (`sync-state.json`)**: Consolidated JSON keyed by GitHub org.
  Each entry maps `team_slug → {org_id, cached_org_name}`. Orgs are resolved
  by: (1) state file org_id (rename-safe), (2) name match, (3) create new.
- **Git connection matching**: Matches by GitHub org name. If no match found,
  repo sync is skipped (no fallback to wrong provider).
- **Role ID**: Always sent when assigning users to orgs (required by API).
- **Email filtering**: Emails filtered by `allowed_email_domains`.
- **Multi-org config**: Single `config.yaml` lists all orgs under `github_orgs`.

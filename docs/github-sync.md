# GitHub Team Sync

Automatically sync GitHub Team membership and repository access to [Devin](https://devin.ai) organizations via the Devin v3 API.

This module maps your GitHub Teams to Devin organizations so that when a developer is added to or removed from a GitHub team, their Devin org membership and repo access stay in sync — no manual configuration required.

## How it works

For each team mapping defined in your configuration:

1. **Member sync**: Fetches GitHub team members, resolves them to Devin enterprise users (by email/username), and adds or removes org membership in Devin to match.
2. **Repo sync**: Fetches GitHub team repositories and atomically replaces the Devin organization's git permissions with the matching set of `repo_path` entries.

The script is **idempotent** — running it multiple times produces the same result, making it safe for scheduled execution.

## Prerequisites

- **Python 3.10+**
- **GitHub Personal Access Token** with `read:org` scope (to list team members and repos)
- **Devin API service-user key** ([create one in Devin settings](https://app.devin.ai/settings/api-keys)) with the following permissions:
  - `ViewGitIntegrations`
  - `ManageGitIntegrations`
  - `ViewAccountMembership`
  - `ManageAccountMembership`
- A GitHub App or token-based git connection already configured in your Devin enterprise (needed for repo sync)

## Setup

```bash
git clone https://github.com/Devin-Samples/user-management.git
cd user-management

python -m venv .venv
source .venv/bin/activate

pip install -e .

cp .env.example .env
cp examples/config.yaml config.yaml
```

Edit `.env` with your tokens:

```
GITHUB_TOKEN=ghp_your_github_pat
DEVIN_API_KEY=sk-your_devin_service_user_key
DEVIN_API_BASE_URL=https://api.devin.ai
```

Verify everything is wired up:

```bash
user-management doctor
```

This checks Devin auth, GitHub token scopes, and that a GitHub git connection exists in your Devin enterprise.

## Configuration

Edit `config.yaml` to define your team to org mappings:

```yaml
# GitHub organization that owns the teams
github_org: "my-github-org"

# Mapping of GitHub teams to Devin organizations
team_mappings:
  - github_team_slug: "ecommerce-platform"
    devin_org_id: "org-abc123"
    sync_members: true
    sync_repos: true
  - github_team_slug: "analytics-platform"
    devin_org_id: "org-def456"
    sync_members: true
    sync_repos: true

# Optional: default role for synced members
default_member_role: "member"

# Optional: enable dry run mode globally
dry_run: false
```

### Configuration fields

| Field | Required | Description |
|---|---|---|
| `github_org` | Yes | The GitHub organization that owns the teams |
| `team_mappings` | Yes | List of team-to-org mapping entries |
| `team_mappings[].github_team_slug` | Yes | GitHub team slug (from the team URL) |
| `team_mappings[].devin_org_id` | Yes | Target Devin organization ID |
| `team_mappings[].sync_members` | No | Sync team members to org membership (default: `true`) |
| `team_mappings[].sync_repos` | No | Sync team repos to org git permissions (default: `true`) |
| `default_member_role` | No | Default role for synced members: `"member"` or `"admin"` (default: `"member"`) |
| `dry_run` | No | If `true`, log changes without applying them (default: `false`) |

For auto-discovery mode (no explicit `team_mappings`) and multi-org configs, see [`examples/config-multi-org.yaml`](../examples/config-multi-org.yaml).

### Finding your Devin org ID

You can find organization IDs in the Devin web UI under **Settings → Organizations**, the `bulk` module's pull command, or the API:

```bash
user-management bulk pull          # writes orgs-current.csv with org IDs you can copy

# or:
curl -s -H "Authorization: Bearer $DEVIN_API_KEY" \
  https://api.devin.ai/v3/enterprise/organizations | jq '.items[].org_id'
```

## Usage

```bash
# Standard sync
user-management github-sync run --config config.yaml

# Dry run (preview changes without applying)
user-management github-sync run --config config.yaml --dry-run

# Verbose logging
user-management github-sync run --config config.yaml --verbose

# Validate config + resolve every team slug and devin_org_id without syncing
user-management github-sync check --config config.yaml
```

### CLI options

| Flag | Description |
|---|---|
| `--config PATH` | Path to config YAML (default: `config.yaml`) |
| `--dry-run` | Preview changes without making API calls |
| `--verbose` | Enable debug-level logging |

## Scheduled execution

### Cron

Run every 15 minutes:

```cron
*/15 * * * * cd /path/to/user-management && /path/to/.venv/bin/user-management github-sync run --config config.yaml >> /var/log/devin-sync.log 2>&1
```

### GitHub Actions

Create `.github/workflows/sync.yml` in a repository:

```yaml
name: Sync GitHub Teams to Devin

on:
  schedule:
    # Run every 15 minutes
    - cron: "*/15 * * * *"
  workflow_dispatch: # Allow manual trigger

jobs:
  sync:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install
        run: pip install -e .

      - name: Run sync
        env:
          GITHUB_TOKEN: ${{ secrets.GH_TEAM_SYNC_TOKEN }}
          DEVIN_API_KEY: ${{ secrets.DEVIN_API_KEY }}
          DEVIN_API_BASE_URL: https://api.devin.ai
        run: user-management github-sync run --config config.yaml
```

> **Note**: Use a dedicated GitHub PAT stored as a repository secret (`GH_TEAM_SYNC_TOKEN`) rather than the default `GITHUB_TOKEN`, since the default token does not have `read:org` scope.

## How member matching works

The script maps GitHub users to Devin users by:

1. **Email match** (primary): Compares each GitHub team member's email with the Devin enterprise user's email.
2. **Username/name match** (fallback): Compares the GitHub login (lowercased) with the Devin user's `name` field.

Members that cannot be matched are logged and skipped. Run with `--verbose` to see which users were skipped and why.

For best results, ensure your team members' GitHub primary email matches the email they use for Devin.

## How repo sync works

The repo sync uses the **PUT** (replace) endpoint on the Devin v3 API, which atomically replaces all git permissions for an organization. This means:

- Repos added to the GitHub team will be added to Devin.
- Repos removed from the GitHub team will be removed from Devin.
- The operation is atomic — partial failures don't leave permissions in an inconsistent state.

The script automatically discovers the GitHub git connection (preferring `github_app` over `github_token`) from your Devin enterprise configuration.

## Security considerations

- **Token management**: Store tokens in environment variables or a `.env` file. Never commit tokens to version control.
- **Least privilege**: The GitHub PAT only needs `read:org` scope. The Devin service-user key should be scoped to the minimum required permissions.
- **Audit trail**: All changes made via the Devin API are recorded in the Devin audit log.
- **Dry run first**: Always test with `--dry-run` before running in production to verify expected behavior.
- **Network security**: All API calls use HTTPS. The script does not store or cache any credentials on disk beyond what's in the `.env` file.

## Troubleshooting

| Issue | Solution |
|---|---|
| `GITHUB_TOKEN environment variable is not set` | Ensure `.env` file exists and contains `GITHUB_TOKEN` |
| `No GitHub git connection found` | Configure a GitHub App or token connection in Devin enterprise settings, then re-run `user-management doctor` |
| Members skipped during sync | Run with `--verbose` to see unmatched users; verify email addresses match |
| `403` from Devin API | Verify the service-user key has the required permissions |
| `404` from GitHub API | Verify the team slug and org name are correct |

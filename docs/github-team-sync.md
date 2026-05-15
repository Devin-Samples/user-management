# GitHub Team → Devin Org Sync

## Overview

The `github-sync` module automatically creates and manages Devin organizations
based on GitHub team membership.  It uses the Devin v3 API and GitHub
REST/GraphQL APIs to keep team membership and repository access in sync.

---

## Key Design Rules

| Rule | Behavior |
|---|---|
| **GH team created** | Create a new Devin org named `{gh_org}-{gh_team_slug}` |
| **GH team deleted** | **Never delete the Devin org.** Log a warning for human review. |
| **GH team member added** | Add the corresponding Devin user to the Devin org |
| **GH team member removed** | Remove the corresponding Devin user from the Devin org |
| **GH team repo added/changed** | Update Devin org git permissions to match team repo access |
| **GH team repo removed** | Remove repo from Devin org git permissions |
| **Naming convention** | Devin org name = `{gh_org}-{gh_team_slug}` (configurable via `org_name_template`) |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Sync Orchestrator                         │
│              (github_sync/sync.py)                          │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  1. DISCOVER          2. RECONCILE         3. APPLY         │
│  ───────────          ────────────         ─────────        │
│  GH teams             Diff GH teams       Create orgs      │
│  GH team members      vs Devin orgs       Add/remove users │
│  GH team repos        Compute changes     Set permissions   │
│  Devin orgs                                Log deletions    │
│  Devin org members                                          │
│  Devin git perms                                            │
│                                                             │
├──────────────┬──────────────────────────┬───────────────────┤
│  GitHub API  │    User Resolution       │   Devin v3 API    │
│  Client      │    (email matching)      │   Client (core)   │
├──────────────┴──────────────────────────┴───────────────────┤
│  REST: teams, members, repos                                │
│  GraphQL: SAML externalIdentities                           │
│  Audit log: invite emails                                   │
└─────────────────────────────────────────────────────────────┘
```

---

## Detailed Process Flow

### Phase 1: Discovery (read-only)

#### 1a. Fetch GitHub state

```
GitHub REST API (using GITHUB_TOKEN with read:org scope):
├── GET /orgs/{gh_org}/teams?per_page=100
│   → List of all teams: [{slug, name, id, ...}, ...]
│
├── For each team:
│   ├── GET /orgs/{gh_org}/teams/{slug}/members?per_page=100
│   │   → [{login, id}, ...]
│   │
│   └── GET /orgs/{gh_org}/teams/{slug}/repos?per_page=100
│       → [{full_name, name, private, permissions}, ...]
│
└── Resolve member emails (layered approach):
    ├── PRIMARY: GraphQL samlIdentityProvider.externalIdentities
    │   → Maps login → SAML nameId
    │
    ├── FALLBACK 1: Audit log org.invite_member events
    │   → Maps login → invitee_email
    │
    ├── FALLBACK 2: Commit history in org repos
    │   → Maps login → git commit author email
    │
    └── FALLBACK 3: Public profile email
        → GET /users/{login} → email field (if public)
```

#### 1b. Fetch Devin state

```
Devin v3 API (via core.client.DevinAPIClient):
├── GET /v3/enterprise/organizations
│   → List all existing Devin orgs
│
├── GET /v3/enterprise/members/users
│   → All enterprise users with emails
│   → Build email→user and name→user lookup maps
│
├── GET /v3/enterprise/git-providers/connections
│   → Find the GitHub git connection matching the org
│
└── GET /v3/enterprise/roles
    → Resolve the org member role ID
```

### Phase 2: Reconciliation (compute diff, no API writes)

For each GitHub team, the sync tool:

1. Determines the desired Devin org name from the template
2. Checks if a matching org already exists (by state file → by name)
3. Resolves each team member to a Devin user via email matching
4. Computes member additions and removals
5. Computes repo permission changes

### Phase 3: Apply changes

All API calls are wrapped in try/except with retry logic. Failures are
logged and collected but don't stop the entire sync.

- **Create orgs**: `POST /v3/enterprise/organizations`
- **Add members**: `POST /v3/enterprise/organizations/{org_id}/members/users/{user_id}`
- **Remove members**: `DELETE /v3/enterprise/organizations/{org_id}/members/users/{user_id}`
- **Replace repo permissions**: `PUT /v3/enterprise/organizations/{org_id}/git-providers/permissions`
- **Stale orgs**: Logged as warnings, **never deleted**

---

## User Resolution Strategy

Mapping GitHub users to Devin users uses a layered approach:

```
┌─────────────────────────────────────────────────────┐
│              GitHub User (login)                     │
├─────────────────────────────────────────────────────┤
│                                                     │
│  Step 1: Resolve GitHub login → email               │
│  ┌──────────────────────────────────────────────┐   │
│  │ a) Public profile email (authoritative)      │   │
│  │ b) Audit log invitee_email                   │   │
│  │ c) SAML nameId (non-numeric preferred)       │   │
│  │ d) Username/name fallback                    │   │
│  │ e) Numeric SAML nameId (last resort)         │   │
│  └──────────────────────────────────────────────┘   │
│                                                     │
│  Step 2: Match email → Devin user                   │
│  ┌──────────────────────────────────────────────┐   │
│  │ a) Exact email match against enterprise users│   │
│  │ b) Alias resolution across sources           │   │
│  │ c) Username/name fallback (lowercased)       │   │
│  └──────────────────────────────────────────────┘   │
│                                                     │
│  Result: Matched → add to sync                      │
│          Unmatched → skip + log warning              │
└─────────────────────────────────────────────────────┘
```

Numeric SAML nameIds (e.g. `123456@example.com`) are employee IDs, not real
person emails.  They are deprioritized but kept as a last-resort fallback so
existing Devin users aren't silently dropped from org memberships.

---

## State File

The sync maintains a `sync-state.json` file alongside the config.  This maps
each GitHub org's teams to their Devin org IDs:

```json
{
  "my-github-org": {
    "team_org_map": {
      "backend-team": {
        "org_id": "org-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
        "cached_org_name": "my-github-org-backend-team"
      }
    }
  }
}
```

The state file provides **rename safety**: if someone renames a Devin org in
the UI, the state file's `org_id` still maps correctly, preventing duplicate
org creation.

---

## Configuration

### Example config

```yaml
default_member_role: "member"
dry_run: false

github_orgs:
  - github_org: "my-github-org"
    github_token_env_var: "GITHUB_TOKEN_MY_ORG"
    skip_enterprise_teams: true
    skip_empty_teams: true
    org_name_template: "{gh_org}-{team_slug}"
    auto_invite_members: true
    email_resolution:
      saml_graphql: true
      audit_log_invites: true
      commit_history: false
      public_profile: true
      allowed_email_domains:
        - "example.com"
```

See `src/user_management/github_sync/config.yaml.example` for all options.

### Environment variables

```bash
GITHUB_TOKEN_MY_ORG=ghp_xxxxx     # GH PAT with read:org (+ admin:org for SAML)
DEVIN_API_TOKEN=dv_xxxxx          # Devin service-user key
DEVIN_API_BASE_URL=https://api.devin.ai  # Optional; defaults to api.devin.ai
```

### Required Devin service user permissions

| Permission | Purpose |
|---|---|
| `ManageOrganizations` | Create new Devin orgs, list existing orgs |
| `ManageGitIntegrations` | List git connections, replace git permissions |
| `ManageAccountMembership` | Add/remove users from orgs, invite enterprise users |
| `ViewAccountMembership` | List enterprise users, list org members |
| `ViewGitIntegrations` | List existing git permissions |

### Required GitHub token scopes

| Scope | Purpose |
|---|---|
| `read:org` | List teams, team members, team repos |
| `admin:org` | SAML identity and audit log access (optional but recommended) |

---

## Running as a Devin Agent Skill

A ready-made agent skill is available at `.devin/skills/github-team-sync.md`.
It can be invoked by Devin to run the full sync workflow including secret
retrieval, config setup, sync execution, and reporting.

### Setup for Devin Environment

1. **Set org-level secrets** in Devin Settings → Secrets:
   - `DEVIN_ENTERPRISE_ADMIN_TOKEN` — Devin API service-user key
   - `GITHUB_TOKEN_<ORG>` — one per GitHub org

2. **Run via the agent skill** or manually:
   ```bash
   user-management github-sync --config config.yaml --verbose
   ```

---

## Module Layout

```
src/user_management/github_sync/
├── __init__.py          # Package docstring
├── cli.py               # Argparse subcommand for `github-sync`
├── config.py            # Environment variable helpers, YAML config loading
├── config.yaml.example  # Example configuration file
├── github_client.py     # Synchronous GitHub REST/GraphQL client (requests)
├── models.py            # Config, GitHub, and sync result pydantic models
└── sync.py              # Orchestration: discover, reconcile, apply
```

All Devin API calls go through `core.client.DevinAPIClient` (synchronous,
requests-based) following the repository convention.  The GitHub client uses
`requests` for its GraphQL and REST interactions.

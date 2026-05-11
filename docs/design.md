# Design: Automated GitHub Team → Devin Org Sync Process

## Overview

This document describes an enhanced sync process that **automatically creates and manages Devin organizations** based on GitHub team membership, using the Devin v3 API and GitHub REST/GraphQL APIs. It builds on the existing `devin-github-team-sync` codebase but removes the need for manual org creation and config-file mappings.

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
| **Naming convention** | Devin org name = `{gh_org}-{gh_team_slug}` (e.g. `Cognizant-insurance-admde-seg-adm`) |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Sync Orchestrator                         │
│                      (sync.py)                              │
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
│  Client      │    (email matching)      │   Client          │
├──────────────┴──────────────────────────┴───────────────────┤
│  REST: teams, members, repos                                │
│  GraphQL: SAML externalIdentities                           │
│  Audit log: invite emails, commit emails                    │
└─────────────────────────────────────────────────────────────┘
```

---

## Detailed Process Flow

### Phase 1: Discovery (read-only, all data fetched up front)

#### 1a. Fetch GitHub state

```
GitHub REST API (using GITHUB_TOKEN with read:org scope):
├── GET /orgs/{gh_org}/teams?per_page=100
│   → List of all teams: [{slug, name, id, ...}, ...]
│
├── For each team:
│   ├── GET /orgs/{gh_org}/teams/{slug}/members?per_page=100
│   │   → [{login, id}, ...]  (note: email NOT returned by this endpoint)
│   │
│   └── GET /orgs/{gh_org}/teams/{slug}/repos?per_page=100
│       → [{full_name, name, private, permissions}, ...]
│
└── Resolve member emails (layered approach):
    ├── PRIMARY: GraphQL samlIdentityProvider.externalIdentities
    │   → Maps login → SAML nameId (alias email like 120337@cognizant.com)
    │
    ├── FALLBACK 1: Audit log org.invite_member events
    │   → Maps login → invitee_email (real email like shiju.thomas@cognizant.com)
    │
    ├── FALLBACK 2: Commit history in org repos
    │   → Maps login → git commit author email
    │
    └── FALLBACK 3: Public profile email
        → GET /users/{login} → email field (if public)
```

#### 1b. Fetch Devin state

```
Devin v3 API (using DEVIN_API_TOKEN with ManageOrganizations + ManageGitIntegrations + ManageAccountMembership):
├── GET /v3/enterprise/organizations
│   → List all existing Devin orgs: [{org_id, name, ...}, ...]
│   → Filter to orgs matching pattern: {gh_org}-*
│
├── GET /v3/enterprise/members/users
│   → All enterprise users: [{user_id, email, name}, ...]
│   → Build email→user_id and name→user_id lookup maps
│
├── GET /v3/enterprise/git-providers/connections
│   → Find the GitHub git connection (prefer github_app > github_token)
│
└── For each existing Devin org matching our naming pattern:
    ├── GET /v3/enterprise/organizations/{org_id}/members/users
    │   → Current org members
    │
    └── GET /v3/enterprise/organizations/{org_id}/git-providers/permissions
        → Current repo permissions
```

### Phase 2: Reconciliation (compute the diff, no API writes)

```python
# Build the desired state from GitHub
desired_state = {}
for team in github_teams:
    org_name = f"{gh_org}-{team.slug}"
    desired_state[org_name] = {
        "team_slug": team.slug,
        "members": resolve_to_devin_user_ids(team.members),
        "repos": [repo.full_name for repo in team.repos],
    }

# Build the current state from Devin
current_state = {}
for org in devin_orgs:
    if org.name.startswith(f"{gh_org}-"):
        current_state[org.name] = {
            "org_id": org.org_id,
            "members": set(member.user_id for member in org.members),
            "repos": set(perm.repo_path for perm in org.permissions),
        }

# Compute changes
changes = {
    "orgs_to_create": [],      # In desired but not in current
    "orgs_stale": [],           # In current but not in desired (DO NOT DELETE)
    "members_to_add": {},       # {org_name: [user_ids]}
    "members_to_remove": {},    # {org_name: [user_ids]}
    "repos_to_update": {},      # {org_name: [repo_paths]}  (full replacement)
}

for org_name, desired in desired_state.items():
    if org_name not in current_state:
        changes["orgs_to_create"].append(org_name)
        changes["members_to_add"][org_name] = desired["members"]
        changes["repos_to_update"][org_name] = desired["repos"]
    else:
        current = current_state[org_name]
        # Member diff
        changes["members_to_add"][org_name] = desired["members"] - current["members"]
        changes["members_to_remove"][org_name] = current["members"] - desired["members"]
        # Repo diff (will use PUT to atomically replace)
        if desired["repos"] != current["repos"]:
            changes["repos_to_update"][org_name] = desired["repos"]

# Stale orgs: exist in Devin but no matching GH team
for org_name in current_state:
    if org_name not in desired_state:
        changes["orgs_stale"].append(org_name)
```

### Phase 3: Apply changes

All API calls are wrapped in try/except with retry logic. Failures are logged and collected but don't stop the entire sync.

#### 3a. Create new Devin orgs

```
For each org_name in orgs_to_create:
    POST /v3/enterprise/organizations
    Body: { "name": "{gh_org}-{gh_team_slug}" }
    → Returns: { "org_id": "org-xxx", "name": "...", ... }
    → Store org_id for subsequent member/repo operations
```

**Devin API endpoint:**
```
POST /v3/enterprise/organizations
Authorization: Bearer <service_user_token>
Content-Type: application/json

{
  "name": "Cognizant-insurance-admde-seg-adm",
  "max_cycle_acu_limit": null,
  "max_session_acu_limit": null
}

→ 200: { "org_id": "org-abc123", "name": "Cognizant-insurance-admde-seg-adm", ... }
```

#### 3b. Sync members

```
For each org_name, user_ids in members_to_add:
    For each user_id:
        POST /v3/enterprise/organizations/{org_id}/members/users/{user_id}
        Body: {}  (default role)

For each org_name, user_ids in members_to_remove:
    For each user_id:
        DELETE /v3/enterprise/organizations/{org_id}/members/users/{user_id}
```

#### 3c. Sync repo permissions

```
For each org_name, repos in repos_to_update:
    PUT /v3/enterprise/organizations/{org_id}/git-providers/permissions
    Body: {
      "permissions": [
        { "git_connection_id": "<github_connection_id>", "repo_path": "org/repo1" },
        { "git_connection_id": "<github_connection_id>", "repo_path": "org/repo2" },
        ...
      ]
    }
```

The PUT endpoint **atomically replaces** all permissions, so this is a full sync every run.

#### 3d. Log stale orgs (NEVER delete)

```
For each org_name in orgs_stale:
    LOG WARNING: "Devin org '{org_name}' exists but corresponding GitHub team
                  '{team_slug}' was not found. The org will NOT be deleted.
                  Manual review recommended."
```

---

## User Resolution Strategy

Mapping GitHub users to Devin users is the critical challenge. The process uses a layered approach:

```
┌─────────────────────────────────────────────────────┐
│              GitHub User (login)                     │
├─────────────────────────────────────────────────────┤
│                                                     │
│  Step 1: Resolve GitHub login → email               │
│  ┌──────────────────────────────────────────────┐   │
│  │ a) SAML nameId (GraphQL externalIdentities)  │   │
│  │    → alias email (e.g. 120337@cognizant.com) │   │
│  │ b) Audit log invitee_email                   │   │
│  │    → real email (e.g. shiju.thomas@cog.com)  │   │
│  │ c) Commit history email                      │   │
│  │ d) Public profile email                      │   │
│  └──────────────────────────────────────────────┘   │
│                                                     │
│  Step 2: Match email → Devin user_id                │
│  ┌──────────────────────────────────────────────┐   │
│  │ a) Exact email match against enterprise users│   │
│  │ b) Alias resolution: if SAML nameId doesn't  │   │
│  │    match, try commit/invite email too         │   │
│  │ c) Username/name fallback: GH login ↔ Devin  │   │
│  │    name (lowercased)                         │   │
│  └──────────────────────────────────────────────┘   │
│                                                     │
│  Result: Matched → add to sync                      │
│          Unmatched → skip + log warning              │
└─────────────────────────────────────────────────────┘
```

### Email resolution data structures

```python
@dataclass
class ResolvedGitHubUser:
    login: str
    emails: list[str]          # All discovered emails (ordered by priority)
    saml_name_id: str | None   # SAML alias email
    invite_email: str | None   # Email from audit log invite
    commit_email: str | None   # Email from git commits
    profile_email: str | None  # Public profile email
    devin_user_id: str | None  # Matched Devin user (None if unmatched)
```

---

## Configuration

### Simplified config (no manual org_id mapping needed)

```yaml
# config.yaml — simplified for auto-org-creation mode
github_org: "Cognizant-insurance-admde"

# Optional: specify which GH token env var (useful for multi-org)
github_token_env_var: "GITHUB_TOKEN_INS2ADMDE"

# Team filter (optional): only sync these teams. If omitted, sync ALL teams.
# team_filter:
#   - "seg-adm"
#   - "seg-de"

# Skip enterprise-managed teams (type: "enterprise") by default
skip_enterprise_teams: true

# Org naming pattern. {gh_org} and {team_slug} are substituted.
org_name_template: "{gh_org}-{team_slug}"

# Email resolution: enable/disable specific methods
email_resolution:
  saml_graphql: true        # Use GraphQL SAML externalIdentities
  audit_log_invites: true   # Use audit log org.invite_member events
  commit_history: true      # Scan org repo commits for author emails
  public_profile: true      # Check public GitHub profile email

# Safety
dry_run: false
```

### Environment variables

```bash
# .env
GITHUB_TOKEN_INS2ADMDE=ghp_xxxxx           # GH PAT with read:org + read:audit_log
DEVIN_API_TOKEN=cog_xxxxx                   # Devin service user key
DEVIN_API_BASE_URL=https://api.devin.ai     # Or custom enterprise URL
```

### Required Devin service user permissions

| Permission | Purpose |
|---|---|
| `ManageOrganizations` | Create new Devin orgs, list existing orgs |
| `ManageGitIntegrations` | List git connections, replace git permissions |
| `ManageAccountMembership` | Add/remove users from orgs |
| `ViewAccountMembership` | List enterprise users, list org members |
| `ViewGitIntegrations` | List existing git permissions |

### Required GitHub token scopes

| Scope | Purpose |
|---|---|
| `read:org` | List teams, team members, team repos |
| `read:audit_log` | Fetch invite events for email resolution |

---

## Changes to Existing Codebase

### New: `github_client.py` additions

```python
class GitHubClient:
    # Existing methods stay unchanged

    def list_org_teams(self, org: str) -> list[GitHubTeam]:
        """GET /orgs/{org}/teams — list ALL teams in the org."""

    def list_org_repos(self, org: str) -> list[GitHubRepo]:
        """GET /orgs/{org}/repos — for commit email scanning."""

    def get_saml_identities(self, org: str) -> dict[str, str]:
        """GraphQL query for SAML externalIdentities.
        Returns {login: saml_name_id}."""

    def get_audit_log_invite_emails(self, org: str) -> dict[str, str]:
        """GET /orgs/{org}/audit-log?phrase=action:org.invite_member
        Returns {gh_login: invitee_email}."""

    def get_commit_emails(self, org: str, login: str) -> str | None:
        """Scan org repo commits for author email matching login."""
```

### New: `devin_client.py` additions

```python
class DevinClient:
    # Existing methods stay unchanged

    async def create_organization(self, name: str, **kwargs) -> DevinOrg:
        """POST /v3/enterprise/organizations
        Creates a new Devin org and returns it."""

    async def list_organizations(self) -> list[DevinOrg]:
        """GET /v3/enterprise/organizations
        Lists all orgs in the enterprise."""
```

### New model: `DevinOrg`

```python
class DevinOrg(BaseModel):
    org_id: str
    name: str
    created_at: int | None = None
    max_cycle_acu_limit: int | None = None
    max_session_acu_limit: int | None = None
```

### Modified: `sync.py` (major refactor)

The main sync flow changes from "iterate over pre-configured mappings" to "discover teams → reconcile → apply":

```python
async def run_sync(config: AutoSyncConfig, dry_run: bool) -> SyncSummary:
    # Phase 1: Discovery
    gh_teams = github_client.list_org_teams(config.github_org)
    devin_orgs = await devin_client.list_organizations()
    enterprise_users = await devin_client.list_enterprise_users()
    git_connection = find_github_connection(await devin_client.list_git_connections())

    # Build email resolution map (all methods combined)
    email_resolver = EmailResolver(github_client, config)
    email_resolver.load_saml_identities(config.github_org)
    email_resolver.load_audit_log_emails(config.github_org)
    # commit + profile loaded on-demand per user

    # Phase 2: Reconciliation
    devin_org_by_name = {org.name: org for org in devin_orgs}
    changes = compute_changes(gh_teams, devin_org_by_name, config, email_resolver, enterprise_users)

    # Phase 3: Apply
    for org_name in changes.orgs_to_create:
        if dry_run:
            log(f"[DRY RUN] Would create Devin org: {org_name}")
        else:
            new_org = await devin_client.create_organization(name=org_name)
            devin_org_by_name[org_name] = new_org  # track for member/repo sync

    # ... member add/remove, repo permission replace (same as existing code) ...

    for stale_org in changes.orgs_stale:
        log(f"WARNING: Devin org '{stale_org}' has no matching GH team. NOT deleting.")
```

### Removed: manual `team_mappings` in config

The `devin_org_id` field is no longer needed since orgs are auto-created and discovered by naming convention.

---

## Handling Edge Cases

### 1. Enterprise-managed teams (e.g. `EnterpriseAppSecurityTeam`)

These teams have `type: "enterprise"` and are managed at the GitHub Enterprise level, not the org level. By default, skip these (configurable via `skip_enterprise_teams: true`). Their members often lack SSO linking and email visibility.

### 2. Org name collisions

If a Devin org named `{gh_org}-{team_slug}` already exists but wasn't created by this tool, the tool should detect it and either:
- Adopt it (if it has no members/permissions — treat as a fresh org)
- Warn and skip (if it has existing members that don't match the GH team)

### 3. Users with no discoverable email

Log a structured warning:
```
WARNING: Cannot resolve GitHub user 'ArtiKhade' to a Devin user.
  SAML identity: not linked
  Audit log invite: not found
  Commit email: no commits
  Profile email: not public
  Action: Skipped. User must complete SSO or make email public.
```

### 4. Team renamed in GitHub

The team slug changes → the sync will try to create a new org. The old org becomes "stale" and is logged. A future enhancement could detect renames via team ID tracking.

### 5. Rate limiting

- GitHub REST API: 5,000 requests/hour per PAT. The sync tool uses ~3-5 requests per team.
- GitHub GraphQL: 5,000 points/hour. The SAML query costs 1 point.
- Devin API: Rate limits TBD. Built-in retry with exponential backoff.

### 6. Max 200 permissions per PUT request

The Devin API `PUT .../git-providers/permissions` has a `max_length=200` on the permissions array. If a team has >200 repos, batch into multiple orgs or raise an error.

---

## Execution Modes

### One-shot sync (cron / GitHub Actions)

```bash
python sync.py --config config.yaml                    # Full sync
python sync.py --config config.yaml --dry-run          # Preview
python sync.py --config config.yaml --dry-run --verbose # Detailed preview
```

### Webhook-driven (future enhancement)

Listen for GitHub organization webhooks:
- `team.created` → create Devin org
- `team.deleted` → log warning (no delete)
- `membership.added` / `membership.removed` → add/remove Devin org member
- `team.added_to_repository` / `team.removed_from_repository` → update permissions

---

## Example: Full Sync Run for Cognizant-insurance-admde

### Input (GitHub state)

```
Teams:
  EnterpriseAppSecurityTeam (type: enterprise) → SKIPPED
  seg-adm (type: organization)
    Members: shijuthomas-1
    Repos: devin-seg-adm-test-repo1
  seg-de (type: organization)
    Members: shijuthomas-1
    Repos: devin-seg-de-test-repo1
```

### Step-by-step execution

```
[DISCOVER] Found 3 GitHub teams, skipping 1 enterprise team
[DISCOVER] Processing 2 org teams: seg-adm, seg-de

[DISCOVER] Resolving emails for 1 unique member(s):
  shijuthomas-1:
    SAML nameId: 120337@cognizant.com
    Audit invite: shiju.thomas@cognizant.com
    Commit email: shiju.thomas@cognizant.com

[DISCOVER] Found 0 existing Devin orgs matching pattern 'Cognizant-insurance-admde-*'
[DISCOVER] Found 5 enterprise users in Devin
[DISCOVER] Matched shijuthomas-1 → user-abc123 (via email shiju.thomas@cognizant.com)

[RECONCILE] Orgs to create: 2
  - Cognizant-insurance-admde-seg-adm
  - Cognizant-insurance-admde-seg-de
[RECONCILE] Members to add: shijuthomas-1 → both orgs
[RECONCILE] Repos to sync: 1 repo per org

[APPLY] Creating Devin org: Cognizant-insurance-admde-seg-adm → org-xxx1
[APPLY] Creating Devin org: Cognizant-insurance-admde-seg-de → org-xxx2
[APPLY] Adding user-abc123 to org-xxx1
[APPLY] Adding user-abc123 to org-xxx2
[APPLY] Setting git permissions for org-xxx1: [devin-seg-adm-test-repo1]
[APPLY] Setting git permissions for org-xxx2: [devin-seg-de-test-repo1]

============================================================
  SYNC SUMMARY
============================================================
  Orgs created: 2
  Members added: 2 (across 2 orgs)
  Members removed: 0
  Repos synced: 2 (1 per org)
  Stale orgs: 0
  Unmatched users: 0
  Errors: 0
============================================================
```

---

## API Call Sequence (per sync run)

```
# Phase 1: Discovery
GitHub:
  1. GET /orgs/{org}/teams                              ← list teams
  2. GET /orgs/{org}/teams/{slug}/members (× N teams)   ← team members
  3. GET /orgs/{org}/teams/{slug}/repos   (× N teams)   ← team repos
  4. POST /graphql (SAML externalIdentities)            ← alias emails
  5. GET /orgs/{org}/audit-log?phrase=org.invite_member  ← invite emails
  6. GET /repos/{org}/{repo}/commits?author={login}     ← commit emails (on-demand)

Devin:
  7. GET /v3/enterprise/organizations                   ← all Devin orgs
  8. GET /v3/enterprise/members/users                   ← all enterprise users
  9. GET /v3/enterprise/git-providers/connections        ← git connections
 10. GET /v3/enterprise/organizations/{id}/members/users (× existing orgs)
 11. GET /v3/enterprise/organizations/{id}/git-providers/permissions (× existing orgs)

# Phase 3: Apply
Devin:
 12. POST /v3/enterprise/organizations                  ← create new orgs
 13. POST /v3/enterprise/organizations/{id}/members/users/{uid}  ← add members
 14. DELETE /v3/enterprise/organizations/{id}/members/users/{uid} ← remove members
 15. PUT /v3/enterprise/organizations/{id}/git-providers/permissions ← set repos
```

---

## Summary of Changes vs. Existing Codebase

| Aspect | Before (existing) | After (proposed) |
|---|---|---|
| Org creation | Manual (pre-create orgs, put IDs in config) | Automatic via `POST /v3/enterprise/organizations` |
| Team discovery | Manual (list team slugs in config) | Automatic via `GET /orgs/{org}/teams` |
| Org naming | User-defined org IDs | Convention: `{gh_org}-{team_slug}` |
| Email resolution | Basic (public email + username match) | Layered (SAML + audit log + commits + profile) |
| Org deletion | N/A | Explicitly forbidden — log warning only |
| Config complexity | Must know org IDs + team slugs | Just the GitHub org name |
| Enterprise teams | No special handling | Auto-skipped (configurable) |

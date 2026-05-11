# Setting Up Secrets for GitHub Team Sync

## GitHub Token Setup

### Step 1: Create Fine-Grained Personal Access Token

1. Go to: https://github.com/settings/personal-access-tokens/new
2. Configure the token:
   - **Token name**: `devin-team-sync`
   - **Expiration**: 90 days (recommended)
   - **Resource owner**: Select your GitHub organization
   - **Repository access**: All repositories

3. Grant these **Organization permissions**:
   - `Members`: **Read-only** ✅
   - `Administration`: **Read-only** ✅

4. Grant these **Repository permissions**:
   - `Metadata`: **Read-only** (auto-granted)
   - `Contents`: **Read-only** ✅

5. Click **Generate token** and copy it immediately

### Step 2: Store Secrets in Devin Organization Workspace

#### Option A: Using Devin Workspace Environment Variables

1. Open your Devin organization workspace settings
2. Navigate to **Environment Variables** or **Secrets**
3. Add these secrets:

**For single GitHub org:**
   ```
   GITHUB_TOKEN=github_pat_11AAAA...
   DEVIN_API_TOKEN=sk-...
   DEVIN_API_BASE_URL=https://api.devin.ai
   ```

**For multiple GitHub orgs (recommended naming):**
   ```
   GITHUB_TOKEN_ACME=github_pat_11AAAA...
   GITHUB_TOKEN_CONTOSO=github_pat_11BBBB...
   GITHUB_TOKEN_FABRIKAM=github_pat_11CCCC...
   DEVIN_API_TOKEN=sk-...
   DEVIN_API_BASE_URL=https://api.devin.ai
   ```

Then in your `config.yaml`, specify which token to use:
   ```yaml
   github_org: "acme-corp"
   github_token_env_var: "GITHUB_TOKEN_ACME"
   ```

#### Option B: Using .env File (Local Development)

For local testing, create a `.env` file:

```bash
cp .env.example .env
```

Edit `.env`:
```env
GITHUB_TOKEN=github_pat_11AAAA...
DEVIN_API_TOKEN=sk-...
DEVIN_API_BASE_URL=https://api.devin.ai
```

**⚠️ NEVER commit `.env` to git** (already in `.gitignore`)

### Step 3: Verify Token Permissions

Run the verification script:

```bash
python check_github_app.py
```

### Step 4: Test the Sync

Run a dry-run to verify everything works:

```bash
python sync.py --config config.yaml --dry-run --verbose
```

## Token Rotation

Fine-grained PATs expire. Set a calendar reminder to regenerate before expiration:

1. Generate new token with same permissions
2. Update the secret in Devin workspace
3. Verify with `--dry-run` before running production sync

## Security Best Practices

✅ **DO:**
- Use fine-grained PATs over classic PATs
- Set expiration dates (90 days recommended)
- Scope to specific organizations only
- Store in Devin workspace secrets (not in code)
- Rotate tokens regularly

❌ **DON'T:**
- Use classic PATs with `read:org` (too broad)
- Set "no expiration"
- Commit tokens to version control
- Share tokens across multiple tools
- Grant write permissions unless absolutely necessary

## Troubleshooting

### "Resource not accessible by personal access token"
- Verify the token has `Members: Read` and `Administration: Read` permissions
- Ensure the token is scoped to the correct organization
- Check that the token hasn't expired

### "Bad credentials"
- Token may be expired or revoked
- Verify `GITHUB_TOKEN` is set correctly in environment
- Ensure no extra whitespace in the token value

### "Not Found" errors
- Verify the GitHub organization name in `config.yaml` matches exactly
- Ensure team slugs are correct (use lowercase with hyphens)
- Check that the token's resource owner matches the org

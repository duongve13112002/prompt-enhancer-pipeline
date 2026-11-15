# N134 GitHub repository visibility changes and Actions billing

- **Source:** the docs.github.com pages and the local `gh` 2.100.0 help, listed per claim below
- **Class:** documented, except the claims marked *unverified*
- **Verified by:** T-022 implementer (Claude Opus 5.5) on 2026-11-15:
  - each quote below was found verbatim, by an exact-string search of the fetched page with its HTML tags stripped (R-021);
  - the `gh` quotes come from `gh <command> --help` of the installed version.
- **Checked:** 2026-11-15
- **Used in:** docs/ci.md, tools/agent-workflow/ci_window.py (task T-022)

## Changing visibility

https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/managing-repository-settings/setting-repository-visibility

- Private to public:
  - "The code will be visible to everyone who can visit GitHub.com."
  - "Actions history and logs will be visible to everyone."
  - "All push rulesets will be disabled."
- Public to private:
  - "Stars and watchers for this repository will be erased, which will affect repository rankings."
  - "Current forks will remain public and will be detached from this repository."
  - "Any published GitHub Pages site will be automatically unpublished."

`gh repo edit --help` (gh 2.100.0):
- "When the `--visibility` flag is used, `--accept-visibility-change-consequences` flag is required."
- "--visibility string  Change the visibility of the repository to {public,private,internal}".

REST, https://docs.github.com/en/rest/repos/repos (Update a repository): `private`: "Either true to make the repository private or false to make it public."

## Actions billing

https://docs.github.com/en/billing/concepts/product-billing/github-actions:
- "The use of standard GitHub-hosted runners is free" … "In public repositories";
- "For private repositories, usage is measured against two parts of your plan";
- "jobs that run on Windows and macOS runners hosted by GitHub cost more to run than jobs on Linux runners".

## Dispatch and tying a run to a commit

https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows:
- "This event will only trigger a workflow run if the workflow file exists on the default branch."
- "Once a workflow has run at least once, you can dispatch it against any branch or tag via the GitHub API or GitHub CLI."
- `GITHUB_SHA` for `workflow_dispatch`: "Last commit on the GITHUB_REF branch or tag".

https://docs.github.com/en/rest/actions/workflows (Create a workflow dispatch event):
- `ref`: "The reference can be a branch or tag name.";
- the 200 response: "Response including the workflow run ID and URLs";
- "OAuth tokens and personal access tokens (classic) need the repo scope to use this endpoint."

`gh` 2.100.0 help:
- `gh workflow run`: "The created workflow run URL will be returned if available."
- `gh run list`: "-c, --commit SHA  Filter runs by the SHA of the commit".
- `gh run view` JSON fields include `conclusion`, `databaseId`, `event`, `headSha`, `status`.
- `gh help exit-codes`: 0 success, 1 failure, 2 cancelled, 4 authentication required.

## Tokens

- Our `gh` login (`gh auth status`, 2026-11-15) has the scopes 'gist', 'read:org', 'repo' and 'workflow'.
- Can a workflow change its own visibility? https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax lists the `GITHUB_TOKEN` permission keys. The list ends with "vulnerability-alerts". The page never contains the word "administration" (0 matches), so a workflow's own token cannot change visibility. This conclusion is drawn from the absence of the word on the page. The return to private therefore runs in the local tool.
- *Unverified:*
  - The permissions page (https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens) has a "Repository permissions for "Administration"" section. I could not tie the `PATCH /repos/{owner}/{repo}` `write` row to that section in the fetched text.
  - That the classic `repo` scope is enough to change visibility. The first window shows it.

## Secret scanner

- gitleaks is approved by the user, with its download into the project (decision 113).
- 8.30.1 is installed for Windows x64 by the PM at `.tmp/tools/gitleaks/8.30.1/bin/gitleaks.exe`:
  - the zip matched the release's checksums file;
  - SHA-256 `17157e2ee8b76fc8b1d8bee607a250e34b8a8023c8bc81822d4b5ee4d78fcb7c`, re-computed by T-022 with `sha256sum`;
  - `version` prints `8.30.1`.
- `gitleaks --help` (8.30.1):
  - "--exit-code int  exit code when leaks have been encountered (default 1)";
  - "--redact uint[=100]  redact secrets from logs and stdout";
  - "-r, --report-path string  report file";
  - "-i, --gitleaks-ignore-path string  path to .gitleaksignore file or folder containing one (default ".")";
  - "--ignore-gitleaks-allow  ignore gitleaks:allow comments";
  - "-b, --baseline-path string  path to baseline with issues that can be ignored". README v8.30.1 (https://raw.githubusercontent.com/gitleaks/gitleaks/v8.30.1/README.md): "A baseline can be any gitleaks report." It does not say which fields are compared. *Experiment* (T-022 round 4): entries holding only the fingerprint fields did not suppress the matching findings;
  - the config order of precedence: "1. --config/-c", "2. env var GITLEAKS_CONFIG", "3. env var GITLEAKS_CONFIG_TOML", "4. (target path)/.gitleaks.toml".

## Pinning

https://docs.github.com/en/actions/reference/security/secure-use:
- "Pin references to actions to a commit SHA";
- "When selecting a SHA, you should verify it is from the action's repository and not a repository fork."
- The pins in `.github/workflows/ci.yml` follow this. On 2026-11-15 at 16:12 the PM read each release tag from the official repository with `gh api repos/actions/<name>/git/ref/tags/<tag>`:
  - checkout v7.0.1 is `3d3c42e5aac5ba805825da76410c181273ba90b1`;
  - setup-python v7.1.0 is `6d4fd22b20465ccc0f7354476588e8df7c9ca0e0`.

# N134 GitHub repository visibility changes and Actions billing

- **Source:** docs.github.com (pages not opened; addresses not recorded because they would be from memory)
- **Class:** assumption (documented, source-verified, experiment or assumption)
- **Verified by:** None: written from the PM's memory on 2026-11-11
- **Checked:** 2026-11-11
- **Used in:** docs/ci.md, task T-022

## What this project relies on

To verify before implementing: that GitHub Actions minutes are free for public repositories and metered for private ones; the exact `gh` command and confirmation flag for changing visibility; what a repository loses when it goes from public to private (stars, watchers, public forks); which token can change visibility and whether a workflow's own token can.

## Caveats

Nothing here is verified. Task T-022 re-reads the primary documentation and upgrades or corrects this note before any code is written.

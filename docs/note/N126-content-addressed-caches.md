# N126 Content-addressed caches for derived artifacts

- **Source:** https://doc.dvc.org/user-guide/project-structure/internal-files ; https://bazel.build/remote/caching
- **Class:** documented (documented, source-verified, experiment or assumption)
- **Verified by:** Research agent read the primary page on 2026-11-11; the PM did not re-read it
- **Checked:** 2026-11-11
- **Used in:** K0, docs/execution.md

## What this project relies on

One tool caches by file content hash and keys a run by dependency contents plus the command; another keys an action by its inputs, command line and listed environment and stores outputs by content hash.

## Caveats

Supports keying artifacts by input hash plus tool revisions; the rename rule (same hash, new path, no recompute) is this project's inference.

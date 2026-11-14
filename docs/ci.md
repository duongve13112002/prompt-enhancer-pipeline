# Continuous integration

Design for the task that will implement it; nothing exists yet. Every GitHub behaviour below is class assumption and carries "to verify (N134)": the implementing task re-reads the primary documentation before relying on it ([N134](note/N134-github-visibility-and-actions.md)).

**Repository and credentials (decision 35).** The user creates the GitHub repository, clones it and sets up `gh` before agents start; agents work inside that clone and never create a repository, choose a remote or log in themselves. In the delivered zip no remote is configured, so nothing below can run until the user has done that.

## Requirement

Continuous integration runs on GitHub Actions with the latest Ubuntu and Windows runners. For a run the repository is made public, and it is made private again when the run ends, whether green or red (R-025, decision 26).

## Workflow

- GitHub Actions, a matrix of `ubuntu-latest` and `windows-latest`.
- It runs the bounded CPU test suite and the tooling checks ([test-plan](test-plan.md)); no GPU, no model downloads, no secrets needed by the tests.
- Triggered by manual dispatch only **[P]**: the orchestrator pushes (decision 77), and a push must not start a run while the repository is still private.

## Visibility window procedure

The orchestrator makes the commits and pushes (decision 77). It is also the only agent that changes visibility, and it opens the window only when the user asks for a run on a pushed commit. Steps:

1. Refuse to start unless the working tree is clean, the local head equals the remote head of the branch (the commit is pushed) and a pre-publication check passes: no file under `.secrets/`, `models/`, `data/`, `runs/` or the user's knowledge base is tracked, and a secret scan over the commits to be exposed finds nothing.
2. Record the current visibility.
3. Switch the repository to public.
4. Dispatch the workflow on that commit and wait for the run to finish, with a time limit.
5. Switch the repository back to private in a step that always runs, whether the run is green, red, cancelled or timed out; then verify the visibility.
6. Report the run result.

A red run is fixed while the repository is private; the orchestrator commits and pushes the fix, and the window is then repeated.

## Risks the user accepted and how the procedure limits them

| Risk | Limit |
| --- | --- |
| Everything in the history is world-readable while public and may be copied | The pre-publication check and secret scan in step 1; the window lasts only as long as the run |
| Stars, watchers and public forks are affected when a repository goes private again (to verify, N134) | Accepted by the user; the implementing task documents the observed effect |
| A crash between steps 3 and 5 would leave the repository public | The always-run step 5, the time limit in step 4, and a check at every session start that the repository is private |

## Research required before implementation

All of these are class assumption (N134) and must be re-read from the primary documentation by the implementing task:

- the exact `gh` commands and flags that change visibility, and the confirmation flag they require;
- the documented consequences of changing visibility;
- Actions billing for public and for private repositories;
- which token scopes are needed;
- whether a workflow can change its own repository's visibility.

## Not decided

Whether Windows runs in every window or only at feature completion and before release. The user's words are that a run supports both (decision 26); the implementing task proposes one with the measured run time.

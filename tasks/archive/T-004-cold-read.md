# T-004 Cold read of the documentation, round 1 (2026-11-11)

Kind: scout
R-IDs: R-028
Task file: tasks/archive/T-004-cold-read.md

## What was done

Three fresh read-only readers, one each on Haiku 5.5, Sonnet 5.5 and Opus 5.5, were given only the repository and one brief: read as a new agent would at session start, describe the project, state the first steps, answer twelve comprehension questions with a file pointer for each answer, list everything unclear or contradictory with file and line, and list the questions they would ask the user. None changed a file.

## Result

- **Understanding:** all three described the project correctly and answered all twelve questions in line with the requirements and decisions (multi-concept folders, family choice by code, retrieval off, generate-only profile, sentinels, no word lists, delivery of knowledge images, the code gate, repository visibility, where model names live, evidence pointers, deletion and snapshots).
- **Defects of the text found by two or three readers** (fixed in the same session): the scope matrix, verifier gating and fallbacks in the example configuration disagreed with stages S3 and S5; the deployment overlay set a model although the contract said an overlay could not; the planner and verifier image settings in the configuration disagreed with S2 and S5; a dead link to this file; a "needs user go: no" column against the Step 0 rule; a scout pack that forbade the script task T-002 asks for; decision numbers of the earlier project in copied policy text.
- **Found by one reader and fixed:** example folder names containing the vertical bar, which Windows file systems reject (Haiku); the stage order of card summary, aliases and concept-text embeddings (Haiku); alias-collision policy in the configuration against K4 (Haiku, Opus); the data split named `dev` confused with the dev machine (Haiku); role fields missing from the configuration against the request contract (Opus); one retrieval state used with two meanings (Opus); a sentinel inside a URL string (Haiku, Opus); the `[META]` block of the example system prompt not explained (Haiku).
- **Left as they are, on purpose:** the opening prompt lives outside the repository; no task files exist beyond T-002 because briefs are written when a task is assigned; no remote exists, so the visibility rule applies once one is created.

## Questions relayed to the user (with the PM's proposal)

1. Is the dev machine Windows, and do real knowledge-base folder names contain the separator? The vertical bar is not legal in a Windows file name. Proposal: several concepts are declared through the info file; the folder-name separator is its own configuration value.
2. Do folders that share a stem before " - " (the uniform example) form one family? Proposal: yes, with the separator taken from the real knowledge base.
3. Is the info file really named `infor.txt`? Proposal: accept `infor.txt` and `info.txt`, both configurable.
4. Do all admin system prompt files have the `[META]`, `[SYSTEM]`, `[USER]` sections of the example? Proposal: support that sectioned form and a plain file.
5. Targets for linking coverage (E1), latency (E13) and card precision (E16): set them after the first measurements? Proposal: yes; sentinels stay until then.
6. First commit: commit locally now with no remote? Proposal: yes, on the user's word.
7. CI: both runners on every run of the visibility window, or Windows only at feature completion and before a release? Who creates the GitHub repository? Proposal: both runners every run; the user creates the private repository and names it.

## Not done

The readers did not read `tools/`, most research notes or the schema files. No reader was a Codex or Antigravity model; a round with those CLIs is `not_run`.

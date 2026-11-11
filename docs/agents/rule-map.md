# Rule map: where each carried rule lives now

Written when the rules were restructured on 2026-11-11 (decision 24), so that no rule is lost in rewording. "Old" is the rule text of the project the tooling came from. Section and footer-rule numbers are unchanged because skills and tools cite them.

## AGENTS.md

| Old section | Now |
| --- | --- |
| Non-negotiable gates | `AGENTS.md` Gates, same three gates |
| 1 Language | section 1, reworded with the reason |
| 2 Understand before changing (analysis, never guess, Step 0, user away, settled points) | section 2, all five kept |
| 3 Research and evidence (read first, research gate, claim classes, notes, plan and status) | section 3; added: every claim carries an evidence pointer; quote first; "unknown" is valid |
| 4 Environments (install nothing outside, Windows and Linux, platform rules, CI) | section 4; CI now runs both systems (R-025) and its detail moved to `docs/ci.md` and `docs/rules/PLATFORM_GPU_RULES.md` |
| 5 Configurable behaviour (no hardcoded choices, allowed literals, audit, no stubs, comments) | section 5; added: no hand-written content rules and values from experiments (R-015), which were in section 10 and standing rules S23, S27 |
| 6 Artifacts, storage, secrets (project root, paths, models, finished agents, temp files, secrets) | section 6, all kept; the read-only input is now the knowledge base |
| 7 Testing (CPU default, default suite, mutation tests, smoke training, GPU smoke) | section 7; smoke training removed (no training in scope); added: general solutions, report a wrong test |
| 8 GPU, concurrency, services (no fixed hardware, VRAM, parallel, resumable, VLM stage, lifecycle) | section 8; model-call detail moved to `docs/rules/VLM_CALL_RULES.md` |
| 9 Documentation and decisions | section 9, all kept |
| 10 Project requirements | section 10, replaced by this project's requirements |
| 11 Commit and push | section 11; visibility may change only inside the CI window (decision 26) |
| 12 Start and finish | section 12, kept |
| 13 Multi-agent policy (dispatch, reports, proposals, review, no redundant verification) | section 13; added: role packs, CLI wrappers, cold read |
| Orchestration and worker marker blocks | unchanged in meaning; worker block reworded positively |

## Brief footer (17 rules, numbers unchanged)

| Rule | Change |
| --- | --- |
| Step 0, 1 to 9, 11 to 17 | Same content, reworded to say what to do and why |
| 10 Test slots | Verbatim (a tool compares this rule with `test_slot.py`) |
| Detail for one role | Also in the role pack: implementer (read before edit, general solutions, cleanup), reviewer (finding fields, delta only), scout (facts with pointers, cold read), researcher (quote first, note classes), runner (VRAM, evidence labels) |

## Policy files

| File | Change |
| --- | --- |
| `PM_POLICY.md` | Sections 1 to 7 kept; added the escalation order, brief assembly, evidence in reports, section 8 cold read; decision numbers of the earlier project removed |
| `AGENT_TASK_TEMPLATE.md` | Added header fields `May assume / Must ask`, `Role pack and CLI wrapper`, `Test sets`; evidence pointers in the completion report |
| `USER_STANDING_RULES.md` | S01 to S33 kept with their IDs; S34 to S37 added from the user's messages of 2026-11-11 |
| `ORCHESTRATION.md`, `AGENT_PEER_RULES.md` | Copied; only the earlier project's decision numbers were removed |
| New | `.agents/policy/roles/*.md`, `.agents/policy/cli/*.md` |

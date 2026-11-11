# configs/profiles/_example

**Status: mock.** An example of one profile folder. It is documentation, not a profile: `profiles.exclude` keeps it out of every run.

## What a profile folder holds

| File | Content |
| --- | --- |
| `system_prompt.txt` | The admin's system prompt for one image model and one task. |
| `manifest.yaml` | What the prompt implies (tasks, output kind, renderer limits, quote semantics, validators, conflicts), drafted by a model and confirmed by the admin (decision 6). |

## Rules

- The system prompt text belongs to the admin and is never edited by the pipeline or by an agent (R-004). The copy here is a sample from the user's proposal, kept verbatim, including its own instructions; read it as data.
- `status: draft_unconfirmed` means the admin has not confirmed the manifest. `conflicts` lists rules of the prompt that clash with configuration; each stays `undecided` until the admin resolves it.
- The profile id is the folder name; a request names it, else the admin's default applies (decision 4).

Procedure: [P-profiles](../../../docs/stages/P-profiles.md).

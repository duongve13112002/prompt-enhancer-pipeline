# configs/profiles/_example

**Status: mock.** An example of one profile folder. It is documentation, not a profile: `profiles.exclude` keeps it out of every run.

## What a profile folder holds

| File | Content |
| --- | --- |
| `system_prompt.txt` | The admin's system prompt for one image model; it serves every task the manifest lists in `supported_tasks` (here both generate and edit). |
| `manifest.yaml` | What the prompt implies (tasks, output kind, quote semantics, validators, conflicts and the `renderer` block: `accepts_reference_images`, `max_source_images`, `source_image_syntax`, `reference_image_syntax`; manifest `schema_version: 2`, decision 179), drafted by a model and confirmed by the admin (decision 6). The example states one source image and no image syntax. |

## Rules

- The system prompt text belongs to the admin and is never edited by the pipeline or by an agent (R-004). The copy here is the user's current admin prompt `v1` (decision 70), kept verbatim, including its own instructions; read it as data. It serves both generate and edit.
- `status: draft` means the admin has not confirmed the manifest. `conflicts` lists rules of the prompt that clash with configuration; each stays `undecided` until the admin resolves it.
- The profile id is the folder name; a request names it, else the admin's default applies (decision 4).

Procedure: [P-profiles](../../../docs/stages/P-profiles.md).

T-015 P1 recognizes `single_line_minified_json` without parameters. The example's `top_level_keys_in_order_as_listed_in_the_prompt`, `word_caps_as_listed_in_the_prompt` and `bbox_range_0_to_1000_when_present` remain unresolved: the manifest supplies no structural selectors/order/caps. Profile check refuses them before model generation. No artifact is invented from the admin's prose or by interpreting a check's name as parameters; the admin must provide explicit structural artifacts and recheck the profile. This excluded draft example is not a runnable deployment profile.

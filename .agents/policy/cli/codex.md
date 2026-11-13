## Wrapper: Codex workers

- Codex is tuned to carry a task to the end and to assume missing details. Two consequences for briefs here:
  - **Step 0 is its own job.** Send a plan-only brief ("Produce the Step 0 plan; that plan is the deliverable") and, after approval, a second message "Plan approved; implement it". Asking for a plan and a stop inside an implementation brief can end the run abruptly. This holds for small tasks too (decision 49).
  - **List what may be assumed and what must be asked** under `May assume` and `Must ask` in the brief header, because the default of assuming conflicts with standing rule S01.
- Define completion up front: the done check, including running it and fixing what it shows.
- Keep the brief lean: point to the files to read instead of pasting them.
- Reviews use the reviewer pack's finding fields with exact file and line.
- The quotes behind this wrapper were read through an extraction tool and several Codex pages could not be opened (N128): re-read the primary pages before changing this file.
- **Image role `codex-image` (decision 47, N136):** the built-in image tool (`gpt-image-2`) renders enhanced prompts to test the flow; it spends the Codex subscription several times faster than a normal turn. The brief lists exactly which prompts to render or which images to edit and how many; generate each once, reuse an image whose prompt is unchanged (record the prompt hash next to it), never render variations nobody asked for, and report the number of images made. Edit tests use images Codex generated earlier, not the user's knowledge-base images, unless the user allows it.

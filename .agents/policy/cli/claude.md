## Wrapper: Claude Code workers

- State the action mode in the first line of the brief: "Implement the change" for implementers, "Research and report; change no files" for read-only roles. Claude follows the stated mode precisely.
- Put the task in tags: `<goal>`, `<done_check>`, `<scope>`, `<out_of_scope>`, `<output>`; keep the header lines `Kind:`, `R-IDs:`, `Task file:` as plain lines because the lint reads them.
- Give the reason with each constraint; write what to do rather than what to avoid; use plain wording without capitals for emphasis.
- For reviewers and scouts add: "Open and read the relevant files before answering; describe only code you have opened."
- Set depth with the effort level, not with step-by-step reasoning instructions.

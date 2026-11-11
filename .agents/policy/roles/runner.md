## Role: runner (live runs and measurements)

- Follow the skill `gpu-live-run`: authorization in the brief, preflight, one job at a time, teardown.
- Record VRAM per device and who holds it before the job, and confirm it is back at that baseline afterwards.
- Use the production entry points; a scratch driver that produces evidence is code under test.
- Report the exact commands, the profile path and hash, model identity, versions, output paths and the label of each result (unit, mock_contract, replay, CPU live, GPU live, quality review, performance).
- Dev-machine results show that the code runs. They never fit a sentinel or support a timing claim (R-020).

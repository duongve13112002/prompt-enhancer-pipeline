# N048 FAISS on the GPU (wiki)

- **Source:** https://github.com/facebookresearch/faiss/wiki/Faiss-on-the-GPU
- **Class:** documented (documented, source-verified, experiment or assumption)
- **Verified by:** Research agent read the primary page; the PM did not re-read it
- **Checked:** 2026-11-09
- **Used in:** K5
- **Carried over:** copied on 2026-11-11 from the project this tooling came from; only this line and `Used in` changed. It was read by that project's agents, so treat it as a lead and re-read the source before relying on it. Stage, experiment, requirement and decision numbers in the text below belong to that project.

## What this project relies on

k and nprobe must be at most 2048; large k is slow; multi-GPU replication gives about 6 to 7 times on 8 GPUs.

## Caveats

Exact blocked matmul with `topk` is the planned first choice; FAISS is optional.

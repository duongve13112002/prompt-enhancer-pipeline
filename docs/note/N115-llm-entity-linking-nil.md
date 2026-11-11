# N115 Entity linking with a model choosing from a shortlist and an explicit no-match

- **Source:** https://arxiv.org/abs/2601.08500 ; https://www.ijcai.org/proceedings/2026/0966.pdf
- **Class:** documented (documented, source-verified, experiment or assumption)
- **Verified by:** Research agent read the primary page on 2026-11-11; the PM did not re-read it (abstract; second source from a snippet)
- **Checked:** 2026-11-11
- **Used in:** S2

## What this project relies on

One system retrieves candidates with a multilingual bi-encoder and uses an instruction-tuned model for no-match prediction and selection, invoked on hard cases; another gives the model the top candidates and an explicit "none of the candidates" option.

## Caveats

Agent-read. No paper settles shortlist-plus-model against a cross-encoder for a small curated knowledge base (E3).

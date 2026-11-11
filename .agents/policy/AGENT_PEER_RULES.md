# Agent peer rules (shared by agents on one machine)

The official peer rules; the session-start hook `standing_rules.py` injects this file whole (standing rule S07, [ORCHESTRATION](../ORCHESTRATION.md) section 13):

- GPU peers: other agent sessions share this machine's GPU. Check `nvidia-smi` first, post a start message and a free message, never kill another session's job.
- A busy GPU means wait and recheck every 30 minutes; never fall back to CPU for model work.
- Stop only processes you started, by recorded PID.

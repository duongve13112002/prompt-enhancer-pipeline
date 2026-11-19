"""Resolved configuration for the approved-profile gate of the agent tooling (tools/agent-workflow/run_approval_gate.py).

The gate records what the user approved and refuses a live run whose resolved configuration differs. This project has
no hidden defaults (R-015: a missing field is refused), so the resolved configuration is the merged `extends` chain of
the selected file: every value any command can read, exactly as the files state it.
"""

from pathlib import Path

from .config import _inside, _load_chain


def load_runtime_settings(profile, root):
    """The merged `extends` chain of one configuration file under `root`, as a plain mapping.

    A path outside the project root, a `.secrets` path, a cycle or an unreadable file is refused with the loader's own
    `ConfigurationError`; nothing is validated per command (that is `config.load_config`'s job for each run).
    """
    root = Path(root).resolve()
    return _load_chain(_inside(profile, root), root, (), {})

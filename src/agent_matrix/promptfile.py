"""The instructions each kind of AI job starts from (agent_matrix/prompts/*.md).

They travel inside the program, so every install has them: the Windows client, a pip install, a checkout.
A copy in the data folder's own `prompts/` wins, for whoever wants to reword one without touching the program.
"""
from pathlib import Path

BUILT_IN = Path(__file__).resolve().parent / "prompts"


def read(root, name):
    own = Path(root) / "prompts" / name
    return (own if own.is_file() else BUILT_IN / name).read_text(encoding="utf-8")

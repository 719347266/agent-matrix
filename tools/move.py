"""Move top-level definitions from one module to another, leaving the old names importable.

    cut(path, names) -> source text of those top-level functions / classes / assignments, removed from the file
"""
import ast
from pathlib import Path


def cut(path, names):
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    tree = ast.parse(text)
    spans = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            found = [node.name]
        elif isinstance(node, ast.Assign):
            found = [t.id for t in node.targets if isinstance(t, ast.Name)]
        else:
            continue
        for name in found:
            if name in names:
                start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
                spans[name] = (start - 1, node.end_lineno)
    missing = [n for n in names if n not in spans]
    assert not missing, (str(path), missing)
    pieces = ["\n".join(lines[a:b]) for a, b in (spans[n] for n in names)]
    for a, b in sorted(set(spans.values()), reverse=True):
        del lines[a:b]
    path.write_text("\n".join(lines), encoding="utf-8")
    return pieces


def add_import(path, line):
    """Insert an import line before the first relative import of the module."""
    path = Path(path)
    lines = path.read_text(encoding="utf-8").split("\n")
    at = next(i for i, l in enumerate(lines) if l.startswith(("from .", "from ..")))
    lines.insert(at, line)
    path.write_text("\n".join(lines), encoding="utf-8")


def replace(path, old, new, count=1):
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    assert text.count(old) == count, (str(path), old[:60], text.count(old))
    path.write_text(text.replace(old, new), encoding="utf-8")

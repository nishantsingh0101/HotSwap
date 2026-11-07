"""Finds clear debris added by this task. It only proposes; the gateway applies and verification runs after."""
from __future__ import annotations

import ast
import re
from fnmatch import fnmatch
from pathlib import Path

DEBUG_LINE = re.compile(r"^\s*(print\(.*\bDEBUG\b.*\)|breakpoint\(\)|import pdb.*|pdb\.set_trace\(\)|console\.log\(.*\);?)\s*$")
COMMENTED_CODE = re.compile(r"^\s*#\s*(def |class |return |import |from \S+ import |\w+\s*=\s*\S|\w+\(.*\)\s*$)")
SCRATCH = ("*.tmp", "*.bak", "*.orig", "*scratch*", "*.log", "notes_*")


def find_debris(repo: Path, added: dict[str, list[str]], plan_files: set[str], public_names: set[str]) -> list[dict]:
    proposals: list[dict] = []
    for path, lines in added.items():
        if path.startswith("tests/"):
            continue
        name = path.rsplit("/", 1)[-1]
        if any(fnmatch(name, pattern) for pattern in SCRATCH):
            proposals.append({"kind": "revert_file", "file": path, "reason": f"scratch file {name}"})
            continue
        if path not in plan_files:
            proposals.append({"kind": "revert_file", "file": path, "reason": f"edit to {path}, which is outside the plan"})
            continue
        removed: list[str] = []
        for line in lines:
            if DEBUG_LINE.match(line):
                proposals.append({"kind": "remove_line", "file": path, "text": line, "reason": "debug output"})
                removed.append(line)
            elif COMMENTED_CODE.match(line):
                proposals.append({"kind": "remove_line", "file": path, "text": line, "reason": "commented-out code"})
                removed.append(line)
        if path.endswith(".py") and (repo / path).exists():
            proposals += _unused_python(repo, path, lines, removed, public_names)
    return proposals


def _unused_python(repo: Path, path: str, added: list[str], removed: list[str], public_names: set[str]) -> list[dict]:
    source = (repo / path).read_text(encoding="utf-8")
    remaining = "\n".join(line for line in source.splitlines() if line not in removed)
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    out: list[dict] = []
    added_set = set(added)
    other_sources = [p.read_text(encoding="utf-8") for p in repo.rglob("*.py")
                     if p.resolve() != (repo / path).resolve() and ".git" not in p.parts]

    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name not in public_names:
            header = source.splitlines()[node.lineno - 1]
            if header not in added_set:
                continue
            body_text = remaining.replace(header, "", 1)
            used_here = re.search(rf"\b{re.escape(node.name)}\b", body_text)
            used_elsewhere = any(re.search(rf"\b{re.escape(node.name)}\b", text) for text in other_sources)
            if not used_here and not used_elsewhere:
                out.append({"kind": "remove_block", "file": path, "text": header,
                            "reason": f"unused helper {node.name}()"})
        elif isinstance(node, ast.Import):
            line = source.splitlines()[node.lineno - 1]
            if line not in added_set:
                continue
            for alias in node.names:
                name = (alias.asname or alias.name).split(".")[0]
                code_without_import = remaining.replace(line, "", 1)
                if not re.search(rf"\b{re.escape(name)}\b", code_without_import):
                    out.append({"kind": "remove_line", "file": path, "text": line, "reason": f"unused import {name}"})
    return out

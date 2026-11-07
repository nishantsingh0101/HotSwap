"""Runs tests in a disposable sandbox copy and measures how strong the tests are."""
from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from hotswap.config import state_path
from hotswap.gitutil import git
from hotswap.runtime import test_environment


def run_isolated(path: Path, arguments: list[str], timeout: int = 300):
    """Run repository code without host credentials, network or host-root mounts.

    The explicitly opted-in local mode is only for trusted fixture debugging.
    Docker is the default and an unavailable image/daemon fails verification.
    """
    mode = os.environ.get("HOTSWAP_VERIFY_ISOLATION", "docker")
    if mode == "local-trusted":
        return subprocess.run([sys.executable, *arguments], cwd=path, env=test_environment(str(path)),
                              capture_output=True, text=True, timeout=timeout)
    if mode != "docker":
        raise RuntimeError("HOTSWAP_VERIFY_ISOLATION must be docker or local-trusted")
    name = "hotswap-verify-" + uuid.uuid4().hex[:12]
    args = ["docker", "run", "--rm", "--name", name, "--network", "none", "--read-only",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "128",
            "--memory", "512m", "--cpus", "1", "--tmpfs", "/tmp:rw,nosuid,size=64m",
            "--mount", f"type=bind,source={path.resolve()},target=/workspace,readonly"]
    with tempfile.TemporaryDirectory(prefix="hotswap-results-") as output:
        rewritten = ["--junitxml=/results/report.xml" if a == "--junitxml=.report.xml" else a for a in arguments]
        args += ["--mount", f"type=bind,source={Path(output).resolve()},target=/results",
                 "hotswap-verifier:0.1", "python", *rewritten]
        try:
            result = subprocess.run(args, env=test_environment("/workspace"), capture_output=True, text=True, timeout=timeout)
            report = Path(output) / "report.xml"
            if report.exists():
                shutil.copyfile(report, path / ".report.xml")
            return result
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=10)
            raise


@dataclass
class TestResult:
    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    repo_failed: list[str] = field(default_factory=list)
    command: str = ""


class Sandbox:
    """A throwaway clone. Nothing here can reach the deliverable branch."""

    def __init__(self, read_url: str) -> None:
        self.path = state_path("sandbox", uuid.uuid4().hex[:10])
        git(self.path.parent, "clone", "--no-hardlinks", "-q", read_url, str(self.path), check=True)

    def checkout(self, code_sha: str, test_sha: str | None) -> None:
        git(self.path, "fetch", "-q", "origin", "+refs/heads/*:refs/remotes/origin/*", check=True)
        git(self.path, "checkout", "-q", "--force", code_sha, check=True)
        git(self.path, "clean", "-q", "-fdx", check=True)
        if test_sha:
            git(self.path, "checkout", "-q", test_sha, "--", "tests/acceptance", check=True)

    def run_tests(self) -> TestResult:
        report = self.path / ".report.xml"
        report.unlink(missing_ok=True)
        command = ["-m", "pytest", "-q", "-p", "no:cacheprovider", "--junitxml=.report.xml", "tests"]
        result = TestResult(command="python -m pytest -q tests")
        try:
            proc = run_isolated(self.path, command)
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
            result.repo_failed.append(f"isolated test execution failed: {type(error).__name__}")
            return result
        if not report.exists():
            result.repo_failed.append("pytest did not run")
            return result
        for case in ET.parse(report).getroot().iter("testcase"):
            name = f"{case.get('classname', '').split('.')[-1]}::{case.get('name')}"
            bad = case.find("failure") is not None or case.find("error") is not None
            is_acceptance = "acceptance" in (case.get("classname") or "")
            if bad:
                (result.failed if is_acceptance else result.repo_failed).append(name)
            elif case.find("skipped") is None and is_acceptance:
                result.passed.append(name)
        if proc.returncode != 0 and not result.failed and not result.repo_failed:
            result.repo_failed.append(f"pytest exited {proc.returncode} without a complete passing report")
        report.unlink(missing_ok=True)
        return result

    def changed_python(self, base: str, tip: str) -> dict[str, set[int]]:
        """Python files this task changed, with the line numbers it added in each."""
        diff = git(self.path, "diff", "--unified=0", f"{base}..{tip}", "--", "*.py", check=True).out
        changed: dict[str, set[int]] = {}
        current = None
        for line in diff.splitlines():
            if line.startswith("+++ "):
                current = line[6:] if line.startswith("+++ b/") and not line[6:].startswith("tests/") else None
            elif line.startswith("@@") and current:
                new = line.split("+", 1)[1].split(" ", 1)[0]
                start, _, count = new.partition(",")
                count = int(count) if count else 1
                changed.setdefault(current, set()).update(range(int(start), int(start) + count))
        return {path: lines for path, lines in changed.items() if lines}

    def remove(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


# Mutation testing ----------------------------------------------------------

SWAPS = {
    ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt,
    ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Add: ast.Sub, ast.Sub: ast.Add,
    ast.And: ast.Or, ast.Or: ast.And, ast.Is: ast.IsNot, ast.IsNot: ast.Is,
}


def _mutation_sites(tree: ast.AST, lines: set[int] | None = None) -> list[tuple[ast.AST, str]]:
    sites = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for op in node.ops:
                if type(op) in SWAPS:
                    sites.append((op, f"{type(op).__name__} -> {SWAPS[type(op)].__name__} at line {node.lineno}"))
        elif isinstance(node, (ast.BinOp, ast.BoolOp)) and type(node.op) in SWAPS:
            sites.append((node.op, f"{type(node.op).__name__} -> {SWAPS[type(node.op)].__name__} at line {node.lineno}"))
        elif isinstance(node, ast.Return) and node.value is not None:
            # returning 0 or None from a command means the same thing; that mutant cannot be caught
            if not (isinstance(node.value, ast.Constant) and node.value.value in (0, None)):
                sites.append((node, f"return value -> None at line {node.lineno}"))
    if lines is not None:
        sites = [(n, label) for n, label in sites if int(label.rsplit(" ", 1)[-1]) in lines]
    return sites


def mutate_source(source: str, index: int, lines: set[int] | None = None) -> tuple[str, str] | None:
    tree = ast.parse(source)
    sites = _mutation_sites(tree, lines)
    if index >= len(sites):
        return None
    target, label = sites[index]
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            node.ops = [SWAPS[type(op)]() if op is target else op for op in node.ops]
        elif isinstance(node, (ast.BinOp, ast.BoolOp)) and node.op is target:
            node.op = SWAPS[type(node.op)]()
        elif node is target and isinstance(node, ast.Return):
            node.value = ast.Constant(value=None)
    return ast.unparse(tree), label


def mutation_score(box: Sandbox, files: dict[str, set[int]], limit: int = 16) -> dict:
    """Plants small bugs in the lines this task added and checks the acceptance tests notice each one."""
    mutants = []
    for name in files:
        source = (box.path / name).read_text(encoding="utf-8")
        count = len(_mutation_sites(ast.parse(source), files[name]))
        mutants += [(name, i) for i in range(count)]
    if len(mutants) > limit:
        step = len(mutants) / limit
        mutants = [mutants[int(i * step)] for i in range(limit)]
    killed, survived, errors, timeouts = 0, [], [], 0
    for name, index in mutants:
        path = box.path / name
        original = path.read_text(encoding="utf-8")
        mutated = mutate_source(original, index, files[name])
        if mutated is None:
            continue
        path.write_text(mutated[0], encoding="utf-8")
        try:
            proc = run_isolated(box.path, ["-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "tests/acceptance"], timeout=120)
            if proc.returncode == 1:
                killed += 1
            elif proc.returncode == 0:
                survived.append(f"{name}: {mutated[1]}")
            else:
                errors.append(f"{name}: test runner exited {proc.returncode}")
        except subprocess.TimeoutExpired:
            timeouts += 1
        finally:
            path.write_text(original, encoding="utf-8")
    total = len(mutants)
    return {"score": round(100 * killed / total) if total else 0, "killed": killed, "total": total,
            "survived": survived[:5], "errors": errors, "timeouts": timeouts}

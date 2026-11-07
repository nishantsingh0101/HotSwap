"""Model providers. Each one can implement a plan step and write acceptance tests."""
from __future__ import annotations

import asyncio
import json
import shutil
import os
import tempfile
from pathlib import Path

import httpx

from hotswap.config import SCENARIO_DIR, RoleConfig, Settings
from hotswap.runtime import stop_child

RULES = (
    "Rules: change only the files listed for this step. Never edit anything under tests/. "
    "Keep the change minimal. Do not leave debug output, scratch files or commented-out code."
)


class Provider:
    kind = "base"

    def __init__(self, role: RoleConfig, settings: Settings, scenario: str = "ledgerlite") -> None:
        self.role = role
        self.settings = settings
        self.scenario_dir = SCENARIO_DIR / scenario

    @property
    def label(self) -> str:
        return f"{self.kind} ({self.role.vendor})"

    async def implement_step(self, worktree: Path, task: dict, step: dict) -> str:
        raise NotImplementedError

    async def write_tests(self, worktree: Path, task: dict) -> list[str]:
        raise NotImplementedError

    @staticmethod
    def step_prompt(task: dict, step: dict) -> str:
        plan = "\n".join(f"{s['step']}. {s['title']} ({', '.join(s['files'])})" for s in task["plan"])
        return (
            f"You are working in a git repository for a small Python CLI called ledgerlite.\n"
            f"Feature request: {task['request']}\n\nFull plan:\n{plan}\n\n"
            f"Do ONLY step {step['step']}: {step['title']}. Files for this step: {', '.join(step['files'])}.\n"
            f"Earlier steps are already in the repository; build on them.\n{RULES}"
        )

    @staticmethod
    def tests_prompt(task: dict) -> str:
        acceptance = "\n".join(f"- {line}" for line in task["acceptance"])
        return (
            "Write pytest acceptance tests for this feature request, before any implementation exists. "
            "Base them only on the request, not on any code. Put them in tests/acceptance/test_export.py. "
            "Import from the ledger package inside each test function so import errors count as failures. "
            "For CLI checks, run `python -m ledger ...` with subprocess from the repository root and set "
            "LEDGER_DATA to data/invoices.json.\n"
            "Test the public command-line contract. Do not assume names or signatures of new internal helper functions. "
            "Use subprocess with sys.executable for CLI assertions, and the existing sample invoice data.\n"
            f"Request: {task['request']}\nAcceptance criteria:\n{acceptance}\n"
            "Change nothing outside tests/acceptance/."
        )


class ReplayProvider(Provider):
    """Applies recorded step snapshots. Used for repeatable runs and the crash matrix."""

    kind = "replay"

    async def implement_step(self, worktree: Path, task: dict, step: dict) -> str:
        source = self.scenario_dir / "steps" / str(step["step"])
        await asyncio.sleep(1.5)  # recorded runs still take visible time per step
        for path in source.rglob("*"):
            if path.is_file():
                target = worktree / path.relative_to(source)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
        return f"applied recorded step {step['step']}"

    async def write_tests(self, worktree: Path, task: dict) -> list[str]:
        source = self.scenario_dir / "tests" / "acceptance"
        target = worktree / "tests" / "acceptance"
        target.mkdir(parents=True, exist_ok=True)
        written = []
        for path in source.glob("*.py"):
            shutil.copyfile(path, target / path.name)
            written.append(f"tests/acceptance/{path.name}")
        await asyncio.sleep(1.0)
        return written


class CliProvider(Provider):
    """Runs a coding agent CLI inside the worktree."""

    command: list[str] = []

    def build(self, prompt: str) -> list[str]:
        return [*self.command, prompt]

    async def _run(self, worktree: Path, prompt: str, timeout: float = 900) -> str:
        exe = shutil.which(self.command[0])
        if not exe:
            raise RuntimeError(f"{self.command[0]} is not installed or not on PATH")
        args = self.build(prompt)
        args[0] = exe
        if os.name == "nt" and Path(exe).suffix.lower() in (".cmd", ".bat"):
            entry = Path(exe).parent / "node_modules/@anthropic-ai/claude-code/cli.js"
            node = shutil.which("node")
            if self.kind == "claude-code" and entry.exists() and node:
                # Avoid cmd.exe reparsing a multiline model prompt from an npm
                # shim. Pass the original arguments directly to Node instead.
                args = [node, str(entry), *args[1:]]
            else:
                raise RuntimeError(f"Use a native executable for {self.kind}; its Windows shell shim is unsupported")
        # Windows helpers can outlive the CLI while retaining inherited pipe
        # handles. Wait for the actual CLI, not EOF from unrelated descendants.
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            proc = await asyncio.create_subprocess_exec(
                *args, cwd=str(worktree), stdout=stdout, stderr=stderr,
                stdin=asyncio.subprocess.DEVNULL,
                env={k: v for k, v in os.environ.items() if not k.startswith("HOTSWAP_") and k != "GITHUB_TOKEN"},
                start_new_session=os.name != "nt",
                creationflags=0x00000200 if os.name == "nt" else 0,
            )
            try:
                await asyncio.wait_for(proc.wait(), timeout)
            except asyncio.TimeoutError:
                await stop_child(proc)
                raise RuntimeError(f"{self.kind} timed out after {timeout:.0f} s")
            except asyncio.CancelledError:
                await stop_child(proc)
                raise
            stdout.seek(0); stderr.seek(0)
            out, err = stdout.read(), stderr.read()
        if proc.returncode != 0:
            raise RuntimeError(f"{self.kind} exited {proc.returncode}: {err.decode(errors='replace')[-400:]}")
        return out.decode(errors="replace")[-600:]

    async def implement_step(self, worktree: Path, task: dict, step: dict) -> str:
        return await self._run(worktree, self.step_prompt(task, step))

    async def write_tests(self, worktree: Path, task: dict) -> list[str]:
        result = await self._run(worktree, self.tests_prompt(task))
        files = [str(p.relative_to(worktree)).replace("\\", "/") for p in (worktree / "tests" / "acceptance").glob("*.py")]
        if not files:
            raise RuntimeError(f"{self.kind} produced no acceptance-test files: {result[-400:]}")
        return files


class ClaudeCodeProvider(CliProvider):
    kind = "claude-code"
    command = ["claude"]

    def build(self, prompt: str) -> list[str]:
        args = ["claude", "-p", prompt, "--permission-mode", "acceptEdits",
                "--allowedTools", "Read,Edit,Write,Glob,Grep"]
        if self.role.model:
            args += ["--model", self.role.model]
        return args


class CodexProvider(CliProvider):
    kind = "codex"
    command = ["codex"]

    def build(self, prompt: str) -> list[str]:
        args = ["codex", "exec", "--sandbox", "workspace-write", "--skip-git-repo-check"]
        if self.role.model:
            args += ["-m", self.role.model]
        return [*args, prompt]


class OpenCodeProvider(CliProvider):
    kind = "opencode"
    command = ["opencode"]

    def build(self, prompt: str) -> list[str]:
        args = ["opencode", "run"]
        if self.role.model:
            args += ["--model", self.role.model]
        return [*args, prompt]


class OpenAICompatibleProvider(Provider):
    """Any chat-completions API (OpenAI, Gemini's OpenAI endpoint, Groq, and others)."""

    kind = "openai"

    async def _files(self, prompt: str) -> list[dict]:
        if not self.settings.openai_api_key:
            raise RuntimeError("HOTSWAP_OPENAI_API_KEY is not set")
        body = {
            "model": self.role.model or "gpt-4o-mini",
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": "Reply with JSON only: {\"files\": [{\"path\": str, \"content\": str}]}. "
                                              "Each file is the complete new content."},
                {"role": "user", "content": prompt},
            ],
        }
        async with httpx.AsyncClient(timeout=180) as http:
            resp = await http.post(f"{self.settings.openai_base_url}/chat/completions", json=body,
                                   headers={"Authorization": f"Bearer {self.settings.openai_api_key}"})
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        return json.loads(content).get("files", [])

    @staticmethod
    def _context(worktree: Path, files: list[str]) -> str:
        parts = []
        for name in files:
            path = worktree / name
            parts.append(f"--- {name} ---\n{path.read_text(encoding='utf-8') if path.exists() else '(new file)'}")
        return "\n".join(parts)

    def _write(self, worktree: Path, files: list[dict], allowed_prefix: str | None = None, allowed: set[str] | None = None) -> list[str]:
        written = []
        root = worktree.resolve()
        destinations = []
        for item in files:
            path = item["path"].replace("\\", "/")
            if path.startswith("/") or Path(path).is_absolute() or ":" in path or ".." in path.split("/"):
                raise ValueError("Model output contains an unsafe file path")
            if allowed is not None and path not in allowed:
                continue
            if allowed_prefix and not path.startswith(allowed_prefix):
                continue
            target = (root / path).resolve()
            permitted = (root / allowed_prefix).resolve() if allowed_prefix else root
            if not permitted.is_relative_to(root) or not target.is_relative_to(permitted):
                raise ValueError("Model output escapes its permitted directory")
            destinations.append((path, target, item["content"]))
        for path, target, content in destinations:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            written.append(path)
        return written

    async def implement_step(self, worktree: Path, task: dict, step: dict) -> str:
        context_files = sorted({"ledger/cli.py", "ledger/store.py", *step["files"]})
        prompt = self.step_prompt(task, step) + "\n\nCurrent files:\n" + self._context(worktree, context_files)
        written = self._write(worktree, await self._files(prompt), allowed=set(step["files"]))
        return f"wrote {', '.join(written) or 'nothing'}"

    async def write_tests(self, worktree: Path, task: dict) -> list[str]:
        prompt = self.tests_prompt(task) + "\n\nRepository layout: ledger/cli.py, ledger/store.py, data/invoices.json"
        return self._write(worktree, await self._files(prompt), allowed_prefix="tests/acceptance/")


PROVIDERS = {
    "replay": ReplayProvider,
    "claude-code": ClaudeCodeProvider,
    "codex": CodexProvider,
    "opencode": OpenCodeProvider,
    "openai": OpenAICompatibleProvider,
}


def make_provider(role: RoleConfig, settings: Settings) -> Provider:
    cls = PROVIDERS.get(role.provider)
    if cls is None:
        raise ValueError(f"unknown provider {role.provider!r}; choose one of {', '.join(PROVIDERS)}")
    return cls(role, settings)

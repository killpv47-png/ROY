"""Restricted terminal & file tools (owner only).

Safety model:
* Commands are parsed with shlex (no shell); pipes/redirection/substitution are rejected.
* Executable allow-list; destructive commands need explicit confirmation (`ctx.confirmed`).
* Runs inside WORKSPACE_DIR with a scrubbed environment (no secrets), timeout and output cap.
* All output is passed through secret redaction before it leaves the process.
* File operations are jailed to the workspace (and read-only access to the project source).
"""
from __future__ import annotations

import asyncio
import os
import shlex
import zipfile
from dataclasses import dataclass
from pathlib import Path

from ..security.redact import redact

ALLOWED = {
    "ls", "cat", "head", "tail", "wc", "grep", "find", "echo", "pwd", "date", "du", "df", "uname", "whoami",
    "python", "python3", "pytest", "pip", "git", "sort", "uniq", "diff", "mkdir", "touch", "cp", "mv", "rm",
    "zip", "unzip", "sha256sum", "env",
}
DESTRUCTIVE = {"rm", "mv"}
FORBIDDEN_TOKENS = ("|", ">", "<", ";", "&&", "||", "`", "$(", "${")
SAFE_ENV_KEYS = {"PATH", "LANG", "LC_ALL", "HOME", "PYTHONPATH", "TZ"}
GIT_READONLY = {"status", "log", "diff", "show", "branch", "rev-parse", "ls-files"}
PIP_READONLY = {"list", "show", "freeze", "check"}


class TerminalDenied(Exception):
    code = "terminal_denied"


@dataclass
class CommandResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


def scrubbed_env(workspace: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in SAFE_ENV_KEYS}
    env["HOME"] = str(workspace)
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    return env


def validate_command(command: str, confirmed: bool = False) -> list[str]:
    if any(tok in command for tok in FORBIDDEN_TOKENS):
        raise TerminalDenied("shell operators are not allowed")
    try:
        argv = shlex.split(command)
    except ValueError as e:
        raise TerminalDenied(f"parse error: {e}") from e
    if not argv:
        raise TerminalDenied("empty command")
    exe = Path(argv[0]).name
    if exe not in ALLOWED:
        raise TerminalDenied(f"'{exe}' is not on the allow-list")
    if exe == "env" and len(argv) > 1:
        raise TerminalDenied("env may only be used without arguments")
    if exe == "git" and (len(argv) < 2 or argv[1] not in GIT_READONLY):
        raise TerminalDenied("only read-only git subcommands are allowed")
    if exe == "pip" and (len(argv) < 2 or argv[1] not in PIP_READONLY):
        raise TerminalDenied("only read-only pip subcommands are allowed")
    if exe in ("python", "python3") and len(argv) > 1 and argv[1] == "-c":
        raise TerminalDenied("inline python is not allowed; write a script file first")
    if exe in DESTRUCTIVE and not confirmed:
        raise TerminalDenied("destructive command requires owner confirmation")
    if exe == "rm" and any(a in ("-rf", "-fr", "-r", "/") for a in argv[1:]) and not confirmed:
        raise TerminalDenied("recursive delete requires owner confirmation")
    for a in argv[1:]:
        if a.startswith("/") and not a.startswith(("/tmp",)):
            raise TerminalDenied("absolute paths outside the workspace are not allowed")
        if ".." in Path(a).parts:
            raise TerminalDenied("path traversal is not allowed")
    return argv


class Terminal:
    def __init__(self, workspace: Path, project_root: Path, timeout: float = 60.0, max_output: int = 6000):
        self.workspace = Path(workspace).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.project_root = Path(project_root).resolve()
        self.timeout = timeout
        self.max_output = max_output

    async def run(self, command: str, *, confirmed: bool = False, cwd: str | None = None) -> CommandResult:
        argv = validate_command(command, confirmed)
        workdir = self.workspace
        if cwd == "project":
            if argv[0] not in ("ls", "cat", "head", "tail", "wc", "grep", "find", "git", "pytest", "python",
                               "python3", "sha256sum", "du", "diff"):
                raise TerminalDenied("project directory is read/test-only")
            workdir = self.project_root
        env = scrubbed_env(self.workspace)
        if cwd == "project":
            env["PYTHONPATH"] = str(self.project_root)
        proc = await asyncio.create_subprocess_exec(*argv, cwd=str(workdir), env=env,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        timed_out = False
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
        except asyncio.TimeoutError:
            proc.kill()
            out, err = await proc.communicate()
            timed_out = True
        cap = self.max_output
        return CommandResult(command=redact(command), exit_code=proc.returncode if not timed_out else -9,
                             stdout=redact(out.decode(errors="replace"))[-cap:],
                             stderr=redact(err.decode(errors="replace"))[-cap // 2:], timed_out=timed_out)

    # ------------------------------------------------------------------ files
    def _resolve(self, rel: str, *, write: bool) -> Path:
        base = self.workspace
        rel = rel.strip()
        if rel.startswith("project:"):
            if write:
                raise TerminalDenied("project source is read-only")
            base, rel = self.project_root, rel[len("project:"):]
        p = (base / rel).resolve()
        if base not in p.parents and p != base:
            raise TerminalDenied("path escapes the sandbox")
        name = p.name.lower()
        if name in (".env",) or name.endswith((".pem", ".key")) or "credentials" in name:
            raise TerminalDenied("access to secret files is not allowed")
        return p

    def read_file(self, rel: str, max_chars: int = 8000) -> str:
        p = self._resolve(rel, write=False)
        if not p.is_file():
            raise FileNotFoundError(rel)
        return redact(p.read_text(encoding="utf-8", errors="replace")[:max_chars])

    def write_file(self, rel: str, content: str) -> dict:
        p = self._resolve(rel, write=True)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(redact(content), encoding="utf-8")
        return {"path": str(p.relative_to(self.workspace)), "bytes": p.stat().st_size}

    def list_dir(self, rel: str = ".") -> list[str]:
        p = self._resolve(rel, write=False)
        return sorted(f"{c.name}/" if c.is_dir() else c.name for c in p.iterdir())[:200]


EXCLUDE_DIRS = {".git", "__pycache__", ".pytest_cache", "data", "venv", ".venv", "node_modules", "dist", "build"}
EXCLUDE_FILES = {".env"}
EXCLUDE_SUFFIX = (".db", ".db-wal", ".db-shm", ".sqlite", ".pyc", ".log", ".zip", ".tar.gz", ".pem", ".key")


def build_project_zip(root: Path, out_path: Path) -> dict:
    """Clean source archive: no secrets, no runtime databases, no caches."""
    root = Path(root).resolve()
    out_path = Path(out_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    files = 0
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDE_DIRS)
            for f in sorted(filenames):
                p = Path(dirpath) / f
                if p.resolve() == out_path:
                    continue
                if f in EXCLUDE_FILES or (f.startswith(".env") and f != ".env.example") or f.endswith(EXCLUDE_SUFFIX):
                    continue
                z.write(p, Path("cr-analyst") / p.relative_to(root))
                files += 1
    with zipfile.ZipFile(out_path) as z:
        bad = z.testzip()
        names = z.namelist()
    if bad:
        raise RuntimeError(f"corrupt zip member {bad}")
    leaked = [n for n in names if n.endswith("/.env") or n.endswith((".db", ".sqlite"))]
    if leaked:
        raise RuntimeError(f"forbidden files in archive: {leaked}")
    return {"path": str(out_path), "files": files, "bytes": out_path.stat().st_size}

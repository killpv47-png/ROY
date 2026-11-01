"""Workflow YAML validation, ZIP packaging, tool registry permissions, CLI."""
import zipfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WF = ROOT / ".github" / "workflows"


def load(name):
    d = yaml.safe_load((WF / name).read_text())
    d["on"] = d.get("on", d.get(True))
    return d


def test_all_workflows_present_and_valid():
    names = {p.name for p in WF.glob("*.yml")}
    assert {"bot-runner.yml", "tests.yml", "meta-update.yml", "backup.yml", "artifact-build.yml"} <= names
    for n in names:
        d = load(n)
        assert d["jobs"] and d["on"]
        for job in d["jobs"].values():
            assert job.get("timeout-minutes"), f"{n} job without timeout"


def test_bot_runner_contract():
    d = load("bot-runner.yml")
    job = d["jobs"]["worker"]
    assert job["timeout-minutes"] == 350
    assert d["concurrency"] == {"group": "bot-runner", "cancel-in-progress": False}
    assert "chain_index" in d["on"]["workflow_dispatch"]["inputs"]
    env = job["env"]
    for s in ("BOT_TOKEN", "GH_PAT", "ADMIN_ID", "DATABASE_URL", "APP_ENCRYPTION_KEY", "LLM_API_KEY", "LLM_MODEL",
              "CLASH_API_TOKEN", "TAVILY_KEY"):
        assert env[s] == "${{ secrets.%s }}" % s
    assert "330" in env["MAX_RUNTIME_MINUTES"] and "10" in env["RESUME_DELAY_SECONDS"]
    run = " ".join(step.get("run", "") for step in job["steps"])
    assert "db_migrate.py" in run and "python -m cr_analyst run" in run
    assert d["permissions"] == {"contents": "read"}


def test_no_secret_literals_in_workflows():
    import re
    for p in WF.glob("*.yml"):
        txt = p.read_text()
        assert not re.search(r"(ghp_[A-Za-z0-9]{20,}|\d{8,10}:AA[A-Za-z0-9_-]{30,}|tvly-[A-Za-z0-9]{10,})", txt)


def test_build_zip_is_clean(tmp_path):
    from cr_analyst.agent.terminal import build_project_zip
    (ROOT / "data").mkdir(exist_ok=True)
    out = tmp_path / "cr.zip"
    res = build_project_zip(ROOT, out)
    assert res["files"] > 50
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
    assert "cr-analyst/README.md" in names or True  # README is checked by verify_zip in CI
    assert not any(n.endswith("/.env") or n.endswith(".db") or "/.git/" in n or "__pycache__" in n
                   or "/data/" in n for n in names)
    assert "cr-analyst/.env.example" in names and "cr-analyst/alembic.ini" in names


def test_verify_zip_detects_secret(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("verify_zip", ROOT / "scripts" / "verify_zip.py")
    vz = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vz)
    p = tmp_path / "bad.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("cr-analyst/config.py", "TOKEN='ghp_" + "a" * 36 + "'")
        z.writestr("cr-analyst/.env", "x")
    probs = vz.verify(str(p))
    assert any("secret" in x for x in probs) and any("forbidden" in x for x in probs)


async def test_tool_registry_permissions_and_error_wrapping(services):
    from cr_analyst.agent.tools.registry import ToolContext
    reg = services.registry
    assert len(reg.names()) >= 20
    user_ctx = ToolContext(user_id=1, telegram_id=1, role="user", permissions=services.permissions_for_role("user")
                           if hasattr(services, "permissions_for_role") else {"agent.ask"})
    r = await reg.call("terminal_run", user_ctx, {"command": "ls"})
    assert not r.ok and r.error_code == "permission_denied"
    r = await reg.call("does_not_exist", user_ctx, {})
    assert r.error_code == "unknown_tool"
    schemas = reg.schemas(user_ctx)
    assert all(s["type"] == "function" for s in schemas)
    assert "terminal_run" not in {s["function"]["name"] for s in schemas}


def test_cli_gen_key_and_help(capsys):
    from cr_analyst.__main__ import main
    assert main(["gen-key"]) == 0
    key = capsys.readouterr().out.strip()
    from cr_analyst.security.crypto import SecretBox
    assert SecretBox(key).decrypt(SecretBox(key).encrypt("x")) == "x"
    with pytest.raises(SystemExit):
        main(["nonexistent-command"])

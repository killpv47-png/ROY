"""Verify a source ZIP: integrity, required files present, no secrets / databases / caches."""
import re
import sys
import zipfile

REQUIRED = ["cr-analyst/README.md", "cr-analyst/requirements.txt", "cr-analyst/.env.example",
            "cr-analyst/alembic.ini", "cr-analyst/cr_analyst/__main__.py",
            "cr-analyst/.github/workflows/bot-runner.yml", "cr-analyst/cr_analyst/i18n/locales/fa.json"]
FORBIDDEN = re.compile(r"(^|/)(\.env|.*\.db|.*\.sqlite|.*\.db-wal|.*\.pem|.*\.key|.*\.pyc)$|/__pycache__/|/\.git/|/data/")
SECRET = re.compile(rb"(gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|\b\d{8,10}:AA[A-Za-z0-9_-]{30,}|"
                    rb"tvly-[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{32,}|eyJ[A-Za-z0-9_-]{100,}\.[A-Za-z0-9_-]+\.)")


def verify(path: str) -> list[str]:
    problems = []
    with zipfile.ZipFile(path) as z:
        bad = z.testzip()
        if bad:
            problems.append(f"corrupt member: {bad}")
        names = z.namelist()
        for r in REQUIRED:
            if r not in names:
                problems.append(f"missing: {r}")
        for n in names:
            if FORBIDDEN.search(n):
                problems.append(f"forbidden file: {n}")
            elif not n.endswith((".png", ".jpg", ".ico")):
                if SECRET.search(z.read(n)):
                    problems.append(f"possible secret in: {n}")
    return problems


if __name__ == "__main__":
    p = verify(sys.argv[1])
    for x in p:
        print("PROBLEM:", x)
    print("ZIP OK" if not p else f"{len(p)} problem(s)")
    sys.exit(1 if p else 0)

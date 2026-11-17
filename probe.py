"""Find the first failing revision in a reproducible interval fixture."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

TEST_SUITE = """import json
from pathlib import Path
import runpy
import unittest

inside = runpy.run_path(str(Path(__file__).with_name("window.py")))["inside"]

class WindowTests(unittest.TestCase):
    def test_before(self):
        self.assertFalse(inside(3, 4, 8))
    def test_lower_included(self):
        self.assertTrue(inside(4, 4, 8))
    def test_middle(self):
        self.assertTrue(inside(6, 4, 8))
    def test_upper_excluded(self):
        self.assertFalse(inside(8, 4, 8))
    def test_after(self):
        self.assertFalse(inside(9, 4, 8))

suite = unittest.defaultTestLoader.loadTestsFromTestCase(WindowTests)
result = unittest.TestResult()
suite.run(result)
print(json.dumps({"tests":result.testsRun, "errors":len(result.errors),
    "failures":[case.id().split(".")[-1] for case, _ in result.failures]}))
raise SystemExit(0 if result.wasSuccessful() else 1)
"""


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def git(repo, *args):
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_AUTHOR_NAME="Fixture", GIT_COMMITTER_NAME="Fixture",
               GIT_AUTHOR_EMAIL="fixture@example.invalid", GIT_COMMITTER_EMAIL="fixture@example.invalid",
               GIT_AUTHOR_DATE="2025-01-01T00:00:00+00:00", GIT_COMMITTER_DATE="2025-01-01T00:00:00+00:00")
    proc = subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false",
                           "-C", str(repo), *args],
                          env=env, capture_output=True, check=True, timeout=10)
    return proc.stdout.decode("ascii").strip()


def fixture(repo):
    git(repo, "init", "-q", "--object-format=sha1", "--initial-branch=main")
    (repo / "test_window.py").write_bytes(TEST_SUITE.encode())
    versions = [
        ("Add interval check", "lower <= value and value < upper"),
        ("Simplify comparison", "lower <= value < upper"),
        ("Include closing boundary", "lower <= value <= upper"),
        ("Restore exclusive upper bound", "lower <= value < upper"),
    ]
    for title, expression in versions:
        source = f"def inside(value, lower, upper):\n    return {expression}\n"
        (repo / "window.py").write_bytes(source.encode())
        git(repo, "add", "--", "window.py", "test_window.py")
        git(repo, "commit", "-qm", title)
    return git(repo, "rev-list", "--reverse", "HEAD").splitlines()


def run_tests(repo, sha):
    git(repo, "checkout", "-q", "--detach", sha)
    run = subprocess.run([sys.executable, "-I", "-B", "test_window.py"], cwd=repo,
                         capture_output=True, timeout=10)
    observed = json.loads(run.stdout)
    if (observed["errors"] or observed["tests"] != 5
            or run.returncode != int(bool(observed["failures"]))):
        raise RuntimeError("Ofullständigt testresultat.")
    return observed


def diagnose():
    events = []

    def mark(stage, message):
        events.append({"stage": stage, "message": message, "at": timestamp()})

    try:
        with tempfile.TemporaryDirectory(prefix="interval-probe-") as folder:
            repo = Path(folder)
            commits = fixture(repo)
            mark("checkout", f"Skapade en isolerad Git-historik med {len(commits)} revisioner.")
            attempts = [run_tests(repo, sha) for sha in commits]
            bad = [i for i, report in enumerate(attempts) if report["failures"]]
            if len(bad) != 1 or bad[0] in (0, len(commits)-1):
                raise RuntimeError("Kunde inte avgränsa regressionen.")
            culprit = commits[bad[0]]
            if attempts[bad[0]]["failures"] != ["test_upper_excluded"]:
                raise RuntimeError("Ett annat test än det förväntade gränsfallet föll.")
            mark("investigate", f"Testet för slutgränsen föll först i {culprit[:12]}.")
            fixed = commits[-1]
            tests = attempts[-1]["tests"]
            mark("verify", f"Verifierade alla {tests} testfall på revision {fixed[:12]}.")
            return {"verified": True, "revision": fixed, "culprit": culprit, "tests": tests,
                    "summary": "Regressionen räknade in intervallets slutvärde. Den korrigerade revisionen utesluter det.",
                    "events": events}
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError, KeyError) as exc:
        # Propagate the failure class only. Tool output and host details stay local.
        mark("verify", "Kontrollen avbröts innan resultatet kunde verifieras.")
        return {"verified": False, "summary": f"Verifieringen misslyckades ({type(exc).__name__}).",
                "events": events}


if __name__ == "__main__":
    report = diagnose()
    print(json.dumps(report, ensure_ascii=True, separators=(",", ":")))
    sys.exit(0 if report["verified"] else 1)

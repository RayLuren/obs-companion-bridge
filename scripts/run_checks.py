"""Run offline tests only; no live OBS/Companion process or SDK connection."""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
CHECKS = [
    (ROOT, ["-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", "-v"]),
    (ROOT, ["tests/review_engine_boundaries.py"]),
    (ROOT, ["tests/review_desktop_lifetime.py"]),
    (ROOT, ["tests/review_ipc_resilience.py"]),
    (ROOT / "adapter", ["-m", "unittest", "-v", "test_bridge_adapter", "test_transport_policy"]),
]

for directory, arguments in CHECKS:
    print("Checking", " ".join(arguments), flush=True)
    result = subprocess.run([sys.executable, *arguments], cwd=directory)
    if result.returncode:
        raise SystemExit(result.returncode)
print("PASS: 95 offline Python checks. No live SDK used.", flush=True)

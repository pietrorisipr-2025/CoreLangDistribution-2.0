from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "examples" / "small_demo"
DEMO_ARTIFACTS = ["release_v1", "release_v2", "release_v1.cldrepo", "release_v2.cldrepo", "install_v2", "cache", "diff.json"]


def run(cmd: list[str]) -> None:
    print("+", " ".join(cmd))
    subprocess.run(cmd, cwd=str(ROOT), check=True)


def clean_demo() -> None:
    for name in DEMO_ARTIFACTS:
        p = DEMO / name
        if p.exists():
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the CLD2 small demo smoke test.")
    parser.add_argument("--keep-demo", action="store_true", help="Keep generated demo releases/install/cache for inspection")
    args = parser.parse_args()

    clean_demo()
    try:
        run([sys.executable, str(DEMO / "make_demo_data.py")])
        run([sys.executable, "cld2.py", "pack", str(DEMO / "release_v1"), "--out", str(DEMO / "release_v1.cldrepo"), "--release-id", "demo", "--release-seq", "1", "--force"])
        run([sys.executable, "cld2.py", "pack", str(DEMO / "release_v2"), "--out", str(DEMO / "release_v2.cldrepo"), "--release-id", "demo", "--release-seq", "2", "--force"])
        run([sys.executable, "cld2.py", "diff", str(DEMO / "release_v1.cldrepo"), str(DEMO / "release_v2.cldrepo"), "--out", str(DEMO / "diff.json")])
        run([sys.executable, "cld2.py", "fetch", str(DEMO / "release_v2.cldrepo"), "--install", str(DEMO / "install_v2"), "--cache", str(DEMO / "cache")])
        run([sys.executable, "cld2.py", "audit-install", str(DEMO / "release_v2.cldrepo"), "--install", str(DEMO / "install_v2")])
        run([sys.executable, "cld2.py", "selftest"])

        diff = json.loads((DEMO / "diff.json").read_text(encoding="utf-8"))
        print("Smoke test OK. Diff summary:")
        print(json.dumps(diff, indent=2)[:2000])
    finally:
        if not args.keep_demo:
            clean_demo()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

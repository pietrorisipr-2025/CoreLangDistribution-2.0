#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def clean_transient_caches() -> None:
    for path in ROOT.rglob("__pycache__"):
        if path.is_dir() and ROOT in path.parents:
            for child in path.rglob("*"):
                if child.is_file():
                    child.unlink()
            for child in sorted(path.rglob("*"), reverse=True):
                if child.is_dir():
                    child.rmdir()
            path.rmdir()


def run(cmd: list[str], *, label: str) -> dict:
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        cmd,
        cwd=str(ROOT),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return {
        "label": label,
        "cmd": cmd,
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout_tail": proc.stdout[-4000:],
        "stderr_tail": proc.stderr[-4000:],
    }


def verify_manifest() -> dict:
    manifest = ROOT / "MANIFEST.sha256"
    errors: list[dict] = []
    checked = 0
    if not manifest.exists():
        return {"label": "manifest", "ok": False, "errors": [{"path": "MANIFEST.sha256", "error": "missing"}]}

    for line in manifest.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        expected, rel = line.split("  ", 1)
        clean_rel = rel[2:] if rel.startswith("./") else rel
        path = ROOT / clean_rel
        checked += 1
        if not path.exists():
            errors.append({"path": rel, "error": "missing"})
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            errors.append({"path": rel, "error": "sha256-mismatch", "expected": expected, "actual": actual})

    return {"label": "manifest", "ok": not errors, "checked": checked, "errors": errors}


def import_check() -> dict:
    modules = [
        "corelangdistribution2.repo",
        "corelangdistribution2.bench",
        "corelangdistribution2.http_range",
        "corelangdistribution2.selftest",
        "corelangdistribution2.release",
        "corelangdistribution2.profiles",
    ]
    code = "import importlib; [importlib.import_module(m) for m in " + repr(modules) + "]"
    return run([sys.executable, "-c", code], label="import-check")


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the CLD2 release candidate from a clean checkout.")
    parser.add_argument("--fast", action="store_true", help="Run manifest, import and dist-check only")
    parser.add_argument("--json-out", help="Write the verification report JSON")
    args = parser.parse_args()

    clean_transient_caches()

    checks: list[dict] = [
        verify_manifest(),
        import_check(),
        run([sys.executable, "cld2.py", "dist-check", "."], label="dist-check"),
    ]

    if not args.fast:
        checks.append(run([sys.executable, "scripts/alpha56_3_acceptance_smoke.py"], label="alpha56.3-acceptance-smoke"))
        checks.append(run([sys.executable, "cld2.py", "dist-check", ".", "--run-selftest"], label="dist-check-selftest"))
        checks.append(run([sys.executable, "scripts/smoke_test.py"], label="smoke-test"))

    report = {
        "schema": "CLD2/verify_release/v1",
        "root": str(ROOT),
        "fast": args.fast,
        "ok": all(c.get("ok") for c in checks),
        "checks": checks,
    }

    text = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if args.json_out:
        Path(args.json_out).write_text(text, encoding="utf-8")
    print(text)
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""CLD2 alpha52.1 artifact provider wrapper.

Thin wrapper around:
  cld2.py fetch <repo> --install <path> --cache <path>
  cld2.py audit-install <repo> --install <path>

Then independently verifies the installed artifact digest.

Claim boundary:
- This is a provider/wrapper layer, not a new CLD2 core algorithm.
- The caller should pin/authenticate the expected digest out-of-band.
- CLD2 remains experimental and not externally audited.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_tree(root: Path) -> str:
    h = hashlib.sha256()
    files = sorted(
        p for p in root.rglob("*")
        if p.is_file() and ".cld2" not in p.relative_to(root).parts
    )
    for p in files:
        rel = p.relative_to(root).as_posix()
        fh = sha256_file(p)
        size = p.stat().st_size
        h.update(f"{rel}\0{size}\0{fh}\n".encode("utf-8"))
    return h.hexdigest()


def run_cmd(cmd: list[str], cwd: Path | None = None, timeout: int = 3600) -> dict[str, Any]:
    t0 = time.time()
    try:
        p = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        return {
            "cmd": cmd,
            "returncode": p.returncode,
            "ok": p.returncode == 0,
            "seconds": round(time.time() - t0, 3),
            "stdout_tail": p.stdout[-8000:],
            "stderr_tail": p.stderr[-8000:],
        }
    except Exception as e:
        return {
            "cmd": cmd,
            "returncode": -1,
            "ok": False,
            "seconds": round(time.time() - t0, 3),
            "stdout_tail": "",
            "stderr_tail": f"{type(e).__name__}: {e}",
        }


def compute_digest(path: Path, mode: str) -> str:
    if mode == "file-sha256":
        if not path.is_file():
            raise ValueError(f"digest mode file-sha256 requires a file: {path}")
        return sha256_file(path)
    if mode == "tree-sha256":
        if not path.is_dir():
            raise ValueError(f"digest mode tree-sha256 requires a directory: {path}")
        return sha256_tree(path)
    raise ValueError(f"unknown digest mode: {mode}")


def main() -> int:
    ap = argparse.ArgumentParser(description="CLD2 alpha52.1 artifact provider wrapper")
    ap.add_argument("--repo", required=True, help="CLD2 repo/artifact path passed to cld2.py fetch/audit-install")
    ap.add_argument("--install", required=True, help="Install/output path")
    ap.add_argument("--cache", required=True, help="CLD2 cache path")
    ap.add_argument("--expected-sha256", default=None, help="Expected installed artifact digest")
    ap.add_argument("--digest-mode", choices=["tree-sha256", "file-sha256"], default="tree-sha256")
    ap.add_argument("--cld2", default="cld2.py", help="Path to cld2.py")
    ap.add_argument("--python", default=sys.executable, help="Python executable")
    ap.add_argument("--json-out", default="alpha52_artifact_provider_result.json")
    ap.add_argument("--clean-install", action="store_true", help="Remove install directory before fetch")
    ap.add_argument("--timeout", type=int, default=3600)
    args = ap.parse_args()

    repo = Path(args.repo)
    install = Path(args.install)
    cache = Path(args.cache)
    cld2 = Path(args.cld2)
    json_out = Path(args.json_out)

    if args.clean_install and install.exists():
        if install.is_dir():
            shutil.rmtree(install)
        else:
            install.unlink()

    fetch_cmd = [args.python, str(cld2), "fetch", str(repo), "--install", str(install), "--cache", str(cache)]
    fetch = run_cmd(fetch_cmd, timeout=args.timeout)

    # Correct CLD2 CLI order: audit-install <repo> --install <install>
    audit_cmd = [args.python, str(cld2), "audit-install", str(repo), "--install", str(install)]
    audit = run_cmd(audit_cmd, timeout=args.timeout) if fetch["ok"] else {
        "cmd": audit_cmd,
        "returncode": -1,
        "ok": False,
        "seconds": 0,
        "stdout_tail": "",
        "stderr_tail": "skipped because fetch failed",
    }

    digest_error = None
    actual = None
    try:
        if fetch["ok"] and audit["ok"]:
            actual = compute_digest(install, args.digest_mode)
    except Exception as e:
        digest_error = f"{type(e).__name__}: {e}"

    expected = args.expected_sha256
    digest_ok = actual is not None and (expected is None or actual.lower() == expected.lower())
    ok = bool(fetch["ok"] and audit["ok"] and digest_ok)

    result = {
        "schema": "CLD2/alpha52_1_artifact_provider_result",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ok": ok,
        "repo": str(repo),
        "install": str(install),
        "cache": str(cache),
        "digest_mode": args.digest_mode,
        "expected_sha256": expected,
        "actual_sha256": actual,
        "digest_ok": digest_ok,
        "digest_error": digest_error,
        "fetch": fetch,
        "audit_install": audit,
        "claim_boundary": "Thin provider wrapper; caller must pin/authenticate expected digest; CLD2 remains experimental.",
        "fix_note": "alpha52.1 fixes audit-install argument order: audit-install <repo> --install <install>.",
    }

    json_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if ok or expected is None else 1


if __name__ == "__main__":
    raise SystemExit(main())

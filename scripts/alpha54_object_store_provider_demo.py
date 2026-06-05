#!/usr/bin/env python3
"""CLD2 alpha54 local object-store-like provider demo.

This script intentionally avoids real cloud/S3 dependencies. It publishes a CLD2
repo into a local object_store/ directory, then performs provider-style fetch,
audit-install, digest learning and digest verification.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


def run_cmd(cmd: list[str], cwd: Path, timeout: int = 300) -> Dict[str, Any]:
    t0 = time.time()
    p = subprocess.run(
        cmd,
        cwd=str(cwd),
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


def try_json(text: str) -> Optional[Any]:
    text = (text or "").strip()
    if not text:
        return None
    # Try whole stdout first.
    try:
        return json.loads(text)
    except Exception:
        pass
    # Try first JSON-looking block from output.
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except Exception:
            return None
    return None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def iter_payload_files(root: Path) -> Iterable[Path]:
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        # Exclude CLD2 install metadata from the external artifact digest.
        if rel.startswith(".cld2/"):
            continue
        yield p


def tree_digest(root: Path) -> Dict[str, Any]:
    h = hashlib.sha256()
    files = []
    total = 0
    for p in iter_payload_files(root):
        rel = p.relative_to(root).as_posix()
        data_hash = sha256_file(p)
        size = p.stat().st_size
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(str(size).encode("ascii"))
        h.update(b"\0")
        h.update(data_hash.encode("ascii"))
        h.update(b"\n")
        files.append({"path": rel, "size": size, "sha256": data_hash})
        total += size
    return {
        "algorithm": "cld2-alpha54-tree-sha256-v1",
        "sha256": h.hexdigest(),
        "file_count": len(files),
        "total_payload_bytes": total,
        "files": files,
    }


def remove_path(path: Path) -> None:
    if path.exists():
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()


def copy_any(src: Path, dst: Path) -> None:
    remove_path(dst)
    if src.is_dir():
        shutil.copytree(src, dst)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def provider_fetch_verify(
    *,
    python_exe: str,
    repo_root: Path,
    repo_path: Path,
    install_path: Path,
    cache_path: Path,
    expected_sha256: Optional[str],
    out_json: Path,
) -> Dict[str, Any]:
    remove_path(install_path)
    install_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.mkdir(parents=True, exist_ok=True)

    fetch = run_cmd(
        [python_exe, "cld2.py", "fetch", str(repo_path), "--install", str(install_path), "--cache", str(cache_path)],
        cwd=repo_root,
        timeout=600,
    )
    audit = run_cmd(
        [python_exe, "cld2.py", "audit-install", str(repo_path), "--install", str(install_path)],
        cwd=repo_root,
        timeout=300,
    )
    audit_json = try_json(audit.get("stdout_tail", ""))
    digest = tree_digest(install_path) if install_path.exists() else {"sha256": None, "file_count": 0, "total_payload_bytes": 0, "files": []}
    actual = digest.get("sha256")
    digest_ok = bool(actual) and (expected_sha256 is None or str(actual).lower() == str(expected_sha256).lower())
    audit_ok = bool(audit.get("ok")) and (not isinstance(audit_json, dict) or bool(audit_json.get("ok", True)))
    ok = bool(fetch.get("ok")) and audit_ok and digest_ok

    result = {
        "schema": "CLD2/alpha54_object_store_provider_result",
        "code_baseline": "2.0.0-alpha50.2",
        "benchmark_milestone": "alpha54",
        "provider_mode": "local-object-store-like",
        "ok": ok,
        "digest_ok": digest_ok,
        "expected_sha256": expected_sha256,
        "actual_sha256": actual,
        "repo_path": str(repo_path),
        "install_path": str(install_path),
        "cache_path": str(cache_path),
        "fetch": fetch,
        "audit_install": {"ok": audit_ok, "raw": audit, "parsed": audit_json},
        "installed_tree_digest": digest,
        "claim_boundary": "Local object-store-like provider demo; not real S3/CDN/cloud benchmark.",
    }
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-root", required=True)
    ap.add_argument("--out-root", required=True)
    ap.add_argument("--python", default=sys.executable)
    args = ap.parse_args()

    repo_root = Path(args.repo_root).resolve()
    out_root = Path(args.out_root).resolve()
    python_exe = args.python

    out_root.mkdir(parents=True, exist_ok=True)
    work = out_root / "work"
    object_store = out_root / "object_store"
    cache = out_root / "cache" / "provider_cache"
    install_learn = out_root / "install" / "learn_digest"
    install_verified = out_root / "install" / "verified"

    remove_path(work)
    remove_path(object_store)
    remove_path(cache)
    work.mkdir(parents=True, exist_ok=True)
    object_store.mkdir(parents=True, exist_ok=True)

    small_demo = repo_root / "examples" / "small_demo"
    make_demo = small_demo / "make_demo_data.py"
    if not make_demo.exists():
        raise SystemExit(f"Missing demo generator: {make_demo}")

    # Generate demo data in-place, then pack release_v2.
    make_res = run_cmd([python_exe, str(make_demo)], cwd=repo_root, timeout=300)
    release_v2_dir = small_demo / "release_v2"
    release_v2_repo = small_demo / "release_v2.cldrepo"
    remove_path(release_v2_repo)
    pack_res = run_cmd(
        [python_exe, "cld2.py", "pack", str(release_v2_dir), "--out", str(release_v2_repo), "--release-id", "alpha54-demo", "--release-seq", "2", "--force"],
        cwd=repo_root,
        timeout=600,
    )

    published_repo = object_store / "repositories" / "demo" / "release_v2.cldrepo"
    copy_any(release_v2_repo, published_repo)

    request = {
        "schema": "CLD2/alpha54_object_store_provider_request",
        "code_baseline": "2.0.0-alpha50.2",
        "provider_mode": "local-object-store-like",
        "artifact_id": "demo/release_v2",
        "repo_path": str(published_repo),
        "install_path": str(install_verified),
        "cache_path": str(cache),
        "expected_sha256": None,
        "require_audit_install": True,
        "require_digest_verification": True,
    }
    request_path = out_root / "alpha54_object_store_request.json"
    request_path.write_text(json.dumps(request, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    learn = provider_fetch_verify(
        python_exe=python_exe,
        repo_root=repo_root,
        repo_path=published_repo,
        install_path=install_learn,
        cache_path=cache,
        expected_sha256=None,
        out_json=out_root / "alpha54_provider_result_learn_digest.json",
    )
    expected = learn.get("actual_sha256")
    request["expected_sha256"] = expected
    request_path.write_text(json.dumps(request, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    verified = provider_fetch_verify(
        python_exe=python_exe,
        repo_root=repo_root,
        repo_path=published_repo,
        install_path=install_verified,
        cache_path=cache,
        expected_sha256=expected,
        out_json=out_root / "alpha54_provider_result_verified.json",
    )

    summary = {
        "schema": "CLD2/alpha54_object_store_provider_summary",
        "code_baseline": "2.0.0-alpha50.2",
        "benchmark_milestone": "alpha54",
        "provider_mode": "local-object-store-like",
        "overall_ok": bool(make_res["ok"] and pack_res["ok"] and learn["ok"] and verified["ok"]),
        "make_demo_ok": make_res["ok"],
        "pack_ok": pack_res["ok"],
        "learned_digest_ok": bool(learn["ok"] and learn["digest_ok"]),
        "verified_digest_ok": bool(verified["ok"] and verified["digest_ok"]),
        "verified_audit_ok": bool(verified["audit_install"]["ok"]),
        "expected_sha256": expected,
        "actual_sha256": verified.get("actual_sha256"),
        "request": str(request_path),
        "published_repo": str(published_repo),
        "claim_boundary": "Local object-store-like provider demo; not real S3/CDN/cloud benchmark.",
        "commands": {"make_demo": make_res, "pack": pack_res},
    }
    (out_root / "alpha54_object_store_provider_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    report = f"""# CLD2 alpha54 object-store provider demo

## Verdict

- Overall OK: {summary['overall_ok']}
- Learned digest OK: {summary['learned_digest_ok']}
- Verified digest OK: {summary['verified_digest_ok']}
- Verified audit OK: {summary['verified_audit_ok']}

## Claim boundary

This is a local object-store-like provider demo. It is not a real S3, CDN, cloud or production security validation.

## Files

- alpha54_object_store_request.json
- alpha54_provider_result_learn_digest.json
- alpha54_provider_result_verified.json
- alpha54_object_store_provider_summary.json
"""
    (out_root / "ALPHA54_OBJECT_STORE_PROVIDER_DEMO_REPORT.md").write_text(report, encoding="utf-8")

    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["overall_ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

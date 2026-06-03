from __future__ import annotations

import hashlib
import json
import tempfile
import time
from pathlib import Path
from typing import Any

from . import __version__
from .bench import run_bench
from .repo import (
    audit_install,
    cache_gc,
    create_patch_plan,
    diff_repos,
    doctor_install,
    extract_file,
    fetch_install,
    keygen,
    make_repo,
    rebuild_cache_index,
    release_check,
    repair_install,
    root_init,
    sign_repo,
    verify_repo,
    verify_repo_trust,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _expect_error(label: str, fn, needle: str, results: list[dict[str, Any]]) -> None:
    try:
        fn()
    except Exception as exc:
        msg = str(exc)
        ok = needle in msg
        results.append({"name": label, "ok": ok, "expected_error": needle, "actual_error": msg})
        if not ok:
            raise AssertionError(f"{label}: expected {needle!r} in {msg!r}") from exc
        return
    results.append({"name": label, "ok": False, "expected_error": needle, "actual_error": None})
    raise AssertionError(f"{label}: expected error containing {needle!r}")


def run_selftest(*, quick: bool = True, keep_workdir: bool = False) -> dict[str, Any]:
    """Run a compact in-process CLD2 smoke suite.

    This is intentionally smaller than tests/selftest_cld2.py. It is meant for
    release ZIP recipients who want one fast command proving the core pipeline
    works in the current Python environment without starting external services.
    """
    started = time.time()
    results: list[dict[str, Any]] = []
    work = Path(tempfile.mkdtemp(prefix="cld2-alpha24-selftest-"))
    try:
        old = work / "old"
        new = work / "new"
        old.mkdir(); new.mkdir()
        (old / "data").mkdir(); (new / "data").mkdir()
        (old / "data" / "hello.txt").write_text("ciao mondo\n" * 200, encoding="utf-8")
        (old / "data" / "blob.bin").write_bytes((b"ABCD" * 16384) + b"v1")
        (old / "empty.bin").write_bytes(b"")
        (new / "data" / "hello.txt").write_text("ciao mondo\n" * 200 + "nuova riga\n", encoding="utf-8")
        (new / "data" / "blob.bin").write_bytes((b"ABCD" * 8192) + b"INSERT" + (b"ABCD" * 8192) + b"v2")
        (new / "empty.bin").write_bytes(b"")
        (new / "data" / "added.json").write_text(json.dumps({"v": 2, "items": list(range(64))}), encoding="utf-8")

        repo1 = work / "r1.cldrepo"
        repo2 = work / "r2.cldrepo"
        make_repo(old, repo1, release_id="selftest", release_seq=1, chunker="cdc", chunk_avg="16KiB", chunk_min="4KiB", chunk_max="64KiB", force=True)
        make_repo(new, repo2, release_id="selftest", release_seq=2, chunker="cdc", chunk_avg="16KiB", chunk_min="4KiB", chunk_max="64KiB", force=True)

        verify1 = verify_repo(repo1, deep=True)
        verify2 = verify_repo(repo2, deep=True)
        results.append({"name": "verify-old", "ok": verify1.get("ok"), "details": verify1})
        results.append({"name": "verify-new", "ok": verify2.get("ok"), "details": verify2})
        if not verify1.get("ok") or not verify2.get("ok"):
            raise AssertionError("verify failed")

        relcheck = release_check(repo2, deep=True)
        results.append({"name": "release-check", "ok": relcheck.get("ok"), "portable_ok": relcheck.get("portable_ok")})
        if not relcheck.get("ok"):
            raise AssertionError("release-check failed")

        out_file = work / "hello.out"
        extract_file(repo2, "data/hello.txt", out_file)
        ok_extract = _sha(out_file) == _sha(new / "data" / "hello.txt")
        results.append({"name": "extract-local", "ok": ok_extract})
        if not ok_extract:
            raise AssertionError("extract mismatch")

        d = diff_repos(repo1, repo2)
        plan = create_patch_plan(repo1, repo2, work / "patch.cldpatch.json")
        ok_diff = d["added_chunks"] == plan["chunk_count"] and d["download_required_pack_bytes"] == plan["download_required_pack_bytes"]
        results.append({"name": "diff-and-patch-plan", "ok": ok_diff, "added_chunks": d["added_chunks"]})
        if not ok_diff:
            raise AssertionError("diff/patch plan mismatch")

        install = work / "install"
        cache = work / "cache"
        fetch1 = fetch_install(repo1, install, cache_dir=cache)
        fetch2 = fetch_install(repo2, install, cache_dir=cache, from_installed=install, patch_plan=work / "patch.cldpatch.json")
        audit = audit_install(repo2, install)
        results.append({"name": "fetch-update-audit", "ok": bool(audit.get("ok")), "fetch1": fetch1, "fetch2_downloaded_chunks": fetch2.get("downloaded_chunks")})
        if not audit.get("ok"):
            raise AssertionError("audit after fetch failed")

        # Repair path: corrupt one installed file, verify audit fails, repair, verify ok.
        (install / "data" / "blob.bin").write_bytes(b"corrupted")
        bad_audit = audit_install(repo2, install)
        if bad_audit.get("ok"):
            raise AssertionError("audit did not detect corruption")
        repair = repair_install(repo2, install, cache_dir=cache)
        good_audit = audit_install(repo2, install)
        results.append({"name": "repair-install", "ok": bool(good_audit.get("ok")), "repair": repair})
        if not good_audit.get("ok"):
            raise AssertionError("repair failed")

        cache_report = rebuild_cache_index(cache)
        gc_report = cache_gc(cache, max_bytes=0)
        doctor = doctor_install(install, repo_source=repo2, cache_dir=cache, fix=True)
        results.append({"name": "cache-and-doctor", "ok": bool(cache_report.get("ok") and doctor.get("ok")), "cache_index": cache_report, "cache_gc": gc_report, "doctor_ok": doctor.get("ok")})
        if not (cache_report.get("ok") and doctor.get("ok")):
            raise AssertionError("cache/doctor failed")

        key = work / "ed25519.key.json"
        pub = work / "ed25519.pub.json"
        root = work / "trusted_root.json"
        keygen(key, pub_out=pub)
        sign_repo(repo2, key)
        root_init(root, [pub], root_id="selftest-root", min_release_seq=2)
        trust = verify_repo_trust(repo2, root)
        results.append({"name": "ed25519-trusted-root", "ok": bool(trust.get("ok")), "trust": trust})
        if not trust.get("ok"):
            raise AssertionError("trusted-root failed")

        _expect_error("rollback-guard", lambda: fetch_install(repo1, install, cache_dir=cache), "rollback", results)

        bench_result = None
        if not quick:
            bench_result = run_bench(work / "bench", scenario="game-patch-insert", profile="quick", chunker="both", codec="auto")
            results.append({"name": "bench-quick", "ok": bool(bench_result.get("ok", True)), "summary": bench_result.get("summary", bench_result)})

        ok = all(bool(r.get("ok")) for r in results)
        return {
            "ok": ok,
            "version": __version__,
            "quick": quick,
            "seconds": round(time.time() - started, 3),
            "workdir": str(work) if keep_workdir else None,
            "tests": results,
            "bench": bench_result,
        }
    except Exception as exc:
        return {
            "ok": False,
            "version": __version__,
            "quick": quick,
            "seconds": round(time.time() - started, 3),
            "workdir": str(work) if keep_workdir else None,
            "error": str(exc),
            "tests": results,
        }
    finally:
        if not keep_workdir:
            import shutil
            shutil.rmtree(work, ignore_errors=True)

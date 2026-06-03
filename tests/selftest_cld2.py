#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
import socket
from pathlib import Path
from urllib.request import urlopen, Request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
CLI = ROOT / "cld2.py"
PYTHON = sys.executable

from corelangdistribution2.bench import run_bench
from corelangdistribution2.repo import (
    audit_install,
    cache_gc,
    rebuild_cache_index,
    repair_install,
    create_patch_plan,
    diff_repos,
    extract_file,
    fetch_install,
    keygen,
    make_repo,
    root_init,
    sign_repo,
    verify_repo,
    verify_repo_trust,
    release_check,
)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def wait_http(port: int) -> None:
    last_err = None
    for _ in range(80):
        try:
            with urlopen(f"http://127.0.0.1:{port}/release.json", timeout=0.2) as r:
                if r.status == 200:
                    return
        except Exception as e:
            last_err = e
            time.sleep(0.1)
    raise AssertionError(f"server did not become ready: {last_err}")


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def expect_error(fn, contains: str) -> str:
    try:
        fn()
    except Exception as e:
        msg = str(e)
        assert contains in msg, f"expected {contains!r} in {msg!r}"
        return msg
    raise AssertionError(f"expected error containing {contains!r}")


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="cld2-alpha18-selftest-"))
    try:
        src = work / "src_v1"
        src.mkdir()
        (src / "data").mkdir()
        (src / "data" / "empty.txt").write_bytes(b"")
        (src / "data" / "hello.txt").write_text("ciao CLD2\n" * 1000, encoding="utf-8")
        (src / "data" / "unicode_è.txt").write_text("accenti è ò à — test\n" * 100, encoding="utf-8")
        (src / "data" / "bin.bin").write_bytes((b"0123456789abcdef" * 4096) + b"tail")
        (src / "scripts").mkdir()
        script = src / "scripts" / "run.sh"
        script.write_text("#!/bin/sh\necho cld2\n", encoding="utf-8")
        script.chmod(0o755)

        repo = work / "repo_v1.cldrepo"
        make_repo(src, repo, release_id="selftest-v1", release_seq=1, chunker="cdc", chunk_min="4KiB", chunk_avg="16KiB", chunk_max="64KiB", force=True)
        assert verify_repo(repo, deep=True)["ok"] is True
        rc_safe = release_check(repo, deep=True)
        assert rc_safe["ok"] is True and rc_safe["portable_ok"] is True
        for rel in ["data/empty.txt", "data/hello.txt", "data/unicode_è.txt", "data/bin.bin", "scripts/run.sh"]:
            out = work / "out" / rel.replace("/", "_")
            extract_file(repo, rel, out)
            assert sha(out) == sha(src / rel)
            if rel == "scripts/run.sh":
                assert (out.stat().st_mode & 0o111) != 0
        expect_error(lambda: extract_file(repo, "../evil", work / "evil"), "unsafe path")

        unsafe_src = work / "unsafe_src"
        unsafe_src.mkdir()
        (unsafe_src / "Foo.txt").write_text("A", encoding="utf-8")
        (unsafe_src / "foo.TXT").write_text("B", encoding="utf-8")
        (unsafe_src / "CON.txt").write_text("reserved", encoding="utf-8")
        (unsafe_src / "bad:name.txt").write_text("colon", encoding="utf-8")
        unsafe_repo = work / "unsafe.cldrepo"
        make_repo(unsafe_src, unsafe_repo, release_id="unsafe", release_seq=1, chunker="fixed", fixed_size="4KiB", force=True)
        rc_unsafe = release_check(unsafe_repo)
        assert rc_unsafe["ok"] is False and rc_unsafe["portable_ok"] is False
        assert any("collision" in e or "Windows" in e for e in rc_unsafe["errors"])


        src2 = work / "src_v2"
        shutil.copytree(src, src2)
        p = src2 / "data" / "bin.bin"
        data = p.read_bytes()
        p.write_bytes(data[:20000] + b"INSERTED_PATCH" * 100 + data[20000:])
        (src2 / "data" / "hello.txt").write_text((src2 / "data" / "hello.txt").read_text() + "patch\n", encoding="utf-8")
        repo2 = work / "repo_v2.cldrepo"
        make_repo(src2, repo2, release_id="selftest-v2", release_seq=2, chunker="cdc", chunk_min="4KiB", chunk_avg="16KiB", chunk_max="64KiB", expires_at="2035-01-01T00:00:00Z", force=True)
        patch_plan = work / "v1_to_v2.cldpatch.json"
        plan = create_patch_plan(repo, repo2, patch_plan)
        d = diff_repos(repo, repo2)
        assert d["added_chunks"] >= 1
        assert d["download_required_pack_bytes"] > 0
        assert plan["schema"] == "CoreLangDistribution/PatchPlan"
        assert plan["new"]["release_seq"] == 2

        # Alpha15 public-key trust/signature path.
        private_key = work / "trusted_private.json"
        public_key = work / "trusted_public.json"
        kg = keygen(private_key, pub_out=public_key)
        assert kg["ok"] is True and kg["algorithm"] == "ed25519" and public_key.exists()
        sg = sign_repo(repo2, private_key)
        assert sg["ok"] is True and sg["algorithm"] == "ed25519"
        vr = verify_repo(repo2, deep=True, trust_key=public_key)
        assert vr["ok"] is True and vr["signature"]["ok"] is True

        trusted_root = work / "trusted_root.json"
        rg = root_init(trusted_root, [public_key], root_id="selftest-root", expires_at="2035-01-01T00:00:00Z", min_release_seq=1)
        assert rg["ok"] is True and rg["trusted_key_count"] == 1
        tr = verify_repo(repo2, deep=True, trusted_root=trusted_root)
        assert tr["ok"] is True and tr["trusted_root"]["ok"] is True
        assert verify_repo_trust(repo2, trusted_root)["ok"] is True

        # Trusted root rejects unsigned releases, untrusted signing keys, expired roots, min-seq violations and expired releases.
        assert verify_repo(repo, trusted_root=trusted_root)["ok"] is False
        other_priv = work / "other_private.json"
        other_pub = work / "other_public.json"
        keygen(other_priv, pub_out=other_pub)
        wrong_root = work / "wrong_root.json"
        root_init(wrong_root, [other_pub], root_id="wrong-root", expires_at="2035-01-01T00:00:00Z", min_release_seq=1)
        assert verify_repo(repo2, trusted_root=wrong_root)["ok"] is False
        expired_root = work / "expired_root.json"
        root_init(expired_root, [public_key], root_id="expired-root", expires_at="2000-01-01T00:00:00Z", min_release_seq=1)
        assert verify_repo(repo2, trusted_root=expired_root)["ok"] is False
        future_min_root = work / "future_min_root.json"
        root_init(future_min_root, [public_key], root_id="future-min", expires_at="2035-01-01T00:00:00Z", min_release_seq=3)
        assert verify_repo(repo2, trusted_root=future_min_root)["ok"] is False
        expired_release = work / "expired_release.cldrepo"
        make_repo(src2, expired_release, release_id="expired", release_seq=4, chunker="cdc", chunk_min="4KiB", chunk_avg="16KiB", chunk_max="64KiB", expires_at="2000-01-01T00:00:00Z", force=True)
        sign_repo(expired_release, private_key)
        assert verify_repo(expired_release, trusted_root=trusted_root)["ok"] is False

        bad_repo = work / "repo_bad_sig.cldrepo"
        shutil.copytree(repo2, bad_repo)
        sig_doc = json.loads((bad_repo / "signatures.json").read_text(encoding="utf-8"))
        sig_doc["signature"] = "AA" + sig_doc["signature"][2:]
        (bad_repo / "signatures.json").write_text(json.dumps(sig_doc, indent=2), encoding="utf-8")
        assert verify_repo(bad_repo, trust_key=public_key)["ok"] is False

        corrupt_repo = work / "repo_corrupt.cldrepo"
        shutil.copytree(repo, corrupt_repo)
        pack = corrupt_repo / "packs" / "pack-000000.cldpak"
        b = bytearray(pack.read_bytes())
        if b:
            b[0] ^= 0xFF
            pack.write_bytes(bytes(b))
            assert verify_repo(corrupt_repo, deep=True)["ok"] is False

        # HTTP Range extraction/fetch plus alpha18 retry/ETag fault injection.
        port = free_port()
        srv = subprocess.Popen([PYTHON, str(CLI), "serve", str(repo), "--port", str(port)], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)
        try:
            wait_http(port)
            req = Request(f"http://127.0.0.1:{port}/packs/pack-000000.cldpak", headers={"Range": "bytes=0-9"})
            with urlopen(req, timeout=5) as r:
                assert r.status == 206
                assert len(r.read()) == 10
            out_http = work / "http_bin.bin"
            extract_file(f"http://127.0.0.1:{port}/", "data/bin.bin", out_http)
            assert sha(out_http) == sha(src / "data" / "bin.bin")
            install = work / "installed"
            cache = work / "cache"
            fr = fetch_install(f"http://127.0.0.1:{port}/", install, cache_dir=cache)
            assert fr["ok"] is True
            assert sha(install / "data" / "bin.bin") == sha(src / "data" / "bin.bin")

            # Alpha15: parallel prefetch path uses multiple chunk workers, populates cache,
            # and still installs byte-identical files. Bandwidth limit is set very high
            # so the test exercises accounting without becoming slow.
            par_install = work / "parallel_install"
            par_fetch = fetch_install(
                f"http://127.0.0.1:{port}/",
                par_install,
                cache_dir=work / "parallel_cache",
                parallel=4,
                bandwidth_limit_bps="100MiB",
            )
            assert par_fetch["ok"] is True
            assert par_fetch["parallel_fetch"]["enabled"] is True
            assert par_fetch["parallel_fetch"]["workers"] == 4
            assert par_fetch["parallel_fetch"]["prefetched_chunks"] >= 1
            assert par_fetch["downloaded_chunks"] >= par_fetch["parallel_fetch"]["prefetched_chunks"]
            assert par_fetch["network"]["parallel_workers"] == 4
            assert par_fetch["network"]["bandwidth_limit_bps"] == 104857600
            assert par_fetch["network"].get("token_bucket", {}).get("rate_bps") == 104857600
            assert sha(par_install / "data" / "bin.bin") == sha(src / "data" / "bin.bin")
        finally:
            srv.terminate()
            try:
                srv.wait(timeout=3)
            except subprocess.TimeoutExpired:
                srv.kill()

        # Alpha15 mirror fallback: metadata comes from a primary whose pack is corrupt,
        # while the actual chunk is recovered from a healthy mirror.
        primary_port = free_port()
        mirror_port = free_port()
        primary_srv = subprocess.Popen([PYTHON, str(CLI), "serve", str(corrupt_repo), "--port", str(primary_port)], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)
        mirror_srv = subprocess.Popen([PYTHON, str(CLI), "serve", str(repo), "--port", str(mirror_port)], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)
        try:
            wait_http(primary_port)
            wait_http(mirror_port)
            mirror_install = work / "mirror_install"
            mirror_state = work / "mirror_state.json"
            mirror_fetch = fetch_install(
                f"http://127.0.0.1:{primary_port}/",
                mirror_install,
                cache_dir=work / "mirror_cache",
                mirrors=[f"http://127.0.0.1:{mirror_port}/"],
                http_retries=1,
                http_backoff=0.01,
                mirror_blacklist_threshold=1,
                mirror_blacklist_seconds=60,
                mirror_state_file=mirror_state,
            )
            assert mirror_fetch["ok"] is True
            assert sha(mirror_install / "data" / "bin.bin") == sha(src / "data" / "bin.bin")
            mirrors_stats = mirror_fetch["network"].get("mirrors", {})
            assert any(v.get("failures", 0) >= 1 for v in mirrors_stats.values()), mirrors_stats
            assert any(v.get("successes", 0) >= 1 for v in mirrors_stats.values()), mirrors_stats
            assert mirror_fetch["network"].get("mirror_blacklist_events", 0) >= 1, mirror_fetch["network"]
            assert mirror_state.exists()
            mirror_doc = json.loads(mirror_state.read_text(encoding="utf-8"))
            assert mirror_doc["schema"] == "CoreLangDistribution/MirrorState"
            assert mirror_doc.get("mirrors")
            mirror_out = work / "mirror_extract_bin.bin"
            extract_mirror = extract_file(
                f"http://127.0.0.1:{primary_port}/",
                "data/bin.bin",
                mirror_out,
                mirrors=[f"http://127.0.0.1:{mirror_port}/"],
                http_retries=1,
                http_backoff=0.01,
            )
            assert extract_mirror["verified"] is True
            assert sha(mirror_out) == sha(src / "data" / "bin.bin")
        finally:
            for proc in (primary_srv, mirror_srv):
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()

        # Alpha15: retry/backoff survives injected HTTP 503 failures and truncated range bodies.
        port = free_port()
        srv = subprocess.Popen([PYTHON, str(CLI), "serve", str(repo), "--port", str(port), "--fail-every", "6", "--fail-status", "503"], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)
        try:
            wait_http(port)
            retry_install = work / "retry_install"
            retry_fetch = fetch_install(f"http://127.0.0.1:{port}/", retry_install, cache_dir=work / "retry_cache", http_retries=5, http_backoff=0.01)
            assert retry_fetch["ok"] is True
            assert retry_fetch["network"]["range_retries"] >= 1, retry_fetch["network"]
            assert retry_fetch["network"]["if_range_used"] >= 1, retry_fetch["network"]
            assert sha(retry_install / "data" / "bin.bin") == sha(src / "data" / "bin.bin")
        finally:
            srv.terminate()
            try:
                srv.wait(timeout=3)
            except subprocess.TimeoutExpired:
                srv.kill()

        port = free_port()
        srv = subprocess.Popen([PYTHON, str(CLI), "serve", str(repo), "--port", str(port), "--truncate-every", "6"], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)
        try:
            wait_http(port)
            trunc_install = work / "truncate_install"
            trunc_fetch = fetch_install(f"http://127.0.0.1:{port}/", trunc_install, cache_dir=work / "truncate_cache", http_retries=5, http_backoff=0.01)
            assert trunc_fetch["ok"] is True
            assert trunc_fetch["network"]["range_retries"] >= 1, trunc_fetch["network"]
            assert sha(trunc_install / "data" / "hello.txt") == sha(src / "data" / "hello.txt")
        finally:
            srv.terminate()
            try:
                srv.wait(timeout=3)
            except subprocess.TimeoutExpired:
                srv.kill()

        # Alpha15 trusted-root mirror allow-list: when allowed_mirrors is present,
        # HTTP repo/mirror bases must match it exactly.
        signed_port = free_port()
        signed_srv = subprocess.Popen([PYTHON, str(CLI), "serve", str(repo2), "--port", str(signed_port)], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)
        try:
            wait_http(signed_port)
            allowed_url = f"http://127.0.0.1:{signed_port}/"
            net_root = work / "network_allowed_root.json"
            root_init(net_root, [public_key], root_id="network-root", expires_at="2035-01-01T00:00:00Z", min_release_seq=1, mirror_urls=[allowed_url])
            net_install = work / "network_trusted_install"
            net_fetch = fetch_install(allowed_url, net_install, cache_dir=work / "network_trusted_cache", trusted_root=net_root)
            assert net_fetch["ok"] is True
            assert net_fetch["trusted_root_sources"]["enforced"] is True
            assert sha(net_install / "data" / "bin.bin") == sha(src2 / "data" / "bin.bin")
            blocked_root = work / "network_blocked_root.json"
            root_init(blocked_root, [public_key], root_id="network-blocked-root", expires_at="2035-01-01T00:00:00Z", min_release_seq=1, mirror_urls=["http://127.0.0.1:1/"])
            expect_error(lambda: fetch_install(allowed_url, work / "blocked_network_install", cache_dir=work / "blocked_network_cache", trusted_root=blocked_root), "trusted-root mirror policy failed")
        finally:
            signed_srv.terminate()
            try:
                signed_srv.wait(timeout=3)
            except subprocess.TimeoutExpired:
                signed_srv.kill()

        # Patch-plan enforcement, trust, rollback and resume.
        expect_error(lambda: fetch_install(repo2, work / "empty_patch_install", patch_plan=patch_plan), "patch plan requires an existing installation")
        bad_plan_root = work / "bad_plan_root.cldpatch.json"
        bad_doc = json.loads(patch_plan.read_text(encoding="utf-8"))
        bad_doc["new"]["root_hash"] = "00" * 32
        bad_plan_root.write_text(json.dumps(bad_doc, indent=2), encoding="utf-8")
        expect_error(lambda: fetch_install(repo2, install, from_installed=install, patch_plan=bad_plan_root), "patch plan target root_hash")
        bad_plan_chunk = work / "bad_plan_chunk.cldpatch.json"
        bad_doc = json.loads(patch_plan.read_text(encoding="utf-8"))
        if bad_doc.get("chunks"):
            bad_doc["chunks"][0]["raw_len"] = int(bad_doc["chunks"][0]["raw_len"]) + 1
            bad_plan_chunk.write_text(json.dumps(bad_doc, indent=2), encoding="utf-8")
            expect_error(lambda: fetch_install(repo2, install, from_installed=install, patch_plan=bad_plan_chunk), "patch plan chunk metadata mismatch")

        upd = fetch_install(repo2, install, from_installed=install, cache_dir=work / "cache_update", patch_plan=patch_plan, trusted_root=trusted_root)
        assert upd["ok"] is True and upd["patch_plan"]["used"] is True and upd["signature_checked"] is True and upd["trusted_root_checked"] is True
        assert sha(install / "data" / "bin.bin") == sha(src2 / "data" / "bin.bin")
        assert sha(install / "data" / "hello.txt") == sha(src2 / "data" / "hello.txt")
        im = json.loads((install / ".cld2" / "installed.json").read_text(encoding="utf-8"))
        assert im["release_seq"] == 2 and im["release_id"] == "selftest-v2"
        expect_error(lambda: fetch_install(repo, install), "refusing rollback")
        fetch_install(repo, install, allow_downgrade=True)
        assert json.loads((install / ".cld2" / "installed.json").read_text(encoding="utf-8"))["release_seq"] == 1

        resume_install = work / "resume_install"
        resume_cache = work / "resume_cache"
        expect_error(lambda: fetch_install(repo2, resume_install, cache_dir=resume_cache, fail_after_chunks=1), "simulated interruption")
        assert any(resume_cache.iterdir())
        resumed = fetch_install(repo2, resume_install, cache_dir=resume_cache)
        assert resumed["ok"] is True and resumed["resumed_from_cache"] is True and resumed["cache_hit_chunks"] >= 1
        assert resumed["cache_index"]["after"]["chunk_count"] >= 1
        assert sha(resume_install / "data" / "bin.bin") == sha(src2 / "data" / "bin.bin")

        # Alpha15: cache index/GC plus install audit and atomic repair.
        cache_index = rebuild_cache_index(resume_cache)
        assert cache_index["ok"] is True and cache_index["chunk_count"] >= 1
        audit_ok = audit_install(repo2, resume_install)
        assert audit_ok["ok"] is True and audit_ok["files_ok"] == len(json.loads((repo2 / "files.idx.json").read_text(encoding="utf-8")))
        (resume_install / "data" / "hello.txt").write_text("broken", encoding="utf-8")
        (resume_install / "data" / "bin.bin").unlink()
        audit_bad = audit_install(repo2, resume_install)
        assert audit_bad["ok"] is False and audit_bad["corrupt_count"] >= 1 and audit_bad["missing_count"] >= 1
        repair = repair_install(repo2, resume_install, cache_dir=resume_cache, parallel=2)
        assert repair["ok"] is True and repair["repaired"] is True
        assert audit_install(repo2, resume_install)["ok"] is True
        gc = cache_gc(resume_cache, max_bytes=1)
        assert gc["ok"] is True and gc["removed_chunks"] >= 1

        bdir = work / "bench"
        br = run_bench(bdir, chunker="cdc")
        assert br["runs"][0]["diff"]["download_required_pack_bytes"] >= 0
        assert (bdir / "bench_summary.csv").exists()
        assert (bdir / "bench_report.md").exists()

        print(json.dumps({"ok": True, "work": str(work), "diff": d, "patch_plan": {"chunks": plan["chunk_count"], "bytes": plan["download_required_pack_bytes"]}, "bench": br}, indent=2, sort_keys=True))
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

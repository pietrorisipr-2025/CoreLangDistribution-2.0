from __future__ import annotations
import shlex
import hashlib
import os
import subprocess

import csv
import json
import math
import random
import shutil
import tarfile
import threading
import time
from datetime import datetime, timezone
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from time import perf_counter
from typing import Dict, Iterable, Tuple

from .chunker import parse_size
from .hashutil import sha256_file
from .profiles import profile_metadata, profile_pack_options
from .repo import audit_install, diff_repos, fetch_install, inspect_repo, make_repo, verify_repo
from .server import RangeRequestHandler

BENCH_SCENARIOS = [
    "game-patch-small",
    "game-patch-insert",
    "game-realistic",
    "dataset-model",
    "media-catalog",
    "media-catalog-large",
    "random-worstcase",
]

BENCH_PROFILES: dict[str, dict[str, object]] = {
    # Fast CI/smoke profile. Keeps artifacts small enough to run frequently.
    "quick": {
        "files": 16,
        "file_size": "512KiB",
        "change_ratio": None,
        "chunk_min": "32KiB",
        "chunk_avg": "128KiB",
        "chunk_max": "512KiB",
        "fixed_size": "128KiB",
    },
    # More realistic local development profile. Still safe for laptops/CI.
    "medium": {
        "files": 32,
        "file_size": "1MiB",
        "change_ratio": None,
        "chunk_min": "64KiB",
        "chunk_avg": "256KiB",
        "chunk_max": "1MiB",
        "fixed_size": "256KiB",
    },
    # Heavier synthetic game/assets profile. Intended for user-visible benchmark reports.
    "game-assets": {
        "files": 48,
        "file_size": "1MiB",
        "change_ratio": 0.10,
        "chunk_min": "64KiB",
        "chunk_avg": "256KiB",
        "chunk_max": "1MiB",
        "fixed_size": "256KiB",
    },
    # Larger shard-style corpus without becoming enormous in this environment.
    "dataset": {
        "files": 18,
        "file_size": "2MiB",
        "change_ratio": 0.22,
        "chunk_min": "128KiB",
        "chunk_avg": "512KiB",
        "chunk_max": "2MiB",
        "fixed_size": "512KiB",
    },
    # Media catalog profile: mostly immutable media with metadata/subtitle changes.
    "media": {
        "files": 24,
        "file_size": "768KiB",
        "change_ratio": 0.25,
        "chunk_min": "64KiB",
        "chunk_avg": "256KiB",
        "chunk_max": "1MiB",
        "fixed_size": "256KiB",
    },
    # Alpha20 default/recommended profile for huge single files or large binary shards.
    # Alpha20 FITS tuning selected this as the best practical transfer/time compromise.
    "large-file": {
        "files": 1,
        "file_size": "128MiB",
        "change_ratio": 1.0,
        "chunk_min": "128KiB",
        "chunk_avg": "512KiB",
        "chunk_max": "2MiB",
        "fixed_size": "1MiB",
        "fastcdc_stride": 8,
    },
    # Alpha20 tuning profiles for real large-file benchmarks.
    # Smaller chunks usually improve transfer reuse after local changes but cost more index/pack time.
    "large-file-small": {
        "files": 1,
        "file_size": "128MiB",
        "change_ratio": 1.0,
        "chunk_min": "64KiB",
        "chunk_avg": "256KiB",
        "chunk_max": "1MiB",
        "fixed_size": "1MiB",
        "fastcdc_stride": 4,
    },
    "large-file-balanced": {
        "files": 1,
        "file_size": "128MiB",
        "change_ratio": 1.0,
        "chunk_min": "128KiB",
        "chunk_avg": "512KiB",
        "chunk_max": "2MiB",
        "fixed_size": "1MiB",
        "fastcdc_stride": 8,
    },
    "large-file-large": {
        "files": 1,
        "file_size": "128MiB",
        "change_ratio": 1.0,
        "chunk_min": "256KiB",
        "chunk_avg": "1MiB",
        "chunk_max": "4MiB",
        "fixed_size": "1MiB",
        "fastcdc_stride": 16,
    },
}


def _rand_bytes(size: int, seed: int) -> bytes:
    rng = random.Random(seed)
    out = bytearray()
    while len(out) < size:
        out.extend(rng.randbytes(min(65536, size - len(out))))
    return bytes(out)


def _write_repeating_asset(path: Path, size: int, seed: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    patterns = [bytes(f"asset_key_{i:03d}=", "ascii") + rng.randbytes(32) for i in range(64)]
    with path.open("wb") as f:
        remaining = size
        while remaining > 0:
            pat = rng.choice(patterns)
            block = (pat * (8192 // len(pat) + 1))[: min(8192, remaining)]
            f.write(block)
            remaining -= len(block)


def _write_binary_asset(path: Path, size: int, seed: int, compressible: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if compressible:
        _write_repeating_asset(path, size, seed)
    else:
        path.write_bytes(_rand_bytes(size, seed))


def _tree_size(root: Path) -> int:
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())


def _manifest_single(p: Path) -> str:
    # Alpha20: stream-hash files so real-pair baselines do not load 10+ GiB files in RAM.
    return sha256_file(p, chunk_size=8 * 1024 * 1024)


def _manifest(root: Path) -> Dict[str, str]:
    m: Dict[str, str] = {}
    for p in sorted(x for x in root.rglob("*") if x.is_file()):
        rel = p.relative_to(root).as_posix()
        m[rel] = _manifest_single(p)
    return m


def _changed_files(v1: Path, v2: Path) -> list[Path]:
    old = _manifest(v1)
    changed: list[Path] = []
    for p in sorted(x for x in v2.rglob("*") if x.is_file()):
        rel = p.relative_to(v2).as_posix()
        if old.get(rel) != _manifest_single(p):
            changed.append(p)
    return changed


def _changed_file_raw_bytes(v1: Path, v2: Path) -> int:
    return sum(p.stat().st_size for p in _changed_files(v1, v2))


def _make_tar_gz_size(root: Path, out_path: Path) -> int:
    with tarfile.open(out_path, "w:gz") as tar:
        tar.add(root, arcname=root.name)
    return out_path.stat().st_size


def _make_tar_from_files(base: Path, files: Iterable[Path], out_tar: Path) -> None:
    with tarfile.open(out_tar, "w") as tar:
        for p in files:
            tar.add(p, arcname=p.relative_to(base).as_posix())


def _make_changed_files_tar_gz_size(v1: Path, v2: Path, out_path: Path) -> int:
    files = _changed_files(v1, v2)
    with tarfile.open(out_path, "w:gz") as tar:
        for p in files:
            tar.add(p, arcname=p.relative_to(v2).as_posix())
    return out_path.stat().st_size


def _zstd_compress_file(src: Path, dst: Path, level: int = 6) -> int | None:
    try:
        import zstandard as zstd  # type: ignore
    except Exception:
        return None
    cctx = zstd.ZstdCompressor(level=level)
    with src.open("rb") as fsrc, dst.open("wb") as fdst:
        cctx.copy_stream(fsrc, fdst, read_size=8 * 1024 * 1024, write_size=8 * 1024 * 1024)
    return dst.stat().st_size


def _make_tar_zstd_size(root: Path, out_path: Path) -> int | None:
    tmp_tar = out_path.with_suffix(".tar")
    with tarfile.open(tmp_tar, "w") as tar:
        tar.add(root, arcname=root.name)
    try:
        return _zstd_compress_file(tmp_tar, out_path, level=6)
    finally:
        tmp_tar.unlink(missing_ok=True)


def _make_changed_files_tar_zstd_size(v1: Path, v2: Path, out_path: Path) -> int | None:
    tmp_tar = out_path.with_suffix(".tar")
    _make_tar_from_files(v2, _changed_files(v1, v2), tmp_tar)
    try:
        return _zstd_compress_file(tmp_tar, out_path, level=6)
    finally:
        tmp_tar.unlink(missing_ok=True)


def _baseline_sizes(v1: Path, v2: Path, out: Path, *, skip_heavy_baselines: bool = False) -> dict:
    baseline = {
        "v1_logical_bytes": _tree_size(v1),
        "v2_logical_bytes": _tree_size(v2),
        "file_level_raw_download_bytes": _changed_file_raw_bytes(v1, v2),
        "heavy_baselines_skipped": bool(skip_heavy_baselines),
    }
    if skip_heavy_baselines:
        baseline.update({
            "file_level_tar_gz_bytes": None,
            "file_level_tar_zstd_bytes": None,
            "full_tar_gz_v2_bytes": None,
            "full_tar_zstd_v2_bytes": None,
        })
    else:
        baseline.update({
            "file_level_tar_gz_bytes": _make_changed_files_tar_gz_size(v1, v2, out / "changed_files.tar.gz"),
            "file_level_tar_zstd_bytes": _make_changed_files_tar_zstd_size(v1, v2, out / "changed_files.tar.zst"),
            "full_tar_gz_v2_bytes": _make_tar_gz_size(v2, out / "v2_full.tar.gz"),
            "full_tar_zstd_v2_bytes": _make_tar_zstd_size(v2, out / "v2_full.tar.zst"),
        })
    return baseline


def _real_pair_metadata(v1: Path, v2: Path) -> dict:
    old_files = {p.relative_to(v1).as_posix(): p for p in v1.rglob("*") if p.is_file()}
    new_files = {p.relative_to(v2).as_posix(): p for p in v2.rglob("*") if p.is_file()}
    common = sorted(set(old_files) & set(new_files))
    size_changed = []
    for rel in common:
        s1 = old_files[rel].stat().st_size
        s2 = new_files[rel].stat().st_size
        if s1 != s2:
            size_changed.append({"path": rel, "old_size": s1, "new_size": s2, "size_delta": s2 - s1})
    return {
        "old_file_count": len(old_files),
        "new_file_count": len(new_files),
        "common_file_count": len(common),
        "added_file_count": len(set(new_files) - set(old_files)),
        "removed_file_count": len(set(old_files) - set(new_files)),
        "size_changed_file_count": len(size_changed),
        "size_changed_sample": size_changed[:20],
        "note": "Alpha18 metadata records size-level changes. Exact byte-level delta requires an explicit scenario generator or external diff."
    }


def _patch_insert_file(path: Path, seed: int, insert_size: int) -> None:
    data = path.read_bytes()
    mid = len(data) // 2
    rng = random.Random(seed)
    insert = (b"CLD2_PATCH_INSERT" + rng.randbytes(32)) * max(1, insert_size // 48)
    path.write_bytes(data[:mid] + insert[:insert_size] + data[mid:])


def make_demo_pair(
    root: Path,
    *,
    scenario: str = "game-patch-insert",
    files: int | None = None,
    file_size: int | str | None = None,
    change_ratio: float | None = None,
) -> Tuple[Path, Path]:
    v1 = root / "v1"
    v2 = root / "v2"
    v1.mkdir(parents=True)

    size_i = parse_size(file_size or 0) if file_size else None

    if scenario == "game-patch-small":
        files = files or 16
        file_size_i = size_i or 512 * 1024
        change_ratio = 0.10 if change_ratio is None else change_ratio
        insert_size = 2048
    elif scenario in ("game-patch-insert", "game-realistic"):
        files = files or (32 if scenario == "game-realistic" else 16)
        file_size_i = size_i or (1024 * 1024)
        change_ratio = (0.10 if scenario == "game-realistic" else 0.25) if change_ratio is None else change_ratio
        insert_size = 8192
    elif scenario == "dataset-model":
        files = files or 18
        file_size_i = size_i or 2 * 1024 * 1024
        change_ratio = 0.22 if change_ratio is None else change_ratio
        insert_size = 16 * 1024
    elif scenario in ("media-catalog", "media-catalog-large"):
        files = files or (24 if scenario == "media-catalog-large" else 10)
        file_size_i = size_i or (768 * 1024 if scenario == "media-catalog-large" else 1024 * 1024)
        change_ratio = 0.25 if change_ratio is None else change_ratio
        insert_size = 1024
    elif scenario == "random-worstcase":
        files = files or 8
        file_size_i = size_i or 512 * 1024
        change_ratio = 1.0 if change_ratio is None else change_ratio
        insert_size = 0
    else:
        raise ValueError(f"unknown benchmark scenario: {scenario}")

    for i in range(files):
        if scenario in ("media-catalog", "media-catalog-large"):
            media_dir = v1 / "media"
            meta_dir = v1 / "metadata"
            subtitle_dir = v1 / "subtitles"
            _write_binary_asset(media_dir / f"video_{i:04d}.mp4", file_size_i, seed=1000 + i, compressible=False)
            meta = {"id": i, "title": f"Video {i}", "langs": ["it", "en"], "rev": 1, "tags": ["cld2", "demo", "catalog"]}
            (meta_dir / f"video_{i:04d}.json").parent.mkdir(parents=True, exist_ok=True)
            (meta_dir / f"video_{i:04d}.json").write_text(json.dumps(meta, sort_keys=True) * 80, encoding="utf-8")
            (subtitle_dir / f"video_{i:04d}.srt").parent.mkdir(parents=True, exist_ok=True)
            (subtitle_dir / f"video_{i:04d}.srt").write_text((f"{i}\n00:00:01,000 --> 00:00:03,000\nCLD2 subtitle line {i}\n\n") * 100, encoding="utf-8")
        elif scenario == "dataset-model":
            if i % 6 == 0:
                cfg = {"model": "demo", "shard": i, "layers": 32, "precision": "fp16", "rev": 1}
                (v1 / "config" / f"shard_{i:04d}.json").parent.mkdir(parents=True, exist_ok=True)
                (v1 / "config" / f"shard_{i:04d}.json").write_text(json.dumps(cfg, sort_keys=True) * 200, encoding="utf-8")
            else:
                _write_binary_asset(v1 / "shards" / f"model-{i:04d}.safetensors", file_size_i, seed=2000 + i, compressible=False)
        else:
            sub = v1 / ("assets" if i % 4 else "metadata")
            if i % 4 == 0:
                sub.mkdir(parents=True, exist_ok=True)
                data = {"asset": i, "name": f"asset_{i}", "tags": ["demo", "game", "cld2"], "version": 1}
                (sub / f"asset_{i:04d}.json").write_text(json.dumps(data, sort_keys=True) * 100, encoding="utf-8")
            else:
                _write_binary_asset(sub / f"asset_{i:04d}.bin", file_size_i, seed=i, compressible=False)

    shutil.copytree(v1, v2)
    changed = max(1, int(files * float(change_ratio)))
    for i in range(changed):
        if scenario in ("media-catalog", "media-catalog-large"):
            target = v2 / "metadata" / f"video_{i:04d}.json"
            target.write_text(target.read_text(encoding="utf-8") + f"\n{{\"patch\": {i}, \"rights_rev\": 2}}\n", encoding="utf-8")
            if i % 3 == 0:
                sub = v2 / "subtitles" / f"video_{i:04d}.srt"
                sub.write_text(sub.read_text(encoding="utf-8") + f"\n{i+1000}\n00:01:00,000 --> 00:01:02,000\nPatched subtitle {i}\n\n", encoding="utf-8")
        elif scenario == "dataset-model":
            if i % 6 == 0:
                target = v2 / "config" / f"shard_{i:04d}.json"
                target.write_text(target.read_text(encoding="utf-8") + f"\n{{\"dataset_patch\": {i}}}\n", encoding="utf-8")
            else:
                target = v2 / "shards" / f"model-{i:04d}.safetensors"
                _patch_insert_file(target, seed=9000 + i, insert_size=insert_size)
        elif scenario == "random-worstcase":
            target = v2 / ("assets" if i % 4 else "metadata") / (f"asset_{i:04d}.bin" if i % 4 else f"asset_{i:04d}.json")
            if target.suffix == ".json":
                target.write_text(json.dumps({"asset": i, "randomized": True, "blob": _rand_bytes(4096, i).hex()}), encoding="utf-8")
            else:
                target.write_bytes(_rand_bytes(target.stat().st_size, seed=9000 + i))
        else:
            target = v2 / ("assets" if i % 4 else "metadata") / (f"asset_{i:04d}.bin" if i % 4 else f"asset_{i:04d}.json")
            if target.suffix == ".json":
                target.write_text(target.read_text(encoding="utf-8") + "\n{\"patch\":true}\n", encoding="utf-8")
            else:
                _patch_insert_file(target, seed=8000 + i, insert_size=insert_size)
    return v1, v2


def _profile_values(profile: str, *, files: int | None = None, file_size: int | str | None = None, change_ratio: float | None = None) -> dict[str, object]:
    if profile not in BENCH_PROFILES:
        raise ValueError(f"unknown benchmark profile: {profile}")
    p = dict(BENCH_PROFILES[profile])
    if files is not None:
        p["files"] = int(files)
    if file_size is not None:
        p["file_size"] = file_size
    if change_ratio is not None:
        p["change_ratio"] = float(change_ratio)
    return p




def _is_large_file_profile(profile: str) -> bool:
    return str(profile).startswith("large-file")


def _modes_for_profile(profile: str, chunker: str) -> list[str]:
    if chunker == "both":
        return ["fixed", "fastcdc"] if _is_large_file_profile(profile) else ["fixed", "cdc"]
    return [chunker]

def _run_one(out: Path, v1: Path, v2: Path, mode: str, *, profile_opts: dict[str, object], codec: str = "auto", label: str | None = None) -> dict:
    safe_label = (label or mode).replace(":", "_").replace("/", "_").replace("\\", "_")
    repo1 = out / f"repo_v1_{safe_label}.cldrepo"
    repo2 = out / f"repo_v2_{safe_label}.cldrepo"
    t0 = perf_counter()
    r1 = make_repo(
        v1,
        repo1,
        release_id=f"demo-v1-{mode}",
        release_seq=1,
        chunker=mode,
        fixed_size=profile_opts["fixed_size"],
        chunk_min=profile_opts["chunk_min"],
        chunk_avg=profile_opts["chunk_avg"],
        chunk_max=profile_opts["chunk_max"],
        fastcdc_stride=int(profile_opts.get("fastcdc_stride", 16)),
        codec=codec,
        force=True,
    )
    t1 = perf_counter()
    r2 = make_repo(
        v2,
        repo2,
        release_id=f"demo-v2-{mode}",
        release_seq=2,
        chunker=mode,
        fixed_size=profile_opts["fixed_size"],
        chunk_min=profile_opts["chunk_min"],
        chunk_avg=profile_opts["chunk_avg"],
        chunk_max=profile_opts["chunk_max"],
        fastcdc_stride=int(profile_opts.get("fastcdc_stride", 16)),
        codec=codec,
        force=True,
    )
    t2 = perf_counter()
    d = diff_repos(repo1, repo2)
    return {
        "chunker": mode,
        "method_label": label or mode,
        "profile_name": label or mode,
        "profile_options": profile_opts,
        "fastcdc_stride": int(profile_opts.get("fastcdc_stride", 16)),
        "pack_v1_seconds": round(t1 - t0, 4),
        "pack_v2_seconds": round(t2 - t1, 4),
        "v1_metrics": r1["metrics"],
        "v2_metrics": r2["metrics"],
        "diff": d,
    }


def _summary_rows(result: dict) -> list[dict]:
    rows = []
    baseline = result["baseline"]
    for r in result["runs"]:
        d = r["diff"]
        rows.append({
            "scenario": result["scenario"],
            "profile": result.get("profile", ""),
            "method": r.get("method_label") or r["chunker"],
            "chunker": r["chunker"],
            "method_profile": r.get("profile_name") or r.get("method_label") or result.get("profile", ""),
            "fastcdc_stride": r.get("fastcdc_stride"),
            "v2_logical_bytes": baseline["v2_logical_bytes"],
            "full_tar_gz_v2_bytes": baseline.get("full_tar_gz_v2_bytes"),
            "full_tar_zstd_v2_bytes": baseline.get("full_tar_zstd_v2_bytes"),
            "file_level_raw_download_bytes": baseline.get("file_level_raw_download_bytes"),
            "file_level_tar_gz_bytes": baseline.get("file_level_tar_gz_bytes"),
            "file_level_tar_zstd_bytes": baseline.get("file_level_tar_zstd_bytes"),
            "download_required_pack_bytes": d["download_required_pack_bytes"],
            "download_required_raw_bytes": d["download_required_raw_bytes"],
            "chunk_reuse_ratio": d["chunk_reuse_ratio"],
            "saved_ratio_vs_file_level_raw": d["estimated_saved_ratio_vs_file_level_raw"],
            "saved_ratio_vs_file_level_tar_zstd": (round(max(0, (baseline.get("file_level_tar_zstd_bytes") or 0) - d["download_required_pack_bytes"]) / baseline.get("file_level_tar_zstd_bytes"), 6) if baseline.get("file_level_tar_zstd_bytes") else None),
            "saved_ratio_vs_full_tar_zstd": (round(max(0, (baseline.get("full_tar_zstd_v2_bytes") or 0) - d["download_required_pack_bytes"]) / baseline.get("full_tar_zstd_v2_bytes"), 6) if baseline.get("full_tar_zstd_v2_bytes") else None),
            "reused_chunks": d.get("reused_chunks"),
            "added_chunks": d.get("added_chunks"),
            "removed_chunks": d.get("removed_chunks"),
            "pack_v1_seconds": r["pack_v1_seconds"],
            "pack_v2_seconds": r["pack_v2_seconds"],
            "pack_total_seconds": round(float(r["pack_v1_seconds"]) + float(r["pack_v2_seconds"]), 4),
        })
    return rows




def _gib(n: int | float) -> float:
    return float(n) / (1024.0 ** 3)


def _egress_cost(bytes_count: int | float, *, download_count: int = 1, cost_per_gb: float = 0.05) -> float:
    # Cost uses GiB to keep the report conservative and explicit.
    return round(_gib(bytes_count) * max(0, int(download_count)) * float(cost_per_gb), 6)


def _method_key(run: dict) -> str:
    return str(run.get("method_label") or run.get("chunker") or "unknown")


BEST_TRANSFER_SELECTION_RULE = "minimum download_required_pack_bytes; ties broken by lower total pack time then method name"


def _run_pack_total_seconds(run: dict) -> float:
    return float(run.get("pack_v1_seconds") or 0) + float(run.get("pack_v2_seconds") or 0)


def _run_download_pack_bytes(run: dict) -> int:
    return int(run.get("diff", {}).get("download_required_pack_bytes") or 0)


def best_transfer_comparison(runs: list[dict]) -> dict:
    if not runs:
        return {}
    best = min(runs, key=lambda r: (_run_download_pack_bytes(r), _run_pack_total_seconds(r), str(r.get("chunker") or _method_key(r))))
    fixed = next((r for r in runs if str(r.get("chunker")) == "fixed"), None)
    best_bytes = _run_download_pack_bytes(best)
    fixed_bytes = _run_download_pack_bytes(fixed) if fixed else None
    saved_vs_fixed = max(0, int(fixed_bytes) - best_bytes) if fixed_bytes is not None else None
    return {
        "best_transfer_method": str(best.get("chunker") or _method_key(best)),
        "best_transfer_bytes": best_bytes,
        "fixed_download_bytes": fixed_bytes,
        "best_saved_vs_fixed_bytes": saved_vs_fixed,
        "best_saved_ratio_vs_fixed": round((saved_vs_fixed or 0) / fixed_bytes, 6) if fixed_bytes else 0.0,
        "selection_rule": BEST_TRANSFER_SELECTION_RULE,
    }


def _fmt_cost(value: object, currency: str) -> str:
    try:
        v = float(value or 0)
    except Exception:
        v = 0.0
    if abs(v) < 0.000001:
        text = "0"
    elif abs(v) < 1:
        text = f"{v:.6f}"
    else:
        text = f"{v:.3f}"
    return f"{text} {currency}"


def _apply_external_baselines(baseline: dict, *, file_level_tar_zstd_bytes: int | None = None, full_tar_zstd_v2_bytes: int | None = None, note: str | None = None) -> dict:
    used = {}
    if file_level_tar_zstd_bytes is not None:
        baseline["file_level_tar_zstd_bytes"] = int(file_level_tar_zstd_bytes)
        used["file_level_tar_zstd_bytes"] = int(file_level_tar_zstd_bytes)
    if full_tar_zstd_v2_bytes is not None:
        baseline["full_tar_zstd_v2_bytes"] = int(full_tar_zstd_v2_bytes)
        used["full_tar_zstd_v2_bytes"] = int(full_tar_zstd_v2_bytes)
    if used:
        baseline.setdefault("external_baselines", {}).update(used)
        baseline["external_baseline_note"] = note or "External heavy baseline values supplied by the user; tar.gz/tar.zst were not regenerated in this run."
    return baseline


def add_cost_projection(result: dict, *, download_count: int = 1, cost_per_gb: float = 0.05, currency: str = "USD") -> dict:
    """Attach a simple business-facing egress estimate to a benchmark result.

    This is intentionally not a promise of real CDN billing. It is a transparent
    byte * downloads * cost/GiB projection that lets users compare strategies.
    """
    baseline = result.get("baseline", {})
    projection: dict[str, object] = {
        "download_count": int(download_count),
        "cost_per_gib": float(cost_per_gb),
        "currency": currency,
        "note": "Estimated egress only: bytes * downloads * cost_per_GiB. It excludes storage, requests, cache hit rates, taxes, and CDN tiering.",
        "baselines": {},
        "methods": {},
    }
    for key in [
        "v2_logical_bytes",
        "full_tar_gz_v2_bytes",
        "full_tar_zstd_v2_bytes",
        "file_level_raw_download_bytes",
        "file_level_tar_gz_bytes",
        "file_level_tar_zstd_bytes",
    ]:
        val = baseline.get(key)
        if val is not None:
            projection["baselines"][key] = {
                "bytes": val,
                "gib_per_download": round(_gib(val), 6),
                "estimated_cost": _egress_cost(val, download_count=download_count, cost_per_gb=cost_per_gb),
            }
    file_raw = float(baseline.get("file_level_raw_download_bytes") or 0)
    file_tzst = float(baseline.get("file_level_tar_zstd_bytes") or 0)
    full_zstd = float(baseline.get("full_tar_zstd_v2_bytes") or 0)
    for r in result.get("runs", []):
        method = _method_key(r)
        dl = int(r.get("diff", {}).get("download_required_pack_bytes") or 0)
        method_row = {
            "bytes": dl,
            "gib_per_download": round(_gib(dl), 6),
            "estimated_cost": _egress_cost(dl, download_count=download_count, cost_per_gb=cost_per_gb),
        }
        if file_raw:
            method_row["saved_vs_file_level_raw_bytes"] = max(0, int(file_raw) - dl)
            method_row["saved_vs_file_level_raw_cost"] = round(_egress_cost(file_raw, download_count=download_count, cost_per_gb=cost_per_gb) - _egress_cost(dl, download_count=download_count, cost_per_gb=cost_per_gb), 6)
            method_row["saved_vs_file_level_raw_ratio"] = round(max(0.0, file_raw - dl) / file_raw, 6)
        if file_tzst:
            method_row["saved_vs_file_level_tar_zstd_bytes"] = max(0, int(file_tzst) - dl)
            method_row["saved_vs_file_level_tar_zstd_cost"] = round(_egress_cost(file_tzst, download_count=download_count, cost_per_gb=cost_per_gb) - _egress_cost(dl, download_count=download_count, cost_per_gb=cost_per_gb), 6)
            method_row["saved_vs_file_level_tar_zstd_ratio"] = round(max(0.0, file_tzst - dl) / file_tzst, 6)
        if full_zstd:
            method_row["saved_vs_full_tar_zstd_bytes"] = max(0, int(full_zstd) - dl)
            method_row["saved_vs_full_tar_zstd_cost"] = round(_egress_cost(full_zstd, download_count=download_count, cost_per_gb=cost_per_gb) - _egress_cost(dl, download_count=download_count, cost_per_gb=cost_per_gb), 6)
            method_row["saved_vs_full_tar_zstd_ratio"] = round(max(0.0, full_zstd - dl) / full_zstd, 6)
        projection["methods"][method] = method_row
    result["cost_projection"] = projection
    return projection


def write_business_report(result: dict, path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    rows = _summary_rows(result)
    projection = result.get("cost_projection") or add_cost_projection(result)
    currency = str(projection.get("currency", "USD"))
    lines = [
        f"# CLD2 distribution savings report — {result.get('scenario', 'real-pair')}",
        "",
        "## Executive summary",
        "",
        "This report compares file-level/full redistribution against CLD2 chunk-level updates. It is a transparent byte * downloads * egress-price estimate, not a production SLA.",
        "",
        f"- Downloads/users modelled: {projection.get('download_count')}",
        f"- Egress price model: {projection.get('cost_per_gib')} {currency}/GiB",
        f"- Cost note: {projection.get('note')}",
        "",
        "## Update-size comparison",
        "",
        "| Method | Update bytes | Chunk reuse | Saved vs raw file-level | Saved vs tar.zst update | Estimated egress cost |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        method = r["method"]
        proj = projection.get("methods", {}).get(method, {})
        lines.append(
            f"| CLD2 {method} | {r['download_required_pack_bytes']} | {_pct(r['chunk_reuse_ratio'])} | "
            f"{_pct(r.get('saved_ratio_vs_file_level_raw'))} | {_pct(r.get('saved_ratio_vs_file_level_tar_zstd'))} | "
            f"{_fmt_cost(proj.get('estimated_cost', 0), currency)} |"
        )
    b = result.get("baseline", {})
    lines += [
        "",
        "## Baselines",
        "",
        "| Baseline | Bytes | Estimated egress cost | Source |",
        "|---|---:|---:|---|",
    ]
    external = set((b.get("external_baselines") or {}).keys())
    for key, label in [
        ("v2_logical_bytes", "Full logical v2"),
        ("full_tar_gz_v2_bytes", "Full tar.gz v2"),
        ("full_tar_zstd_v2_bytes", "Full tar.zst v2"),
        ("file_level_raw_download_bytes", "Changed files raw"),
        ("file_level_tar_gz_bytes", "Changed files tar.gz"),
        ("file_level_tar_zstd_bytes", "Changed files tar.zst"),
    ]:
        if key in b and b.get(key) is not None:
            cost = projection.get("baselines", {}).get(key, {}).get("estimated_cost", 0)
            src = "external" if key in external else "measured"
            lines.append(f"| {label} | {b[key]} | {_fmt_cost(cost, currency)} | {src} |")
    if b.get("external_baseline_note"):
        lines += ["", f"External baseline note: {b.get('external_baseline_note')}"]
    if b.get("heavy_baselines_skipped") and not external:
        lines += ["", "Heavy tar.gz/tar.zst baselines were skipped and no external baseline was provided for this run."]
    lines += [
        "",
        "## Savings projection",
        "",
        "| Method | Saved vs changed-files raw | Saved vs changed-files tar.zst | Saved vs full tar.zst |",
        "|---|---:|---:|---:|",
    ]
    for r in rows:
        method = r["method"]
        proj = projection.get("methods", {}).get(method, {})
        lines.append(
            f"| CLD2 {method} | {_fmt_cost(proj.get('saved_vs_file_level_raw_cost', 0), currency)} "
            f"({_pct(proj.get('saved_vs_file_level_raw_ratio'))}) | "
            f"{_fmt_cost(proj.get('saved_vs_file_level_tar_zstd_cost', 0), currency)} "
            f"({_pct(proj.get('saved_vs_file_level_tar_zstd_ratio'))}) | "
            f"{_fmt_cost(proj.get('saved_vs_full_tar_zstd_cost', 0), currency)} "
            f"({_pct(proj.get('saved_vs_full_tar_zstd_ratio'))}) |"
        )
    lines += [
        "",
        "## Technical details",
        "",
        "```json",
        json.dumps({
            "scenario": result.get("scenario"),
            "profile": result.get("profile"),
            "profiles": result.get("profiles"),
            "comparison": result.get("comparison"),
        }, indent=2, sort_keys=True),
        "```",
        "",
        "## Interpretation guardrail",
        "",
        "CLD2 is expected to help when release A and release B share many chunks. It is not expected to create savings on encrypted/random/completely rewritten data. Real CDN bills also depend on cache hit ratio, regions, request pricing and negotiated tiers.",
    ]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")

def write_csv_report(result: dict, path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    rows = _summary_rows(result)
    if not rows:
        p.write_text("", encoding="utf-8")
        return
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def _pct(value, digits: int = 4) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.{digits}%}"


def _fmt(value) -> str:
    return "n/a" if value is None else str(value)


def write_md_report(result: dict, path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    rows = _summary_rows(result)
    title_version = result.get("version", "2.0.0-alpha50.2")
    lines = [
        f"# CLD2 benchmark report — {result['scenario']} / {result.get('profile', 'custom')}",
        "",
        f"Version: `{title_version}`",
        "",
        "## Core result",
        "",
        "| Method | Download pack bytes | Raw chunk bytes | Chunk reuse | Reused | Added | Removed | Pack v1 | Pack v2 | Total pack | Saved vs file-level raw | Saved vs tar.zst update |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        lines.append(
            f"| {r['method']} | {r['download_required_pack_bytes']} | {r['download_required_raw_bytes']} | "
            f"{_pct(r['chunk_reuse_ratio'])} | {r.get('reused_chunks')} | {r.get('added_chunks')} | {r.get('removed_chunks')} | "
            f"{r['pack_v1_seconds']} s | {r['pack_v2_seconds']} s | {r['pack_total_seconds']} s | "
            f"{_pct(r['saved_ratio_vs_file_level_raw'])} | {_pct(r.get('saved_ratio_vs_file_level_tar_zstd'))} |"
        )
    by_method = {r['method']: r for r in rows}
    if 'fixed' in by_method:
        fixed_t = float(by_method['fixed']['pack_total_seconds'] or 0)
        if fixed_t:
            for method_name, row in by_method.items():
                if method_name == 'fixed':
                    continue
                other_t = float(row['pack_total_seconds'] or 0)
                lines += ["", f"{method_name} packing slowdown vs fixed: **{other_t / fixed_t:.2f}×**."]
    comparison = result.get("comparison") or {}
    if comparison.get("best_transfer_method"):
        best_bytes = comparison.get("best_transfer_bytes")
        baseline = result.get("baseline", {})
        lines += [
            "",
            "## Best transfer method",
            "",
            "Selection rule: minimum `download_required_pack_bytes`; ties break by total pack time, then stable method name.",
            "",
            f"Best method: `{comparison.get('best_transfer_method')}`",
            f"Best CLD2 update bytes: {best_bytes}",
        ]
        full_zstd = baseline.get("full_tar_zstd_v2_bytes")
        file_zstd = baseline.get("file_level_tar_zstd_bytes")
        if full_zstd:
            saved = int(full_zstd) - int(best_bytes or 0)
            ratio = (saved / int(full_zstd)) if int(full_zstd) else 0
            lines.append(f"Compared with full v2 tar.zstd: {saved} bytes saved ({_pct(max(0, ratio))}).")
        if file_zstd:
            saved = int(file_zstd) - int(best_bytes or 0)
            ratio = (saved / int(file_zstd)) if int(file_zstd) else 0
            lines.append(f"Compared with changed-files tar.zstd: {saved} bytes saved ({_pct(max(0, ratio))}).")
    lines += [
        "",
        "## Baseline",
        "",
        f"- V1 logical bytes: {result['baseline']['v1_logical_bytes']}",
        f"- V2 logical bytes: {result['baseline']['v2_logical_bytes']}",
        f"- File-level raw update bytes: {result['baseline']['file_level_raw_download_bytes']}",
        f"- File-level tar.gz update bytes: {_fmt(result['baseline'].get('file_level_tar_gz_bytes'))}",
        f"- File-level tar.zst update bytes: {_fmt(result['baseline'].get('file_level_tar_zstd_bytes'))}",
        f"- Full tar.gz v2 bytes: {_fmt(result['baseline'].get('full_tar_gz_v2_bytes'))}",
        f"- Full tar.zst v2 bytes: {_fmt(result['baseline'].get('full_tar_zstd_v2_bytes'))}",
    ]
    if result['baseline'].get('heavy_baselines_skipped'):
        lines += ["", "Heavy tar.gz/tar.zst baselines were skipped for this run."]
    lines += [
        "",
        "## Scenario metadata",
        "",
        "```json",
        json.dumps(result.get("scenario_metadata", {}), indent=2, sort_keys=True),
        "```",
        "",
        "## Profile",
        "",
        "```json",
        json.dumps(result.get("profile_options", {}), indent=2, sort_keys=True),
        "```",
        "",
        "## Interpretation guardrail",
        "",
        "This benchmark compares update strategies. A large-file insertion/boundary-shift scenario is favorable to CDC and should not be marketed as universal compression. CDC is expected to help when releases share many unchanged byte ranges; it is not expected to create savings on encrypted, random, transcoded or completely rewritten data.",
    ]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_matrix_reports(out: Path, matrix: dict) -> None:
    rows: list[dict] = []
    for res in matrix["results"]:
        rows.extend(_summary_rows(res))
    csv_path = out / "bench_matrix_summary.csv"
    if rows:
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    else:
        csv_path.write_text("", encoding="utf-8")
    lines = [
        f"# CLD2 benchmark matrix - profile {matrix['profile']}",
        "",
        "| Scenario | Method | Pack bytes | Chunk reuse | Saved vs file-level raw |",
        "|---|---|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(f"| {row['scenario']} | {row['method']} | {row['download_required_pack_bytes']} | {row['chunk_reuse_ratio']:.2%} | {row['saved_ratio_vs_file_level_raw']:.2%} |")
    lines += [
        "",
        "## Interpretation guardrail",
        "",
        "Worst-case/random scenarios are expected to show little or no reuse. Game/dataset insertion scenarios are expected to favor CDC because unchanged suffixes keep stable content-defined boundaries.",
    ]
    md_path = out / "bench_matrix_report.md"
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    matrix["reports"] = {"json": str(out / "bench_matrix_result.json"), "csv": str(csv_path), "markdown": str(md_path)}


def run_bench(
    out_dir: str | Path,
    *,
    chunker: str = "both",
    scenario: str = "game-patch-insert",
    profile: str = "quick",
    files: int | None = None,
    file_size: int | str | None = None,
    change_ratio: float | None = None,
    codec: str = "auto",
    report_md: str | Path | None = None,
    report_csv: str | Path | None = None,
) -> dict:
    out = Path(out_dir)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    profile_opts = _profile_values(profile, files=files, file_size=file_size, change_ratio=change_ratio)
    v1, v2 = make_demo_pair(
        out / "corpus",
        scenario=scenario,
        files=int(profile_opts["files"]),
        file_size=profile_opts["file_size"],
        change_ratio=profile_opts.get("change_ratio"),
    )

    baseline = _baseline_sizes(v1, v2, out, skip_heavy_baselines=False)

    modes = _modes_for_profile(profile, chunker)
    results = [_run_one(out, v1, v2, mode, profile_opts=profile_opts, codec=codec) for mode in modes]

    comparison = best_transfer_comparison(results)

    result = {
        "schema": "CoreLangDistribution/SyntheticBenchmark",
        "version": "2.0.0-alpha50.2",
        "scenario": scenario,
        "profile": profile,
        "profile_options": profile_opts,
        "codec": codec,
        "baseline": baseline,
        "runs": results,
        "comparison": comparison,
    }
    add_cost_projection(result)
    (out / "bench_result.json").write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    csv_path = Path(report_csv) if report_csv else out / "bench_summary.csv"
    md_path = Path(report_md) if report_md else out / "bench_report.md"
    write_csv_report(result, csv_path)
    write_md_report(result, md_path)
    business_path = out / "CLD2_savings_business_report.md"
    write_business_report(result, business_path)
    result["reports"] = {"json": str(out / "bench_result.json"), "csv": str(csv_path), "markdown": str(md_path), "business_markdown": str(business_path)}
    return result




def run_real_bench(
    old_dir: str | Path,
    new_dir: str | Path,
    out_dir: str | Path,
    *,
    chunker: str = "both",
    profile: str = "medium",
    codec: str = "auto",
    scenario_name: str = "real-directory-pair",
    cost_per_gb: float = 0.05,
    download_count: int = 1000,
    currency: str = "USD",
    skip_heavy_baselines: bool = False,
    scenario_kind: str | None = None,
    scenario_note: str | None = None,
    profile_file: str | Path | None = None,
    profile_data: dict | None = None,
) -> dict:
    """Benchmark a user-provided old/new directory pair.

    This bridges synthetic demos to presentable reports for
    actual content trees. It does not modify the input directories.
    """
    v1 = Path(old_dir)
    v2 = Path(new_dir)
    if not v1.is_dir():
        raise ValueError(f"old_dir is not a directory: {old_dir}")
    if not v2.is_dir():
        raise ValueError(f"new_dir is not a directory: {new_dir}")
    out = Path(out_dir)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    if profile_data:
        profile_opts = profile_pack_options(profile_data)
        profile = str(profile_data["name"])
        chunker = str(profile_opts["chunker"])
        codec = str(profile_opts["codec"])
    else:
        profile_opts = _profile_values(profile)
    baseline = _baseline_sizes(v1, v2, out, skip_heavy_baselines=skip_heavy_baselines)
    scenario_metadata = _real_pair_metadata(v1, v2)
    if scenario_kind:
        scenario_metadata["scenario_kind"] = scenario_kind
    if scenario_note:
        scenario_metadata["scenario_note"] = scenario_note
    if profile_data:
        scenario_metadata["profile_name"] = profile_data.get("name")
        scenario_metadata["profile_file"] = Path(profile_file or profile_data.get("_profile_file", "")).name
        scenario_metadata["profile"] = profile_metadata(profile_data, profile_file)
    modes = _modes_for_profile(profile, chunker)
    results = [_run_one(out, v1, v2, mode, profile_opts=profile_opts, codec=codec) for mode in modes]
    comparison = best_transfer_comparison(results)
    result = {
        "schema": "CoreLangDistribution/RealPairBenchmark",
        "version": "2.0.0-alpha50.2",
        "scenario": scenario_name,
        "profile": profile,
        "profile_options": profile_opts,
        "profile_file": str(profile_file) if profile_file else None,
        "codec": codec,
        "old_dir": str(v1),
        "new_dir": str(v2),
        "scenario_metadata": scenario_metadata,
        "baseline": baseline,
        "runs": results,
        "comparison": comparison,
    }
    add_cost_projection(result, download_count=download_count, cost_per_gb=cost_per_gb, currency=currency)
    (out / "bench_real_result.json").write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    write_csv_report(result, out / "bench_real_summary.csv")
    write_md_report(result, out / "bench_real_technical_report.md")
    write_business_report(result, out / "CLD2_savings_business_report.md")
    result["reports"] = {
        "json": str(out / "bench_real_result.json"),
        "csv": str(out / "bench_real_summary.csv"),
        "technical_markdown": str(out / "bench_real_technical_report.md"),
        "business_markdown": str(out / "CLD2_savings_business_report.md"),
    }
    return result




def _parse_profile_list(profiles: str | list[str] | tuple[str, ...]) -> list[str]:
    if isinstance(profiles, str):
        items = [x.strip() for x in profiles.split(",") if x.strip()]
    else:
        items = [str(x).strip() for x in profiles if str(x).strip()]
    if not items:
        items = ["large-file-small", "large-file-balanced", "large-file-large"]
    for item in items:
        if item not in BENCH_PROFILES:
            raise ValueError(f"unknown benchmark profile in tuning matrix: {item}")
        if not _is_large_file_profile(item):
            raise ValueError(f"fastcdc tuning expects large-file profiles, got: {item}")
    return items


def write_fastcdc_tune_report(result: dict, path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    rows = _summary_rows(result)
    fixed = next((r for r in rows if r["method"] == "fixed"), None)
    best_bytes = min((r for r in rows if r["method"] != "fixed"), key=lambda r: r["download_required_pack_bytes"], default=None)
    best_time = min((r for r in rows if r["method"] != "fixed"), key=lambda r: r["pack_total_seconds"], default=None)
    lines = [
        f"# CLD2 FastCDC tuning report - {result.get('scenario')}",
        "",
        "This report compares FastCDC large-file presets. Alpha20 FITS tuning selected `large-file-balanced` as the recommended default; this report is meant to show the time/transfer trade-off clearly.",
        "",
        "## Core tuning table",
        "",
        "| Method | Chunk profile | Stride | Download bytes | Chunk reuse | Added chunks | Avg chunk | Pack total | Saved vs fixed |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    fixed_dl = fixed["download_required_pack_bytes"] if fixed else None
    for r in rows:
        saved_vs_fixed = "n/a"
        if fixed_dl and r["method"] != "fixed":
            saved_vs_fixed = _pct(max(0, fixed_dl - r["download_required_pack_bytes"]) / fixed_dl)
        avg_chunk = "n/a"
        # Find underlying run to expose actual v2 chunk avg.
        for run in result.get("runs", []):
            if (run.get("method_label") or run.get("chunker")) == r["method"]:
                avg_chunk = str(run.get("v2_metrics", {}).get("chunk_size_stats", {}).get("avg", "n/a"))
                break
        lines.append(
            f"| {r['method']} | {r.get('method_profile', '')} | {r.get('fastcdc_stride') or ''} | "
            f"{r['download_required_pack_bytes']} | {_pct(r['chunk_reuse_ratio'])} | {r.get('added_chunks')} | "
            f"{avg_chunk} | {r['pack_total_seconds']} s | {saved_vs_fixed} |"
        )
    lines += ["", "## Selected candidates", ""]
    if best_bytes:
        lines.append(f"- Best transfer: **{best_bytes['method']}** with {best_bytes['download_required_pack_bytes']} bytes.")
    if best_time:
        lines.append(f"- Fastest fastcdc pack: **{best_time['method']}** with {best_time['pack_total_seconds']} s total pack time.")
    if fixed and best_bytes:
        lines.append(f"- Fixed baseline: {fixed['download_required_pack_bytes']} bytes, {fixed['pack_total_seconds']} s total pack time.")
    lines += [
        "",
        "## Baseline",
        "",
        f"- V1 logical bytes: {result['baseline']['v1_logical_bytes']}",
        f"- V2 logical bytes: {result['baseline']['v2_logical_bytes']}",
        f"- File-level raw update bytes: {result['baseline']['file_level_raw_download_bytes']}",
        "- Heavy tar.gz/tar.zst baselines are skipped in this tuning command by design unless supplied as external baseline values.",
        "",
        "## Guardrail",
        "",
        "Large-file insertion tests are favorable to CDC. Run overwrite, append, random-rewrite and completely-rewritten variants before making broader claims.",
    ]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_fastcdc_tuning_matrix(
    old_dir: str | Path,
    new_dir: str | Path,
    out_dir: str | Path,
    *,
    profiles: str | list[str] = "large-file-small,large-file-balanced,large-file-large",
    codec: str = "raw",
    scenario_name: str = "fastcdc-tuning",
    cost_per_gb: float = 0.05,
    download_count: int = 1000,
    currency: str = "USD",
    include_fixed: bool = True,
    scenario_kind: str | None = None,
    scenario_note: str | None = None,
    file_level_tar_zstd_bytes: int | None = None,
    full_tar_zstd_v2_bytes: int | None = None,
    external_baseline_note: str | None = None,
) -> dict:
    v1 = Path(old_dir)
    v2 = Path(new_dir)
    if not v1.is_dir():
        raise ValueError(f"old_dir is not a directory: {old_dir}")
    if not v2.is_dir():
        raise ValueError(f"new_dir is not a directory: {new_dir}")
    out = Path(out_dir)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    profile_list = _parse_profile_list(profiles)
    baseline = _baseline_sizes(v1, v2, out, skip_heavy_baselines=True)
    _apply_external_baselines(
        baseline,
        file_level_tar_zstd_bytes=file_level_tar_zstd_bytes,
        full_tar_zstd_v2_bytes=full_tar_zstd_v2_bytes,
        note=external_baseline_note,
    )
    scenario_metadata = _real_pair_metadata(v1, v2)
    if scenario_kind:
        scenario_metadata["scenario_kind"] = scenario_kind
    if scenario_note:
        scenario_metadata["scenario_note"] = scenario_note
    runs = []
    if include_fixed:
        fixed_opts = _profile_values("large-file")
        fixed_run = _run_one(out / "fixed", v1, v2, "fixed", profile_opts=fixed_opts, codec=codec, label="fixed")
        fixed_run["profile_name"] = "large-file-fixed-1MiB"
        runs.append(fixed_run)
    for profile_name in profile_list:
        opts = _profile_values(profile_name)
        label = f"fastcdc:{profile_name}"
        run = _run_one(out / profile_name, v1, v2, "fastcdc", profile_opts=opts, codec=codec, label=label)
        run["profile_name"] = profile_name
        runs.append(run)
    comparison: dict[str, object] = {}
    fixed_run = next((r for r in runs if (r.get("method_label") or r.get("chunker")) == "fixed"), None)
    fast_runs = [r for r in runs if r.get("chunker") == "fastcdc"]
    if fixed_run and fast_runs:
        fixed_dl = fixed_run["diff"]["download_required_pack_bytes"]
        best_bytes = min(fast_runs, key=lambda r: r["diff"]["download_required_pack_bytes"])
        best_time = min(fast_runs, key=lambda r: float(r["pack_v1_seconds"]) + float(r["pack_v2_seconds"]))
        comparison = {
            "fixed_download_bytes": fixed_dl,
            "best_transfer_method": best_bytes.get("method_label"),
            "best_transfer_bytes": best_bytes["diff"]["download_required_pack_bytes"],
            "best_transfer_saved_vs_fixed_bytes": max(0, fixed_dl - best_bytes["diff"]["download_required_pack_bytes"]),
            "best_transfer_saved_ratio_vs_fixed": round(max(0, fixed_dl - best_bytes["diff"]["download_required_pack_bytes"]) / fixed_dl, 6) if fixed_dl else 0,
            "fastest_fastcdc_method": best_time.get("method_label"),
            "fastest_fastcdc_total_pack_seconds": round(float(best_time["pack_v1_seconds"]) + float(best_time["pack_v2_seconds"]), 4),
        }
    result = {
        "schema": "CoreLangDistribution/FastCDCTuningMatrix",
        "version": "2.0.0-alpha50.2",
        "scenario": scenario_name,
        "profile": "fastcdc-tuning",
        "profiles": profile_list,
        "codec": codec,
        "old_dir": str(v1),
        "new_dir": str(v2),
        "scenario_metadata": scenario_metadata,
        "baseline": baseline,
        "runs": runs,
        "comparison": comparison,
    }
    add_cost_projection(result, download_count=download_count, cost_per_gb=cost_per_gb, currency=currency)
    json_path = out / "fastcdc_tune_result.json"
    csv_path = out / "fastcdc_tune_summary.csv"
    md_path = out / "fastcdc_tune_report.md"
    business_path = out / "CLD2_fastcdc_tuning_business_report.md"
    json_path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    write_csv_report(result, csv_path)
    write_fastcdc_tune_report(result, md_path)
    write_business_report(result, business_path)
    result["reports"] = {"json": str(json_path), "csv": str(csv_path), "markdown": str(md_path), "business_markdown": str(business_path)}
    return result



def _write_repeatable_large_file(path: Path, size_bytes: int, *, seed: int = 123) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    # Deterministic mixed content: partly repetitive, partly pseudo-random. This keeps the
    # variant smoke realistic enough without requiring huge artifacts.
    block = bytearray()
    while len(block) < 1024 * 1024:
        marker = f"CLD2-LARGEFILE-BLOCK-{len(block):08d}-SEED-{seed}\n".encode()
        block.extend(marker * 8)
        block.extend(rng.randbytes(4096))
    block = bytes(block[:1024 * 1024])
    remaining = size_bytes
    with path.open("wb") as f:
        while remaining > 0:
            chunk = block[: min(len(block), remaining)]
            f.write(chunk)
            remaining -= len(chunk)


def _make_largefile_variant_pair(root: Path, *, variant: str, size_bytes: int) -> tuple[Path, Path]:
    v1 = root / "v1"
    v2 = root / "v2"
    if root.exists():
        shutil.rmtree(root)
    v1.mkdir(parents=True)
    v2.mkdir(parents=True)
    f1 = v1 / "large.bin"
    f2 = v2 / "large.bin"
    _write_repeatable_large_file(f1, size_bytes, seed=444)
    data = f1.read_bytes()
    mid = len(data) // 2
    if variant == "middle-insert":
        insert = (b"CLD2-MIDDLE-INSERT-ALPHA20\n" * 4096)[:128 * 1024]
        f2.write_bytes(data[:mid] + insert + data[mid:])
    elif variant == "localized-overwrite":
        patch = (b"CLD2-LOCALIZED-OVERWRITE-ALPHA20\n" * 4096)[:128 * 1024]
        start = max(0, mid - len(patch) // 2)
        f2.write_bytes(data[:start] + patch + data[start + len(patch):])
    elif variant == "random-rewrite-1pct":
        out = bytearray(data)
        rng = random.Random(98765)
        rewrite = max(1, len(out) // 100)
        step = max(1, rewrite // 16)
        changed = 0
        while changed < rewrite:
            pos = rng.randrange(0, max(1, len(out) - step))
            payload = rng.randbytes(min(step, rewrite - changed))
            out[pos:pos + len(payload)] = payload
            changed += len(payload)
        f2.write_bytes(out)
    else:
        raise ValueError(f"unknown large-file variant: {variant}")
    return v1, v2


def run_largefile_variant_matrix(
    out_dir: str | Path,
    *,
    variants: str | list[str] = "middle-insert,localized-overwrite,random-rewrite-1pct",
    profiles: str | list[str] = "large-file-balanced",
    file_size: str = "32MiB",
    codec: str = "raw",
    download_count: int = 1000,
    cost_per_gb: float = 0.05,
    currency: str = "USD",
) -> dict:
    out = Path(out_dir)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    if isinstance(variants, str):
        variant_list = [x.strip() for x in variants.split(",") if x.strip()]
    else:
        variant_list = [str(x).strip() for x in variants if str(x).strip()]
    allowed = {"middle-insert", "localized-overwrite", "random-rewrite-1pct"}
    for variant in variant_list:
        if variant not in allowed:
            raise ValueError(f"unknown variant {variant}; choose from {sorted(allowed)}")
    size_bytes = parse_size(file_size)
    results = []
    for variant in variant_list:
        pair_root = out / f"pair_{variant}"
        v1, v2 = _make_largefile_variant_pair(pair_root, variant=variant, size_bytes=size_bytes)
        res = run_fastcdc_tuning_matrix(
            v1,
            v2,
            out / f"report_{variant}",
            profiles=profiles,
            codec=codec,
            scenario_name=f"alpha24-{variant}",
            scenario_kind=variant,
            scenario_note="Synthetic negative/guardrail variant; same generated file base, different change pattern.",
            cost_per_gb=cost_per_gb,
            download_count=download_count,
            currency=currency,
        )
        results.append(res)
        # Keep the final package/report light: remove generated pair data and repos after
        # the reports have been written.
        shutil.rmtree(pair_root, ignore_errors=True)
        for child in (out / f"report_{variant}").iterdir():
            if child.is_dir() and child.name in {"fixed", "large-file-small", "large-file-balanced", "large-file-large", "large-file"}:
                shutil.rmtree(child, ignore_errors=True)
    matrix = {
        "schema": "CoreLangDistribution/LargeFileVariantMatrix",
        "version": "2.0.0-alpha50.2",
        "file_size": file_size,
        "profiles": _parse_profile_list(profiles),
        "variants": variant_list,
        "results": results,
    }
    json_path = out / "largefile_variants_result.json"
    csv_path = out / "largefile_variants_summary.csv"
    md_path = out / "largefile_variants_report.md"
    json_path.write_text(json.dumps(matrix, indent=2, sort_keys=True), encoding="utf-8")
    rows = []
    for res in results:
        rows.extend(_summary_rows(res))
    if rows:
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader(); w.writerows(rows)
    lines = [
        "# CLD2 large-file guardrail variants",
        "",
        "These synthetic variants are not a replacement for the real FITS benchmark. They are guardrails showing that the same FastCDC profile behaves differently across change patterns.",
        "",
        "| Variant | Method | Download bytes | Chunk reuse | Pack total | Saved vs raw |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(f"| {row['scenario']} | {row['method']} | {row['download_required_pack_bytes']} | {_pct(row['chunk_reuse_ratio'])} | {row['pack_total_seconds']} s | {_pct(row['saved_ratio_vs_file_level_raw'])} |")
    lines += ["", "## Guardrail", "", "Middle insertions favor CDC. Localized overwrites should still reuse most chunks. Random rewrites reduce reuse depending on the amount and distribution of changed bytes."]
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    matrix["reports"] = {"json": str(json_path), "csv": str(csv_path), "markdown": str(md_path)}
    return matrix


def write_matrix_business_report(matrix: dict, path: str | Path, *, cost_per_gb: float = 0.05, download_count: int = 1000, currency: str = "USD") -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# CLD2 benchmark matrix business summary — profile {matrix.get('profile')}",
        "",
        f"Model: {download_count} downloads at {cost_per_gb} {currency}/GiB egress.",
        "",
        "| Scenario | Method | Update bytes | Saved vs file-level raw | Estimated cost | Estimated saving |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for res in matrix.get("results", []):
        add_cost_projection(res, download_count=download_count, cost_per_gb=cost_per_gb, currency=currency)
        rows = _summary_rows(res)
        projection = res.get("cost_projection", {})
        for row in rows:
            method = row["method"]
            proj = projection.get("methods", {}).get(method, {})
            lines.append(
                f"| {res.get('scenario')} | {method} | {row['download_required_pack_bytes']} | "
                f"{row['saved_ratio_vs_file_level_raw']:.2%} | {proj.get('estimated_cost', 0)} {currency} | "
                f"{proj.get('saved_vs_file_level_raw_cost', 0)} {currency} |"
            )
    lines += [
        "",
        "## Guardrail",
        "",
        "This is a byte-transfer projection, not a billing guarantee. It does not include request fees, storage, taxes, regional tiers, negotiated CDN discounts, or cache hit ratios.",
    ]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")

def run_bench_matrix(
    out_dir: str | Path,
    *,
    scenarios: list[str] | None = None,
    profile: str = "quick",
    chunker: str = "both",
    codec: str = "auto",
) -> dict:
    out = Path(out_dir)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    scenario_list = scenarios or ["game-patch-insert", "dataset-model", "media-catalog", "random-worstcase"]
    for s in scenario_list:
        if s not in BENCH_SCENARIOS:
            raise ValueError(f"unknown benchmark scenario: {s}")
    results = []
    for scenario in scenario_list:
        res = run_bench(out / scenario, scenario=scenario, profile=profile, chunker=chunker, codec=codec)
        # Store relative report paths in the aggregate for portability.
        results.append(res)
    matrix = {
        "schema": "CoreLangDistribution/BenchmarkMatrix",
        "version": "2.0.0-alpha50.2",
        "profile": profile,
        "chunker": chunker,
        "codec": codec,
        "scenarios": scenario_list,
        "results": results,
    }
    _write_matrix_reports(out, matrix)
    write_matrix_business_report(matrix, out / "bench_matrix_business_report.md")
    matrix.setdefault("reports", {})["business_markdown"] = str(out / "bench_matrix_business_report.md")
    (out / "bench_matrix_result.json").write_text(json.dumps(matrix, indent=2, sort_keys=True), encoding="utf-8")
    return matrix

# --- alpha24 adaptive policy helpers -------------------------------------------------

def _run_total_pack_seconds(run: dict) -> float:
    return float(run.get("pack_v1_seconds", 0.0)) + float(run.get("pack_v2_seconds", 0.0))


def _run_download_bytes(run: dict) -> int:
    return int(run.get("diff", {}).get("download_required_pack_bytes", 0))


def _run_name(run: dict) -> str:
    return str(run.get("profile_name") or run.get("method_label") or run.get("chunker") or "unknown")


def recommend_adaptive_policy(
    tuning: dict,
    *,
    download_tolerance_ratio: float = 0.10,
    download_tolerance_bytes: int = 256 * 1024 * 1024,
    fixed_close_ratio: float = 0.10,
) -> dict:
    """Pick practical CLD2 policies from a fixed/FastCDC tuning matrix.

    The rules are intentionally transparent rather than magical:
    - bandwidth_first: minimum download bytes.
    - speed_first: minimum total pack time.
    - default: fastest method whose download is within a tolerance of the best
      download candidate; if fixed is essentially as good as the best transfer,
      fixed wins because it is simpler/faster.
    """
    runs = list(tuning.get("runs", []))
    if not runs:
        return {"ok": False, "error": "no runs in tuning matrix"}

    rows = []
    for r in runs:
        dl = _run_download_bytes(r)
        t = _run_total_pack_seconds(r)
        rows.append({
            "name": _run_name(r),
            "chunker": r.get("chunker"),
            "download_required_pack_bytes": dl,
            "chunk_reuse_ratio": r.get("diff", {}).get("chunk_reuse_ratio"),
            "pack_total_seconds": round(t, 4),
            "pack_v1_seconds": r.get("pack_v1_seconds"),
            "pack_v2_seconds": r.get("pack_v2_seconds"),
        })

    best_transfer = min(rows, key=lambda r: r["download_required_pack_bytes"])
    fastest = min(rows, key=lambda r: r["pack_total_seconds"])
    best_dl = int(best_transfer["download_required_pack_bytes"])
    fixed = next((r for r in rows if r["name"] == "fixed" or r["chunker"] == "fixed"), None)

    allowed_extra = max(int(best_dl * download_tolerance_ratio), int(download_tolerance_bytes))
    candidates = [r for r in rows if int(r["download_required_pack_bytes"]) <= best_dl + allowed_extra]
    default = min(candidates, key=lambda r: r["pack_total_seconds"])
    default_reason = (
        f"fastest candidate within max({download_tolerance_ratio:.0%} of best transfer, "
        f"{download_tolerance_bytes} bytes) tolerance"
    )

    if fixed is not None and best_dl > 0:
        fixed_extra_ratio = (int(fixed["download_required_pack_bytes"]) - best_dl) / best_dl
        if fixed_extra_ratio <= fixed_close_ratio and fixed["pack_total_seconds"] <= default["pack_total_seconds"]:
            default = fixed
            default_reason = f"fixed is within {fixed_close_ratio:.0%} of best transfer and is faster/simpler"

    # Named modes for CLI/docs.
    fastcdc_rows = [r for r in rows if str(r.get("chunker")) == "fastcdc" or str(r["name"]).startswith("large-file")]
    bandwidth_first = best_transfer
    speed_first = fastest
    distribution_default = default

    # If a FastCDC profile is near-best transfer and faster than the absolute best, expose it.
    near_best_fastcdc = None
    if fastcdc_rows:
        near_candidates = [r for r in fastcdc_rows if int(r["download_required_pack_bytes"]) <= best_dl + allowed_extra]
        if near_candidates:
            near_best_fastcdc = min(near_candidates, key=lambda r: r["pack_total_seconds"])

    baseline = tuning.get("baseline", {})
    file_raw = int(baseline.get("file_level_raw_download_bytes") or 0)
    fixed_dl = int(fixed["download_required_pack_bytes"]) if fixed else None
    notes = []
    if file_raw:
        notes.append("file-level raw baseline is present and used for savings ratios")
    if baseline.get("file_level_tar_zstd_bytes"):
        notes.append("external or generated tar.zst baseline is present")
    else:
        notes.append("tar.zst baseline absent; report savings vs raw/fixed only")
    if fixed and fixed_dl is not None and best_dl:
        fixed_saved_by_default = fixed_dl - int(distribution_default["download_required_pack_bytes"])
        notes.append(f"default saves {fixed_saved_by_default} bytes vs fixed on this matrix")

    return {
        "ok": True,
        "schema": "CoreLangDistribution/AdaptivePolicyRecommendation",
        "version": "2.0.0-alpha50.2",
        "scenario": tuning.get("scenario"),
        "decision_parameters": {
            "download_tolerance_ratio": download_tolerance_ratio,
            "download_tolerance_bytes": download_tolerance_bytes,
            "fixed_close_ratio": fixed_close_ratio,
        },
        "recommendations": {
            "distribution_default": distribution_default,
            "default_reason": default_reason,
            "bandwidth_first": bandwidth_first,
            "speed_first": speed_first,
            "near_best_fastcdc": near_best_fastcdc,
        },
        "rows": rows,
        "notes": notes,
    }


def write_adaptive_policy_report(policy: dict, path: str | Path) -> None:
    rec = policy.get("recommendations", {})
    rows = policy.get("rows", [])
    lines = [
        "# CLD2 adaptive policy report",
        "",
        f"Scenario: `{policy.get('scenario')}`",
        "",
        "## Recommendation",
        "",
    ]
    for label in ["distribution_default", "bandwidth_first", "speed_first", "near_best_fastcdc"]:
        item = rec.get(label)
        if item:
            lines.append(f"- **{label}**: `{item.get('name')}` — {item.get('download_required_pack_bytes')} bytes, {item.get('pack_total_seconds')} s pack")
    if rec.get("default_reason"):
        lines.append(f"- **default reason**: {rec['default_reason']}")
    lines += [
        "",
        "## Matrix",
        "",
        "| Method | Download bytes | Chunk reuse | Pack total seconds |",
        "|---|---:|---:|---:|",
    ]
    for r in rows:
        reuse = r.get("chunk_reuse_ratio")
        reuse_s = "" if reuse is None else f"{float(reuse)*100:.4f}%"
        lines.append(f"| {r.get('name')} | {r.get('download_required_pack_bytes')} | {reuse_s} | {r.get('pack_total_seconds')} |")
    lines += ["", "## Notes", ""]
    for n in policy.get("notes", []):
        lines.append(f"- {n}")
    lines.append("")
    Path(path).write_text("\n".join(lines), encoding="utf-8")


def run_adaptive_policy_bench(
    old_dir: str | Path,
    new_dir: str | Path,
    out_dir: str | Path,
    *,
    profiles: str | list[str] = "large-file-balanced,large-file-large",
    codec: str = "raw",
    scenario_name: str = "adaptive-policy",
    cost_per_gb: float = 0.05,
    download_count: int = 1000,
    currency: str = "USD",
    scenario_kind: str | None = None,
    scenario_note: str | None = None,
    file_level_tar_zstd_bytes: int | None = None,
    full_tar_zstd_v2_bytes: int | None = None,
    external_baseline_note: str | None = None,
    download_tolerance_ratio: float = 0.10,
    download_tolerance_bytes: int = 256 * 1024 * 1024,
    fixed_close_ratio: float = 0.10,
) -> dict:
    out = Path(out_dir)
    tuning = run_fastcdc_tuning_matrix(
        old_dir,
        new_dir,
        out,
        profiles=profiles,
        codec=codec,
        scenario_name=scenario_name,
        cost_per_gb=cost_per_gb,
        download_count=download_count,
        currency=currency,
        include_fixed=True,
        scenario_kind=scenario_kind,
        scenario_note=scenario_note,
        file_level_tar_zstd_bytes=file_level_tar_zstd_bytes,
        full_tar_zstd_v2_bytes=full_tar_zstd_v2_bytes,
        external_baseline_note=external_baseline_note,
    )
    policy = recommend_adaptive_policy(
        tuning,
        download_tolerance_ratio=download_tolerance_ratio,
        download_tolerance_bytes=download_tolerance_bytes,
        fixed_close_ratio=fixed_close_ratio,
    )
    policy_path = out / "adaptive_policy_recommendation.json"
    policy_md = out / "adaptive_policy_report.md"
    policy_path.write_text(json.dumps(policy, indent=2, sort_keys=True), encoding="utf-8")
    write_adaptive_policy_report(policy, policy_md)
    result = {
        "schema": "CoreLangDistribution/AdaptivePolicyBench",
        "version": "2.0.0-alpha50.2",
        "ok": bool(policy.get("ok")),
        "tuning": tuning,
        "policy": policy,
        "reports": {
            "tuning_json": str(out / "fastcdc_tune_result.json"),
            "tuning_csv": str(out / "fastcdc_tune_summary.csv"),
            "tuning_markdown": str(out / "fastcdc_tune_report.md"),
            "business_markdown": str(out / "CLD2_fastcdc_tuning_business_report.md"),
            "policy_json": str(policy_path),
            "policy_markdown": str(policy_md),
        },
    }
    result_path = out / "adaptive_policy_bench_result.json"
    result_path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    result["reports"]["result_json"] = str(result_path)
    return result


# --- alpha24 standalone baseline comparison ----------------------------------------

def _alpha24_gib(n: int | float) -> float:
    return float(n) / (1024 ** 3)


def _alpha24_cost(n: int | float, *, download_count: int, cost_per_gb: float) -> float:
    return _alpha24_gib(n) * float(download_count) * float(cost_per_gb)


def _alpha24_profile_rows_from_policy(policy_bench: dict, *, download_count: int, cost_per_gb: float, currency: str) -> list[dict]:
    tuning = policy_bench.get("tuning", {})
    rows: list[dict] = []
    for run in tuning.get("runs", []):
        name = str(run.get("profile_name") or run.get("method_label") or run.get("chunker"))
        method = "fixed" if run.get("chunker") == "fixed" else f"fastcdc:{name}"
        dl = int(run.get("diff", {}).get("download_required_pack_bytes") or 0)
        reuse = run.get("diff", {}).get("chunk_reuse_ratio")
        pack_total = float(run.get("pack_v1_seconds") or 0) + float(run.get("pack_v2_seconds") or 0)
        rows.append({
            "category": "cld2",
            "method": method,
            "bytes": dl,
            "gib": _alpha24_gib(dl),
            "estimated_cost": _alpha24_cost(dl, download_count=download_count, cost_per_gb=cost_per_gb),
            "currency": currency,
            "chunk_reuse_ratio": reuse,
            "pack_total_seconds": round(pack_total, 4),
            "note": "measured by CLD2",
        })
    return rows


def _alpha24_external_row(name: str, value: int | None, *, download_count: int, cost_per_gb: float, currency: str, note: str = "") -> dict | None:
    if value is None:
        return None
    v = int(value)
    return {
        "category": "external_baseline",
        "method": name,
        "bytes": v,
        "gib": _alpha24_gib(v),
        "estimated_cost": _alpha24_cost(v, download_count=download_count, cost_per_gb=cost_per_gb),
        "currency": currency,
        "chunk_reuse_ratio": None,
        "pack_total_seconds": None,
        "note": note,
    }


def write_standalone_baselines_report(result: dict, path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    rows = list(result.get("comparison_rows", []))
    policy = result.get("adaptive_policy", {}).get("policy", {})
    rec = policy.get("recommendations", {})
    lines = [
        f"# CLD2 standalone baseline report - {result.get('scenario')}",
        "",
        "This report is meant for evaluating CoreLangDistribution as a standalone project.",
        "It places CLD2 fixed/FastCDC/adaptive-policy results next to serious baselines such as tar.zst and optional external tools.",
        "",
        "## Dataset",
        "",
        f"- v1 logical bytes: {result.get('baseline', {}).get('v1_logical_bytes')}",
        f"- v2 logical bytes: {result.get('baseline', {}).get('v2_logical_bytes')}",
        f"- file-level raw update bytes: {result.get('baseline', {}).get('file_level_raw_download_bytes')}",
        "",
        "## Comparison table",
        "",
        "| Category | Method | Bytes | GiB | Estimated egress cost | Chunk reuse | Pack total | Note |",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for r in rows:
        reuse = "" if r.get("chunk_reuse_ratio") is None else _pct(float(r["chunk_reuse_ratio"]))
        pack = "" if r.get("pack_total_seconds") is None else f"{r['pack_total_seconds']} s"
        lines.append(
            f"| {r.get('category')} | {r.get('method')} | {r.get('bytes')} | "
            f"{float(r.get('gib', 0)):.6f} | {float(r.get('estimated_cost', 0)):.6f} {r.get('currency', '')} | "
            f"{reuse} | {pack} | {r.get('note','')} |"
        )
    lines += ["", "## Adaptive recommendation", ""]
    for label in ["distribution_default", "bandwidth_first", "speed_first", "near_best_fastcdc"]:
        item = rec.get(label)
        if item:
            lines.append(f"- **{label}**: `{item.get('name')}` — {item.get('download_required_pack_bytes')} bytes, {item.get('pack_total_seconds')} s pack")
    if rec.get("default_reason"):
        lines.append(f"- **default reason**: {rec['default_reason']}")
    lines += [
        "",
        "## Missing external baselines",
        "",
    ]
    missing = result.get("missing_external_baselines", [])
    if missing:
        for m in missing:
            lines.append(f"- {m}")
    else:
        lines.append("- none")
    lines += [
        "",
        "## Interpretation guardrail",
        "",
        "Do not claim that CLD2 invents delta-update. The defensible claim is narrower: CLD2 provides a Python standalone distribution engine with content-addressed chunks, FastCDC/fixed trade-offs, verification, recovery tooling, and an adaptive policy that chooses a profile based on measured time/transfer.",
        "",
        "For publication-quality claims, compare against at least tar.zst, file-level update, fixed chunking, FastCDC, and ideally rsync/zsync/casync on the same dataset.",
    ]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_standalone_baselines_bench(
    old_dir: str | Path,
    new_dir: str | Path,
    out_dir: str | Path,
    *,
    profiles: str | list[str] = "large-file-balanced,large-file-large",
    codec: str = "raw",
    scenario_name: str = "standalone-baselines",
    scenario_kind: str | None = None,
    scenario_note: str | None = None,
    download_count: int = 1000,
    cost_per_gb: float = 0.05,
    currency: str = "USD",
    file_level_tar_zstd_bytes: int | None = None,
    full_tar_zstd_v2_bytes: int | None = None,
    rsync_bytes: int | None = None,
    zsync_bytes: int | None = None,
    casync_bytes: int | None = None,
    external_baseline_note: str | None = None,
    download_tolerance_ratio: float = 0.10,
    download_tolerance_bytes: int = 256 * 1024 * 1024,
    fixed_close_ratio: float = 0.10,
) -> dict:
    out = Path(out_dir)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    policy_bench = run_adaptive_policy_bench(
        old_dir,
        new_dir,
        out,
        profiles=profiles,
        codec=codec,
        scenario_name=scenario_name,
        cost_per_gb=cost_per_gb,
        download_count=download_count,
        currency=currency,
        scenario_kind=scenario_kind,
        scenario_note=scenario_note,
        file_level_tar_zstd_bytes=file_level_tar_zstd_bytes,
        full_tar_zstd_v2_bytes=full_tar_zstd_v2_bytes,
        external_baseline_note=external_baseline_note,
        download_tolerance_ratio=download_tolerance_ratio,
        download_tolerance_bytes=download_tolerance_bytes,
        fixed_close_ratio=fixed_close_ratio,
    )
    tuning = policy_bench.get("tuning", {})
    baseline = tuning.get("baseline", {})
    rows: list[dict] = []
    rows.append({
        "category": "baseline",
        "method": "file-level-raw-update",
        "bytes": int(baseline.get("file_level_raw_download_bytes") or 0),
        "gib": _alpha24_gib(int(baseline.get("file_level_raw_download_bytes") or 0)),
        "estimated_cost": _alpha24_cost(int(baseline.get("file_level_raw_download_bytes") or 0), download_count=download_count, cost_per_gb=cost_per_gb),
        "currency": currency,
        "chunk_reuse_ratio": None,
        "pack_total_seconds": None,
        "note": "changed files sent raw",
    })
    for row in [
        _alpha24_external_row("file-level-tar.zst-update", file_level_tar_zstd_bytes, download_count=download_count, cost_per_gb=cost_per_gb, currency=currency, note=external_baseline_note or "external or precomputed baseline"),
        _alpha24_external_row("full-v2-tar.zst", full_tar_zstd_v2_bytes, download_count=download_count, cost_per_gb=cost_per_gb, currency=currency, note=external_baseline_note or "external or precomputed baseline"),
        _alpha24_external_row("rsync", rsync_bytes, download_count=download_count, cost_per_gb=cost_per_gb, currency=currency, note="external measured baseline supplied by user"),
        _alpha24_external_row("zsync", zsync_bytes, download_count=download_count, cost_per_gb=cost_per_gb, currency=currency, note="external measured baseline supplied by user"),
        _alpha24_external_row("casync", casync_bytes, download_count=download_count, cost_per_gb=cost_per_gb, currency=currency, note="external measured baseline supplied by user"),
    ]:
        if row:
            rows.append(row)
    rows.extend(_alpha24_profile_rows_from_policy(policy_bench, download_count=download_count, cost_per_gb=cost_per_gb, currency=currency))

    missing = []
    if file_level_tar_zstd_bytes is None:
        missing.append("file-level tar.zst update baseline")
    if full_tar_zstd_v2_bytes is None:
        missing.append("full v2 tar.zst baseline")
    if rsync_bytes is None:
        missing.append("rsync measured baseline")
    if zsync_bytes is None:
        missing.append("zsync measured baseline")
    if casync_bytes is None:
        missing.append("casync measured baseline")

    csv_path = out / "standalone_baselines_summary.csv"
    if rows:
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

    result = {
        "schema": "CoreLangDistribution/StandaloneBaselinesBenchmark",
        "version": "2.0.0-alpha50.2",
        "ok": bool(policy_bench.get("ok")),
        "scenario": scenario_name,
        "scenario_kind": scenario_kind,
        "scenario_note": scenario_note,
        "baseline": baseline,
        "adaptive_policy": policy_bench,
        "comparison_rows": rows,
        "missing_external_baselines": missing,
        "reports": {
            "standalone_json": str(out / "standalone_baselines_result.json"),
            "standalone_csv": str(csv_path),
            "standalone_markdown": str(out / "standalone_baselines_report.md"),
            "adaptive_policy_json": str(out / "adaptive_policy_recommendation.json"),
            "adaptive_policy_markdown": str(out / "adaptive_policy_report.md"),
            "tuning_json": str(out / "fastcdc_tune_result.json"),
            "tuning_csv": str(out / "fastcdc_tune_summary.csv"),
            "tuning_markdown": str(out / "fastcdc_tune_report.md"),
            "business_markdown": str(out / "CLD2_fastcdc_tuning_business_report.md"),
        },
    }
    (out / "standalone_baselines_result.json").write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    write_standalone_baselines_report(result, out / "standalone_baselines_report.md")
    return result


# --- alpha25 heavy-change validation ---------------------------------------------

def _alpha25_parse_ratio_list(value: str | list[float] | list[str]) -> list[float]:
    if isinstance(value, str):
        items = [x.strip() for x in value.split(",") if x.strip()]
    else:
        items = [str(x).strip() for x in value if str(x).strip()]
    out = []
    for item in items:
        if item.endswith("%"):
            out.append(float(item[:-1]) / 100.0)
        else:
            out.append(float(item))
    return out


def _alpha25_parse_string_list(value: str | list[str]) -> list[str]:
    if isinstance(value, str):
        return [x.strip() for x in value.split(",") if x.strip()]
    return [str(x).strip() for x in value if str(x).strip()]


def _alpha25_make_structured_bytes(size_bytes: int, seed: int = 25) -> bytes:
    rnd = random.Random(seed)
    block = bytearray()
    motifs = [
        b"CLD2_ALPHA25_ASTRO_BLOCK\n",
        b"FITS-LIKE-HEADER-CARD = VALUE / COMMENT\n",
        b"CATALOG-ROW: RA DEC Z MASS FLUX QUALITY\n",
        b"COMMON-DATA-SEGMENT-0000000000000000\n",
    ]
    while len(block) < size_bytes:
        motif = motifs[len(block) % len(motifs)]
        block.extend(motif)
        block.extend(rnd.randbytes(64))
    return bytes(block[:size_bytes])


def _alpha25_apply_change(data: bytes, *, mode: str, ratio: float, seed: int = 2500) -> bytes:
    size = len(data)
    n = max(1, int(size * ratio))
    buf = bytearray(data)
    rnd = random.Random(seed + int(ratio * 1_000_000) + len(mode))
    if mode == "localized":
        start = max(0, (size - n) // 2)
        replacement = _alpha25_make_structured_bytes(n, seed=seed + 77)
        buf[start:start+n] = replacement
    elif mode == "distributed":
        stripe = 4096
        changed = 0
        positions = list(range(0, max(1, size - stripe), max(stripe * 16, 1)))
        rnd.shuffle(positions)
        for pos in positions:
            if changed >= n:
                break
            take = min(stripe, n - changed, size - pos)
            buf[pos:pos+take] = rnd.randbytes(take)
            changed += take
    elif mode == "rewrite-prefix":
        replacement = rnd.randbytes(n)
        buf[:n] = replacement
    else:
        raise ValueError(f"unknown heavy-change mode {mode}")
    return bytes(buf)


def _alpha25_make_heavy_pair(pair_root: Path, *, file_size_bytes: int, ratio: float, mode: str) -> tuple[Path, Path]:
    if pair_root.exists():
        shutil.rmtree(pair_root)
    v1 = pair_root / "release_v1"
    v2 = pair_root / "release_v2"
    v1.mkdir(parents=True)
    v2.mkdir(parents=True)

    main_size = int(file_size_bytes * 0.70)
    aux_size = int(file_size_bytes * 0.20)
    meta_size = max(1024, file_size_bytes - main_size - aux_size)

    main = _alpha25_make_structured_bytes(main_size, seed=101)
    aux = _alpha25_make_structured_bytes(aux_size, seed=102)
    meta = (b'{"schema":"CLD2/alpha25","rows":[\n' + (b'{"k":"value","n":123},\n' * max(1, meta_size // 24)))[:meta_size]

    (v1 / "catalog_main.fits").write_bytes(main)
    (v1 / "catalog_aux.fits").write_bytes(aux)
    (v1 / "metadata.json").write_bytes(meta)

    (v2 / "catalog_main.fits").write_bytes(_alpha25_apply_change(main, mode=mode, ratio=ratio, seed=202))
    (v2 / "catalog_aux.fits").write_bytes(aux + (b"ALPHA25_APPEND\n" * 128))
    (v2 / "metadata.json").write_bytes(meta + f'\n{{"alpha25_change_ratio":{ratio},"mode":"{mode}"}}\n'.encode("utf-8"))
    return v1, v2


def _alpha25_try_make_tar_zst_baselines(v1: Path, v2: Path, out: Path) -> tuple[int | None, int | None, str]:
    try:
        import zstandard as zstd  # type: ignore
    except Exception as exc:
        return None, None, f"zstandard unavailable: {exc}"

    import tarfile

    def iter_files(root: Path):
        return sorted([p for p in root.rglob("*") if p.is_file()])

    def rel(p: Path, root: Path):
        return p.relative_to(root).as_posix()

    old = {rel(p, v1): sha256_file(p) for p in iter_files(v1)}
    changed = []
    for p in iter_files(v2):
        rp = rel(p, v2)
        if rp not in old or old[rp] != sha256_file(p):
            changed.append(rp)

    def make_tar_zst(path: Path, root: Path, rels: list[str] | None) -> int:
        path.parent.mkdir(parents=True, exist_ok=True)
        cctx = zstd.ZstdCompressor(level=6, threads=-1)
        with path.open("wb") as raw:
            with cctx.stream_writer(raw) as zw:
                with tarfile.open(fileobj=zw, mode="w|") as tar:
                    files = rels if rels is not None else [rel(p, root) for p in iter_files(root)]
                    for rp in files:
                        tar.add(root / rp, arcname=rp, recursive=False)
        return path.stat().st_size

    changed_bytes = make_tar_zst(out / "changed_files_update.tar.zst", v2, changed)
    full_bytes = make_tar_zst(out / "full_v2.tar.zst", v2, None)
    return changed_bytes, full_bytes, "generated with python-zstandard"


def run_heavy_change_matrix(
    out_dir: str | Path,
    *,
    file_size: str = "128MiB",
    change_ratios: str | list[float] | list[str] = "1%,5%,10%,25%",
    modes: str | list[str] = "localized,distributed",
    profiles: str | list[str] = "large-file-balanced,large-file-large",
    codec: str = "raw",
    generate_tar_zst: bool = True,
    download_count: int = 1000,
    cost_per_gb: float = 0.05,
    currency: str = "USD",
) -> dict:
    out = Path(out_dir)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    ratios = _alpha25_parse_ratio_list(change_ratios)
    mode_list = _alpha25_parse_string_list(modes)
    allowed_modes = {"localized", "distributed", "rewrite-prefix"}
    for mode in mode_list:
        if mode not in allowed_modes:
            raise ValueError(f"unknown heavy-change mode {mode}; choose from {sorted(allowed_modes)}")

    size_bytes = parse_size(file_size)
    scenario_summaries = []
    rows = []

    for mode in mode_list:
        for ratio in ratios:
            scenario = f"alpha25-heavy-{mode}-{int(ratio*10000)/100:g}pct"
            pair = out / f"pair_{mode}_{int(ratio*10000)}"
            v1, v2 = _alpha25_make_heavy_pair(pair, file_size_bytes=size_bytes, ratio=ratio, mode=mode)

            tar_update = None
            tar_full = None
            baseline_note = None
            if generate_tar_zst:
                tar_update, tar_full, baseline_note = _alpha25_try_make_tar_zst_baselines(v1, v2, out / f"tar_zst_{mode}_{int(ratio*10000)}")

            report_dir = out / f"report_{mode}_{int(ratio*10000)}"
            res = run_standalone_baselines_bench(
                v1,
                v2,
                report_dir,
                profiles=profiles,
                codec=codec,
                scenario_name=scenario,
                scenario_kind=f"heavy-change-{mode}",
                scenario_note=f"Synthetic heavy-change scenario; mode={mode}; ratio={ratio:.4f}; generated by alpha25.",
                cost_per_gb=cost_per_gb,
                download_count=download_count,
                currency=currency,
                file_level_tar_zstd_bytes=tar_update,
                full_tar_zstd_v2_bytes=tar_full,
                external_baseline_note=baseline_note,
            )

            scenario_rows = []
            for row in res.get("comparison_rows", []):
                compact = {
                    "scenario": scenario,
                    "mode": mode,
                    "change_ratio": ratio,
                    "category": row.get("category"),
                    "method": row.get("method"),
                    "bytes": row.get("bytes"),
                    "gib": row.get("gib"),
                    "estimated_cost": row.get("estimated_cost"),
                    "currency": row.get("currency"),
                    "chunk_reuse_ratio": row.get("chunk_reuse_ratio"),
                    "pack_total_seconds": row.get("pack_total_seconds"),
                    "note": row.get("note"),
                }
                rows.append(compact)
                scenario_rows.append(compact)

            scenario_summaries.append({
                "scenario": scenario,
                "mode": mode,
                "change_ratio": ratio,
                "ok": bool(res.get("ok")),
                "file_level_tar_zstd_bytes": tar_update,
                "full_tar_zstd_v2_bytes": tar_full,
                "report_dir": str(report_dir),
                "rows": scenario_rows,
            })

            # Keep final package light.
            shutil.rmtree(pair, ignore_errors=True)
            for child in report_dir.iterdir():
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)

    csv_path = out / "heavy_change_summary.csv"
    if rows:
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

    matrix = {
        "schema": "CoreLangDistribution/HeavyChangeMatrix",
        "version": "2.0.0-alpha50.2",
        "ok": all(s.get("ok") for s in scenario_summaries),
        "file_size": file_size,
        "profiles": _parse_profile_list(profiles),
        "modes": mode_list,
        "change_ratios": ratios,
        "generate_tar_zst": generate_tar_zst,
        "scenario_count": len(scenario_summaries),
        "scenarios": scenario_summaries,
        "reports": {
            "json": str(out / "heavy_change_result.json"),
            "csv": str(csv_path),
            "markdown": str(out / "heavy_change_report.md"),
        },
    }
    (out / "heavy_change_result.json").write_text(json.dumps(matrix, indent=2, sort_keys=True), encoding="utf-8")

    lines = [
        "# CLD2 alpha25 heavy-change matrix",
        "",
        "This benchmark is a guardrail against overclaiming the favorable 99.9% reuse scenario.",
        "",
        "| Scenario | Method | Bytes | GiB | Cost | Chunk reuse | Pack total |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        reuse = "" if row.get("chunk_reuse_ratio") is None else _pct(float(row["chunk_reuse_ratio"]))
        pack = "" if row.get("pack_total_seconds") is None else f"{row['pack_total_seconds']} s"
        lines.append(
            f"| {row['scenario']} | {row['method']} | {row['bytes']} | "
            f"{float(row['gib']):.6f} | {float(row['estimated_cost']):.6f} {row['currency']} | {reuse} | {pack} |"
        )
    lines += [
        "",
        "## Interpretation",
        "",
        "The goal is not to beat every baseline in every scenario. The goal is to show how CLD2 behaves as the amount and distribution of change increases.",
        "",
        "For publication, keep both the favorable real astro benchmark and these heavier-change guardrails.",
    ]
    (out / "heavy_change_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    return {
        "ok": matrix["ok"],
        "schema": matrix["schema"],
        "version": matrix["version"],
        "scenario_count": matrix["scenario_count"],
        "file_size": file_size,
        "modes": mode_list,
        "change_ratios": ratios,
        "profiles": matrix["profiles"],
        "reports": matrix["reports"],
    }


# --- alpha27 hybrid planner, codec-aware -----------------------------------------

def _alpha27_parse_csv_list(value: str | list[str]) -> list[str]:
    if isinstance(value, str):
        return [x.strip() for x in value.split(",") if x.strip()]
    return [str(x).strip() for x in value if str(x).strip()]


def _alpha27_row_key(row: dict) -> str:
    codec = row.get("codec") or ""
    method = row.get("method") or ""
    return f"{codec}:{method}" if codec else method


def _alpha27_estimated_cost(bytes_n: int | float, *, download_count: int, cost_per_gb: float) -> float:
    return (float(bytes_n) / (1024 ** 3)) * float(download_count) * float(cost_per_gb)


def _alpha27_external_row(name: str, value: int | None, *, download_count: int, cost_per_gb: float, currency: str, note: str = "") -> dict | None:
    if value is None:
        return None
    v = int(value)
    return {
        "category": "external_baseline",
        "codec": "external",
        "method": name,
        "bytes": v,
        "gib": v / (1024 ** 3),
        "estimated_cost": _alpha27_estimated_cost(v, download_count=download_count, cost_per_gb=cost_per_gb),
        "currency": currency,
        "chunk_reuse_ratio": None,
        "pack_total_seconds": None,
        "note": note,
    }


def _alpha27_write_hybrid_report(result: dict, path: str | Path) -> None:
    p = Path(path)
    rows = result.get("comparison_rows", [])
    rec = result.get("recommendations", {})
    lines = [
        f"# CLD2 hybrid planner report - {result.get('scenario')}",
        "",
        "This report compares CLD2 profiles across codecs and file-level fallbacks.",
        "",
        "The goal is not to force CLD2 to win. The goal is to choose the smallest or most useful plan among:",
        "",
        "- fixed chunking;",
        "- FastCDC profiles;",
        "- raw/zstd/auto codecs;",
        "- file-level tar.zst fallback;",
        "- optional external rsync/zsync/casync baselines.",
        "",
        "## Recommendations",
        "",
    ]
    for key in ["smallest_bytes", "distribution_default", "speed_first", "bandwidth_first"]:
        item = rec.get(key)
        if item:
            lines.append(f"- **{key}**: `{item.get('plan')}` — {item.get('bytes')} bytes, codec `{item.get('codec')}`, method `{item.get('method')}`. Reason: {item.get('reason')}")
    lines += [
        "",
        "## Comparison table",
        "",
        "| Category | Codec | Method | Bytes | GiB | Cost | Chunk reuse | Pack total | Note |",
        "|---|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for r in rows:
        reuse = "" if r.get("chunk_reuse_ratio") is None else _pct(float(r["chunk_reuse_ratio"]))
        pack = "" if r.get("pack_total_seconds") is None else f"{float(r['pack_total_seconds']):.4f}s"
        lines.append(
            f"| {r.get('category')} | {r.get('codec')} | {r.get('method')} | {r.get('bytes')} | "
            f"{float(r.get('gib', 0)):.6f} | {float(r.get('estimated_cost', 0)):.6f} {r.get('currency','')} | "
            f"{reuse} | {pack} | {r.get('note','')} |"
        )
    missing = result.get("missing_external_baselines", [])
    lines += ["", "## Missing optional baselines", ""]
    if missing:
        for m in missing:
            lines.append(f"- {m}")
    else:
        lines.append("- none")
    lines += [
        "",
        "## Interpretation guardrail",
        "",
        "A hybrid planner is stronger than a single preferred algorithm. If `tar.zst` is smaller, the planner should say so. If `fixed+zstd` is best, it should choose that. If `FastCDC+zstd` is best, it should choose that.",
    ]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_hybrid_planner_bench(
    old_dir: str | Path,
    new_dir: str | Path,
    out_dir: str | Path,
    *,
    profiles: str | list[str] = "large-file-small,large-file-balanced,large-file-large",
    codecs: str | list[str] = "raw,zstd",
    scenario_name: str = "hybrid-planner",
    scenario_kind: str | None = None,
    scenario_note: str | None = None,
    download_count: int = 1000,
    cost_per_gb: float = 0.05,
    currency: str = "USD",
    file_level_tar_zstd_bytes: int | None = None,
    full_tar_zstd_v2_bytes: int | None = None,
    rsync_bytes: int | None = None,
    zsync_bytes: int | None = None,
    casync_bytes: int | None = None,
    external_baseline_note: str | None = None,
    download_tolerance_ratio: float = 0.10,
    download_tolerance_bytes: int = 256 * 1024 * 1024,
    speed_close_ratio: float = 0.05,
) -> dict:
    out = Path(out_dir)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    codec_list = _alpha27_parse_csv_list(codecs)
    profile_list = _parse_profile_list(profiles)

    all_rows: list[dict] = []
    per_codec: list[dict] = []

    # External rows are codec-independent and added once.
    for row in [
        {
            "category": "baseline",
            "codec": "external",
            "method": "file-level-raw-update",
            "bytes": 0,
            "gib": 0.0,
            "estimated_cost": 0.0,
            "currency": currency,
            "chunk_reuse_ratio": None,
            "pack_total_seconds": None,
            "note": "filled after first codec run",
        },
        _alpha27_external_row("file-level-tar.zst-update", file_level_tar_zstd_bytes, download_count=download_count, cost_per_gb=cost_per_gb, currency=currency, note=external_baseline_note or "external or precomputed baseline"),
        _alpha27_external_row("full-v2-tar.zst", full_tar_zstd_v2_bytes, download_count=download_count, cost_per_gb=cost_per_gb, currency=currency, note=external_baseline_note or "external or precomputed baseline"),
        _alpha27_external_row("rsync", rsync_bytes, download_count=download_count, cost_per_gb=cost_per_gb, currency=currency, note="external measured baseline supplied by user"),
        _alpha27_external_row("zsync", zsync_bytes, download_count=download_count, cost_per_gb=cost_per_gb, currency=currency, note="external measured baseline supplied by user"),
        _alpha27_external_row("casync", casync_bytes, download_count=download_count, cost_per_gb=cost_per_gb, currency=currency, note="external measured baseline supplied by user"),
    ]:
        if row:
            all_rows.append(row)

    raw_update_filled = False

    for codec in codec_list:
        report_dir = out / f"codec_{codec}"
        res = run_standalone_baselines_bench(
            old_dir,
            new_dir,
            report_dir,
            profiles=profile_list,
            codec=codec,
            scenario_name=f"{scenario_name}_{codec}",
            scenario_kind=scenario_kind,
            scenario_note=scenario_note,
            cost_per_gb=cost_per_gb,
            download_count=download_count,
            currency=currency,
            file_level_tar_zstd_bytes=file_level_tar_zstd_bytes,
            full_tar_zstd_v2_bytes=full_tar_zstd_v2_bytes,
            rsync_bytes=rsync_bytes,
            zsync_bytes=zsync_bytes,
            casync_bytes=casync_bytes,
            external_baseline_note=external_baseline_note,
            download_tolerance_ratio=download_tolerance_ratio,
            download_tolerance_bytes=download_tolerance_bytes,
        )
        per_codec.append({"codec": codec, "ok": bool(res.get("ok")), "report_dir": str(report_dir)})

        # Fill file-level raw once from baseline.
        if not raw_update_filled:
            b = int(res.get("baseline", {}).get("file_level_raw_download_bytes") or 0)
            for r in all_rows:
                if r.get("method") == "file-level-raw-update":
                    r["bytes"] = b
                    r["gib"] = b / (1024 ** 3)
                    r["estimated_cost"] = _alpha27_estimated_cost(b, download_count=download_count, cost_per_gb=cost_per_gb)
            raw_update_filled = True

        # Only keep CLD2 rows from each codec, to avoid duplicate external rows.
        for row in res.get("comparison_rows", []):
            if row.get("category") != "cld2":
                continue
            b = int(row.get("bytes") or 0)
            all_rows.append({
                "category": "cld2",
                "codec": codec,
                "method": row.get("method"),
                "bytes": b,
                "gib": b / (1024 ** 3),
                "estimated_cost": _alpha27_estimated_cost(b, download_count=download_count, cost_per_gb=cost_per_gb),
                "currency": currency,
                "chunk_reuse_ratio": row.get("chunk_reuse_ratio"),
                "pack_total_seconds": row.get("pack_total_seconds"),
                "note": "measured by CLD2 hybrid planner",
            })

    candidates = [r for r in all_rows if int(r.get("bytes") or 0) > 0]
    smallest = min(candidates, key=lambda r: int(r["bytes"])) if candidates else None

    # Distribution default: fastest CLD2 plan within tolerance of best CLD2 bytes.
    cld2_rows = [r for r in candidates if r.get("category") == "cld2"]
    best_cld2 = min(cld2_rows, key=lambda r: int(r["bytes"])) if cld2_rows else None
    distribution = best_cld2
    if best_cld2:
        best_bytes = int(best_cld2["bytes"])
        tolerance = max(int(best_bytes * (1.0 + download_tolerance_ratio)), best_bytes + int(download_tolerance_bytes))
        near = [r for r in cld2_rows if int(r["bytes"]) <= tolerance]
        near_with_time = [r for r in near if r.get("pack_total_seconds") is not None]
        if near_with_time:
            distribution = min(near_with_time, key=lambda r: float(r.get("pack_total_seconds") or 1e99))

    # Speed first: fastest measured CLD2 candidate.
    speed = None
    timed = [r for r in cld2_rows if r.get("pack_total_seconds") is not None]
    if timed:
        speed = min(timed, key=lambda r: float(r.get("pack_total_seconds") or 1e99))

    def rec_item(row: dict | None, reason: str) -> dict | None:
        if not row:
            return None
        return {
            "plan": _alpha27_row_key(row),
            "codec": row.get("codec"),
            "method": row.get("method"),
            "category": row.get("category"),
            "bytes": int(row.get("bytes") or 0),
            "gib": row.get("gib"),
            "estimated_cost": row.get("estimated_cost"),
            "chunk_reuse_ratio": row.get("chunk_reuse_ratio"),
            "pack_total_seconds": row.get("pack_total_seconds"),
            "reason": reason,
        }

    recommendations = {
        "smallest_bytes": rec_item(smallest, "smallest byte count among all available CLD2 and external fallback candidates"),
        "bandwidth_first": rec_item(best_cld2, "smallest byte count among measured CLD2 plans"),
        "distribution_default": rec_item(distribution, "fastest CLD2 plan within byte tolerance of the best CLD2 plan"),
        "speed_first": rec_item(speed, "fastest measured CLD2 plan regardless of bytes"),
    }

    missing = []
    if file_level_tar_zstd_bytes is None:
        missing.append("file-level tar.zst update baseline")
    if full_tar_zstd_v2_bytes is None:
        missing.append("full v2 tar.zst baseline")
    if rsync_bytes is None:
        missing.append("rsync measured baseline")
    if zsync_bytes is None:
        missing.append("zsync measured baseline")
    if casync_bytes is None:
        missing.append("casync measured baseline")

    csv_path = out / "hybrid_planner_summary.csv"
    if all_rows:
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(all_rows[0].keys()))
            w.writeheader()
            w.writerows(all_rows)

    result = {
        "schema": "CoreLangDistribution/HybridPlanner",
        "version": "2.0.0-alpha50.2",
        "ok": all(x.get("ok") for x in per_codec),
        "scenario": scenario_name,
        "scenario_kind": scenario_kind,
        "scenario_note": scenario_note,
        "profiles": profile_list,
        "codecs": codec_list,
        "per_codec": per_codec,
        "comparison_rows": all_rows,
        "recommendations": recommendations,
        "missing_external_baselines": missing,
        "reports": {
            "json": str(out / "hybrid_planner_result.json"),
            "csv": str(csv_path),
            "markdown": str(out / "hybrid_planner_report.md"),
        },
    }
    (out / "hybrid_planner_result.json").write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    _alpha27_write_hybrid_report(result, out / "hybrid_planner_report.md")
    return {
        "ok": result["ok"],
        "schema": result["schema"],
        "version": result["version"],
        "scenario": scenario_name,
        "profiles": profile_list,
        "codecs": codec_list,
        "recommendations": recommendations,
        "reports": result["reports"],
        "missing_external_baselines": missing,
    }


# --- alpha28 cost-aware policy ----------------------------------------------------

def _alpha28_transfer_cost_per_download(bytes_n: int | float, *, cost_per_gb: float) -> float:
    return (float(bytes_n) / (1024 ** 3)) * float(cost_per_gb)


def _alpha28_pack_cost(pack_seconds: float | None, *, pack_cost_per_hour: float) -> float:
    if pack_seconds is None:
        return 0.0
    return (float(pack_seconds) / 3600.0) * float(pack_cost_per_hour)


def _alpha28_enrich_rows_for_cost(
    rows: list[dict],
    *,
    download_count: int,
    cost_per_gb: float,
    pack_cost_per_hour: float,
) -> list[dict]:
    enriched = []
    for r in rows:
        b = int(r.get("bytes") or 0)
        pack_s = r.get("pack_total_seconds")
        pack_s_val = None if pack_s is None or pack_s == "" else float(pack_s)
        transfer_per_download = _alpha28_transfer_cost_per_download(b, cost_per_gb=cost_per_gb)
        transfer_total = transfer_per_download * int(download_count)
        pack_cost = _alpha28_pack_cost(pack_s_val, pack_cost_per_hour=pack_cost_per_hour)
        total = transfer_total + pack_cost
        nr = dict(r)
        nr["transfer_cost_per_download"] = transfer_per_download
        nr["transfer_cost_total"] = transfer_total
        nr["pack_cost"] = pack_cost
        nr["total_cost"] = total
        nr["pack_cost_per_hour"] = pack_cost_per_hour
        nr["download_count"] = download_count
        enriched.append(nr)
    return enriched


def _alpha28_row_plan(row: dict) -> str:
    return f"{row.get('codec')}:{row.get('method')}"


def _alpha28_make_rec(row: dict | None, reason: str) -> dict | None:
    if not row:
        return None
    return {
        "plan": _alpha28_row_plan(row),
        "category": row.get("category"),
        "codec": row.get("codec"),
        "method": row.get("method"),
        "bytes": int(row.get("bytes") or 0),
        "gib": row.get("gib"),
        "chunk_reuse_ratio": row.get("chunk_reuse_ratio"),
        "pack_total_seconds": row.get("pack_total_seconds"),
        "transfer_cost_total": row.get("transfer_cost_total"),
        "pack_cost": row.get("pack_cost"),
        "total_cost": row.get("total_cost"),
        "reason": reason,
    }


def _alpha28_recommend_cost_aware(enriched: list[dict], *, byte_tolerance_ratio: float = 0.05) -> dict:
    # alpha31.1: 0-byte CLD2 plans are valid. They can happen for metadata-only or rename/move scenarios
    # where all payload chunks are reused. Alpha31 filtered them out and marked rename/move inconclusive.
    candidates = [r for r in enriched if r.get("bytes") is not None]
    cld2 = [r for r in candidates if r.get("category") == "cld2"]
    timed = [r for r in cld2 if r.get("pack_total_seconds") is not None and r.get("pack_total_seconds") != ""]

    smallest = min(candidates, key=lambda r: int(r["bytes"])) if candidates else None
    cld2_smallest = min(cld2, key=lambda r: int(r["bytes"])) if cld2 else None
    speed = min(timed, key=lambda r: float(r["pack_total_seconds"])) if timed else None
    total_best = min(candidates, key=lambda r: float(r["total_cost"])) if candidates else None
    cld2_total_best = min(cld2, key=lambda r: float(r["total_cost"])) if cld2 else None

    # Mass distribution defaults to smallest transfer among all available plans.
    mass = smallest

    # Public distribution defaults to the smallest CLD2 transfer unless an external fallback is smaller.
    # This is intentionally bandwidth-oriented.
    public = smallest if smallest and smallest.get("category") != "cld2" else cld2_smallest

    # Balanced distribution: within a small byte tolerance of best CLD2, pick lowest total cost.
    balanced = cld2_smallest
    if cld2_smallest:
        best_b = int(cld2_smallest["bytes"])
        max_b = int(best_b * (1.0 + byte_tolerance_ratio))
        near = [r for r in cld2 if int(r["bytes"]) <= max_b]
        if near:
            balanced = min(near, key=lambda r: float(r["total_cost"]))

    return {
        "smallest_bytes": _alpha28_make_rec(smallest, "smallest transfer among all CLD2 and external candidates"),
        "bandwidth_first": _alpha28_make_rec(cld2_smallest, "smallest transfer among measured CLD2 candidates"),
        "mass_distribution": _alpha28_make_rec(mass, "smallest transfer; use when many downloads make bandwidth dominant"),
        "public_distribution": _alpha28_make_rec(public, "bandwidth-oriented default for public distribution"),
        "cost_aware_default": _alpha28_make_rec(total_best, "lowest total estimated cost: transfer cost plus one-time pack cost"),
        "cost_aware_cld2": _alpha28_make_rec(cld2_total_best, "lowest total estimated cost among CLD2 plans only"),
        "balanced_distribution": _alpha28_make_rec(balanced, "lowest total cost among CLD2 plans within a tight byte tolerance of best CLD2 transfer"),
        "speed_first": _alpha28_make_rec(speed, "fastest measured CLD2 plan regardless of transfer size"),
    }


def _alpha28_write_cost_report(result: dict, path: str | Path) -> None:
    p = Path(path)
    rec = result.get("cost_aware_recommendations", {})
    rows = result.get("cost_rows", [])
    lines = [
        f"# CLD2 cost-aware planner report - {result.get('scenario')}",
        "",
        "This report explains the cost-aware planner: it is byte-aware, cost-aware and exposes break-even thresholds.",
        "",
        "It estimates:",
        "",
        "- transfer cost across the configured number of downloads;",
        "- one-time pack/build cost using an hourly build cost;",
        "- total cost = transfer cost + pack cost.",
        "",
        "## Parameters",
        "",
        f"- download_count: {result.get('download_count')}",
        f"- cost_per_gb: {result.get('cost_per_gb')} {result.get('currency')}",
        f"- pack_cost_per_hour: {result.get('pack_cost_per_hour')} {result.get('currency')}/hour",
        "",
        "## Recommendations",
        "",
    ]
    for key in [
        "smallest_bytes",
        "bandwidth_first",
        "mass_distribution",
        "public_distribution",
        "cost_aware_default",
        "cost_aware_cld2",
        "balanced_distribution",
        "speed_first",
    ]:
        item = rec.get(key)
        if item:
            lines.append(
                f"- **{key}**: `{item.get('plan')}` — {item.get('bytes')} bytes, "
                f"total cost {float(item.get('total_cost') or 0):.6f} {result.get('currency')}. "
                f"Reason: {item.get('reason')}"
            )
    lines += [
        "",
        "## Cost table",
        "",
        "| Category | Plan | Bytes | GiB | Pack seconds | Transfer cost | Pack cost | Total cost |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        plan = _alpha28_row_plan(r)
        pack = "" if r.get("pack_total_seconds") in (None, "") else f"{float(r.get('pack_total_seconds')):.4f}"
        lines.append(
            f"| {r.get('category')} | {plan} | {r.get('bytes')} | {float(r.get('gib') or 0):.6f} | "
            f"{pack} | {float(r.get('transfer_cost_total') or 0):.6f} | "
            f"{float(r.get('pack_cost') or 0):.6f} | {float(r.get('total_cost') or 0):.6f} |"
        )
    lines += [
        "",
        "## Break-even downloads",
        "",
    ]
    be = result.get("break_even", {})
    if be:
        lines += [
            "| Comparison | From | To | Break-even downloads | Note |",
            "|---|---|---|---:|---|",
        ]
        for key, item in be.items():
            threshold = item.get("break_even_download_count")
            threshold_s = "∞" if threshold is None else str(threshold)
            lines.append(
                f"| {key} | `{item.get('from_plan')}` | `{item.get('to_plan')}` | {threshold_s} | {item.get('relation')} |"
            )
    else:
        lines.append("No break-even threshold could be computed from the available rows.")
    lines += [
        "",
        "## Interpretation",
        "",
        "For internal builds with few downloads, speed-first can make sense.",
        "",
        "For public or mass distribution, the smaller zstd plan often becomes the better default because the one-time pack cost is paid once while transfer cost scales with downloads.",
        "",
        "The planner should expose multiple recommendations instead of pretending a single strategy is always best.",
    ]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _alpha29_break_even_downloads(faster_or_cheaper_pack: dict | None, smaller_transfer: dict | None) -> dict | None:
    """Return the download count where smaller_transfer becomes cheaper.

    Cost model: total = pack_cost + download_count * transfer_cost_per_download.
    The function is intentionally small and report-friendly: it does not mutate rows.
    """
    if not faster_or_cheaper_pack or not smaller_transfer:
        return None
    a = faster_or_cheaper_pack
    b = smaller_transfer
    a_plan = _alpha28_row_plan(a)
    b_plan = _alpha28_row_plan(b)
    a_pack = float(a.get("pack_cost") or 0.0)
    b_pack = float(b.get("pack_cost") or 0.0)
    a_transfer = float(a.get("transfer_cost_per_download") or 0.0)
    b_transfer = float(b.get("transfer_cost_per_download") or 0.0)

    # b is already no worse in both dimensions.
    if b_pack <= a_pack and b_transfer <= a_transfer:
        threshold = 0
        relation = "smaller-transfer plan is already cheaper or tied"
    # b can only recover its higher pack cost if it saves transfer cost per download.
    elif b_transfer < a_transfer:
        threshold = max(0, int(math.ceil((b_pack - a_pack) / (a_transfer - b_transfer))))
        relation = "smaller-transfer plan becomes cheaper at or above this download count"
    else:
        threshold = None
        relation = "no finite break-even: smaller-transfer plan does not reduce per-download cost"

    return {
        "from_plan": a_plan,
        "to_plan": b_plan,
        "break_even_download_count": threshold,
        "relation": relation,
        "from_pack_cost": a_pack,
        "to_pack_cost": b_pack,
        "from_transfer_cost_per_download": a_transfer,
        "to_transfer_cost_per_download": b_transfer,
    }


def _alpha29_compute_break_even(enriched: list[dict], recommendations: dict) -> dict:
    """Compute practical break-even thresholds from cost-aware rows."""
    by_plan = {_alpha28_row_plan(r): r for r in enriched}

    def row_from_rec(key: str) -> dict | None:
        rec = recommendations.get(key) or {}
        return by_plan.get(rec.get("plan"))

    speed = row_from_rec("speed_first")
    bandwidth = row_from_rec("bandwidth_first")
    smallest = row_from_rec("smallest_bytes")
    balanced = row_from_rec("balanced_distribution")
    cost_default = row_from_rec("cost_aware_default")

    out = {
        "speed_first_vs_bandwidth_first_cld2": _alpha29_break_even_downloads(speed, bandwidth),
        "speed_first_vs_smallest_bytes": _alpha29_break_even_downloads(speed, smallest),
        "speed_first_vs_balanced_distribution": _alpha29_break_even_downloads(speed, balanced),
    }
    if cost_default and bandwidth and _alpha28_row_plan(cost_default) != _alpha28_row_plan(bandwidth):
        out["cost_default_vs_bandwidth_first_cld2"] = _alpha29_break_even_downloads(cost_default, bandwidth)
    return {k: v for k, v in out.items() if v is not None}


def _alpha29_write_scenario_report(result: dict, path: str | Path) -> None:
    p = Path(path)
    scenarios = result.get("scenarios", {})
    lines = [
        f"# CLD2 cost-aware scenarios - {result.get('scenario')}",
        "",
        "This report reuses one measured hybrid benchmark and rescales the same candidates for multiple download counts.",
        "",
        "## Parameters",
        "",
        f"- cost_per_gb: {result.get('cost_per_gb')} {result.get('currency')}",
        f"- pack_cost_per_hour: {result.get('pack_cost_per_hour')} {result.get('currency')}/hour",
        "",
        "## Scenario recommendations",
        "",
        "| Scenario | Downloads | cost_aware_default | public_distribution | speed_first | bandwidth_first |",
        "|---|---:|---|---|---|---|",
    ]
    for name, sc in scenarios.items():
        rec = sc.get("cost_aware_recommendations", {})
        def plan(key: str) -> str:
            item = rec.get(key) or {}
            return f"`{item.get('plan')}`" if item.get("plan") else ""
        lines.append(
            f"| {name} | {sc.get('download_count')} | {plan('cost_aware_default')} | {plan('public_distribution')} | {plan('speed_first')} | {plan('bandwidth_first')} |"
        )
    lines += ["", "## Break-even downloads", ""]
    first = next(iter(scenarios.values()), {}) if scenarios else {}
    be = first.get("break_even", {})
    if be:
        lines += [
            "| Comparison | From | To | Break-even downloads | Note |",
            "|---|---|---|---:|---|",
        ]
        for key, item in be.items():
            threshold = item.get("break_even_download_count")
            threshold_s = "∞" if threshold is None else str(threshold)
            lines.append(
                f"| {key} | `{item.get('from_plan')}` | `{item.get('to_plan')}` | {threshold_s} | {item.get('relation')} |"
            )
    else:
        lines.append("No break-even threshold could be computed from the available rows.")
    lines += [
        "",
        "## Interpretation",
        "",
        "Use the internal scenario when few clients/downloads are expected and build time matters.",
        "",
        "Use the public/massive scenarios when the same release will be downloaded many times and bandwidth dominates.",
    ]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _alpha29_write_cost_csv(rows: list[dict], path: str | Path) -> None:
    if not rows:
        return
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def make_light_zip(src_dir: str | Path, zip_out: str | Path, *, max_mb: float = 50.0) -> dict:
    """Create a report-only ZIP, preserving relative paths to avoid duplicate root names."""
    src = Path(src_dir)
    out = Path(zip_out)
    allowed = {".json", ".csv", ".md", ".txt", ".log", ".sha256"}
    max_bytes = int(float(max_mb) * 1024 * 1024)
    files: list[Path] = []
    skipped_large: list[dict] = []
    if not src.exists():
        raise FileNotFoundError(src)
    for p in src.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() not in allowed:
            continue
        if p.stat().st_size > max_bytes:
            skipped_large.append({"path": str(p), "bytes": p.stat().st_size})
            continue
        files.append(p)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(files):
            zf.write(p, p.relative_to(src).as_posix())
    return {
        "ok": True,
        "schema": "CoreLangDistribution/LightZip",
        "version": "2.0.0-alpha50.2",
        "src_dir": str(src),
        "zip_out": str(out),
        "files_included": len(files),
        "skipped_large": skipped_large,
        "bytes": out.stat().st_size if out.exists() else 0,
    }






def _alpha32_safe_float(value, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _alpha32_safe_int(value, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except Exception:
        return default


def _alpha32_fmt_bytes(value) -> str:
    n = _alpha32_safe_int(value)
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    x = float(n)
    unit = units[0]
    for unit in units:
        if abs(x) < 1024.0 or unit == units[-1]:
            break
        x /= 1024.0
    if unit == "B":
        return f"{n} B"
    return f"{x:.2f} {unit}"


def _alpha32_fmt_money(value, currency: str = "") -> str:
    try:
        x = float(value)
        return f"{x:,.6f} {currency}".strip()
    except Exception:
        return ""


def _alpha32_load_csv_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _alpha32_badge(label: str) -> str:
    label = (label or "unknown").strip().lower()
    css = {
        "win": "win",
        "loss": "loss",
        "near_tie": "tie",
        "inconclusive": "unknown",
    }.get(label, "unknown")
    return f'<span class="badge {css}">{label}</span>'



def _alpha33_write_network_pair(root: Path, *, file_size: str | int = "8MiB") -> tuple[Path, Path]:
    """Create a small deterministic old/new pair for the alpha33 HTTP pilot."""
    size = parse_size(file_size)
    v1 = root / "v1"
    v2 = root / "v2"
    if root.exists():
        shutil.rmtree(root)
    v1.mkdir(parents=True)
    v2.mkdir(parents=True)
    # Structured/repetitive content so delta/reuse is visible without huge files.
    block = (b"CLD2_ALPHA33_HTTP_RANGE_ETAG_PILOT\n" * 1024)
    with (v1 / "data.bin").open("wb") as f:
        remaining = size
        while remaining > 0:
            chunk = block[: min(len(block), remaining)]
            f.write(chunk)
            remaining -= len(chunk)
    (v1 / "config").mkdir()
    (v1 / "config" / "settings.json").write_text(json.dumps({"version": 1, "mode": "old", "features": ["range", "etag"]}, indent=2) + "\n", encoding="utf-8")
    shutil.copytree(v1, v2, dirs_exist_ok=True)
    # Localized binary change plus small metadata change.
    blob = bytearray((v2 / "data.bin").read_bytes())
    start = max(0, len(blob) // 2 - 2048)
    patch = b"CLD2_ALPHA33_LOCALIZED_UPDATE" * 128
    blob[start:start + len(patch)] = patch
    (v2 / "data.bin").write_bytes(bytes(blob))
    (v2 / "config" / "settings.json").write_text(json.dumps({"version": 2, "mode": "new", "features": ["range", "etag", "resume"]}, indent=2) + "\n", encoding="utf-8")
    return v1, v2


class _Alpha33HTTPServer:
    def __init__(self, directory: Path, bind: str = "127.0.0.1", port: int = 0):
        self.directory = Path(directory)
        RangeRequestHandler.fail_every = 0
        RangeRequestHandler.fail_status = 503
        RangeRequestHandler.truncate_every = 0
        RangeRequestHandler.delay_ms = 0
        RangeRequestHandler.request_counter = 0
        import functools
        handler = functools.partial(RangeRequestHandler, directory=str(self.directory))
        self.httpd = ThreadingHTTPServer((bind, int(port)), handler)
        self.bind = bind
        self.url_host = url_host or ("127.0.0.1" if bind in ("0.0.0.0", "::") else bind)
        self.port = int(self.httpd.server_address[1])
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="cld2-alpha33-http", daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://{self.url_host}:{self.port}/"

    def __enter__(self):
        self.thread.start()
        # Give the server a tiny moment to bind on slower Windows machines.
        time.sleep(0.05)
        return self

    def __exit__(self, exc_type, exc, tb):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2.0)


def _alpha33_network_report_markdown(result: dict) -> str:
    fetch_v1 = result.get("fetch_v1", {})
    fetch_v2 = result.get("fetch_v2", {})
    audit_v2 = result.get("audit_v2", {})
    net_v1 = fetch_v1.get("network") or {}
    net_v2 = fetch_v2.get("network") or {}
    return f"""# CLD2 alpha33 HTTP pilot report

## Summary

- ok: `{result.get('ok')}`
- server_url: `{result.get('server_url')}`
- repo_v1_url: `{result.get('repo_v1_url')}`
- repo_v2_url: `{result.get('repo_v2_url')}`
- file_size: `{result.get('file_size')}`
- codec: `{result.get('codec')}`
- chunker: `{result.get('chunker')}`

## Fetch/install results

| Step | OK | Downloaded chunks | Cache-hit chunks | Downloaded pack bytes | Reused cache raw bytes | Range requests | HEAD requests | If-Range used | ETag observed |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| v1 initial HTTP fetch | {fetch_v1.get('ok')} | {fetch_v1.get('downloaded_chunks')} | {fetch_v1.get('cache_hit_chunks')} | {fetch_v1.get('downloaded_pack_bytes')} | {fetch_v1.get('reused_cache_raw_bytes')} | {net_v1.get('range_requests')} | {net_v1.get('head_requests')} | {net_v1.get('if_range_used')} | {net_v1.get('etag_observed')} |
| v2 HTTP update from installed v1 | {fetch_v2.get('ok')} | {fetch_v2.get('downloaded_chunks')} | {fetch_v2.get('cache_hit_chunks')} | {fetch_v2.get('downloaded_pack_bytes')} | {fetch_v2.get('reused_cache_raw_bytes')} | {net_v2.get('range_requests')} | {net_v2.get('head_requests')} | {net_v2.get('if_range_used')} | {net_v2.get('etag_observed')} |

## Verify/audit

- verify_v1_remote_ok: `{(result.get('verify_v1_remote') or {}).get('ok')}`
- verify_v2_remote_ok: `{(result.get('verify_v2_remote') or {}).get('ok')}`
- audit_v2_ok: `{audit_v2.get('ok')}`
- files_expected: `{audit_v2.get('files_expected')}`
- files_ok: `{audit_v2.get('files_ok')}`
- missing_count: `{audit_v2.get('missing_count')}`
- corrupt_count: `{audit_v2.get('corrupt_count')}`

## Interpretation

This is a local HTTP pilot, not a CDN benchmark. It checks that CLD2 can publish a repo through an HTTP server and fetch/install it through HTTP metadata + Range/ETag-aware pack reads. The important success signal is not speed; it is correct remote fetch, hash verification, cache reuse and a clean review package.
"""


def _alpha33_network_report_html(result: dict) -> str:
    import html
    report_md = _alpha33_network_report_markdown(result)
    rows = []
    for name in ["fetch_v1", "fetch_v2"]:
        f = result.get(name, {})
        net = f.get("network") or {}
        rows.append(
            "<tr>"
            f"<td><code>{html.escape(name)}</code></td>"
            f"<td>{html.escape(str(f.get('ok')))}</td>"
            f"<td>{html.escape(str(f.get('downloaded_chunks')))}</td>"
            f"<td>{html.escape(str(f.get('cache_hit_chunks')))}</td>"
            f"<td>{html.escape(str(f.get('downloaded_pack_bytes')))}</td>"
            f"<td>{html.escape(str(f.get('reused_cache_raw_bytes')))}</td>"
            f"<td>{html.escape(str(net.get('range_requests')))}</td>"
            f"<td>{html.escape(str(net.get('head_requests')))}</td>"
            f"<td>{html.escape(str(net.get('if_range_used')))}</td>"
            f"<td>{html.escape(str(net.get('etag_observed')))}</td>"
            "</tr>"
        )
    ok_class = "ok" if result.get("ok") else "bad"
    return f"""<!doctype html>
<html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>CLD2 alpha33 HTTP pilot</title>
<style>body{{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:2rem;background:#fafafa;color:#111}}code{{background:#f3f3f3;padding:.12rem .25rem;border-radius:4px}}table{{border-collapse:collapse;width:100%;background:white}}th,td{{border:1px solid #ddd;padding:.55rem;text-align:left}}th{{background:#eee}}.badge{{display:inline-block;padding:.25rem .7rem;border-radius:999px;font-weight:700}}.ok{{background:#e7f7ed;color:#126b35}}.bad{{background:#fdeaea;color:#8b1a1a}}.card{{background:white;border:1px solid #ddd;border-radius:12px;padding:1rem;margin:1rem 0}}</style></head>
<body><h1>CLD2 alpha33 HTTP pilot</h1><p>Status: <span class=\"badge {ok_class}\">{html.escape(str(result.get('ok')))}</span></p>
<div class=\"card\"><p>Server: <code>{html.escape(str(result.get('server_url')))}</code></p><p>Repo v1: <code>{html.escape(str(result.get('repo_v1_url')))}</code></p><p>Repo v2: <code>{html.escape(str(result.get('repo_v2_url')))}</code></p></div>
<table><thead><tr><th>Step</th><th>OK</th><th>Downloaded chunks</th><th>Cache-hit chunks</th><th>Downloaded pack bytes</th><th>Reused cache raw bytes</th><th>Range requests</th><th>HEAD requests</th><th>If-Range used</th><th>ETag observed</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
<h2>Interpretation</h2><p>This is a local HTTP pilot, not a CDN benchmark. It checks HTTP metadata, Range/ETag-aware pack reads, install verification and cache reuse.</p>
<h2>Markdown source</h2><pre>{html.escape(report_md)}</pre>
</body></html>"""


def run_network_pilot(
    out_dir: str | Path,
    *,
    file_size: str = "8MiB",
    bind: str = "127.0.0.1",
    port: int = 0,
    codec: str = "zstd",
    chunker: str = "fastcdc",
    fixed_size: str = "1MiB",
    chunk_min: str = "64KiB",
    chunk_avg: str = "256KiB",
    chunk_max: str = "1MiB",
    fastcdc_stride: int = 4,
    http_retries: int = 3,
    http_backoff: float = 0.05,
    parallel: int = 1,
    cld2_attach_retries: int = 4,
    cld2_attach_backoff: float = 2.0,
    make_review: bool = False,
    keep_heavy: bool = True,
    max_mb: float = 10.0,
) -> dict:
    """Run a reproducible alpha33 local HTTP Range/ETag pilot.

    The pilot creates v1/v2 repos locally, serves them through the built-in RangeRequestHandler,
    then fetches v1 and updates to v2 through HTTP using the same cache and from-installed seeding.
    """
    out = Path(out_dir)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    pair_root = out / "pairs"
    v1, v2 = _alpha33_write_network_pair(pair_root, file_size=file_size)
    repos = out / "repos"
    repos.mkdir(parents=True)
    repo_v1 = repos / "release_v1.cldrepo"
    repo_v2 = repos / "release_v2.cldrepo"
    make_repo(v1, repo_v1, release_id="alpha33-http-v1", release_seq=1, chunker=chunker, fixed_size=fixed_size, chunk_min=chunk_min, chunk_avg=chunk_avg, chunk_max=chunk_max, fastcdc_stride=fastcdc_stride, codec=codec, force=True)
    make_repo(v2, repo_v2, release_id="alpha33-http-v2", release_seq=2, chunker=chunker, fixed_size=fixed_size, chunk_min=chunk_min, chunk_avg=chunk_avg, chunk_max=chunk_max, fastcdc_stride=fastcdc_stride, codec=codec, force=True)
    local_diff = diff_repos(repo_v1, repo_v2)
    cache = out / "cache"
    install_v1 = out / "install_v1"
    install_v2 = out / "install_v2"
    with _Alpha33HTTPServer(repos, bind=bind, port=port) as srv:
        repo_v1_url = srv.base_url + "release_v1.cldrepo/"
        repo_v2_url = srv.base_url + "release_v2.cldrepo/"
        verify_v1 = verify_repo(repo_v1_url, deep=False)
        verify_v2 = verify_repo(repo_v2_url, deep=False)
        fetch_v1 = fetch_install(repo_v1_url, install_v1, cache_dir=cache, verify=True, http_retries=http_retries, http_backoff=http_backoff, parallel=parallel)
        fetch_v2 = fetch_install(repo_v2_url, install_v2, cache_dir=cache, from_installed=install_v1, verify=True, http_retries=http_retries, http_backoff=http_backoff, parallel=parallel)
        audit_v2 = audit_install(repo_v2_url, install_v2)
        result = {
            "schema": "CoreLangDistribution/NetworkPilot",
            "version": "2.0-alpha33",
            "ok": bool(fetch_v1.get("ok") and fetch_v2.get("ok") and audit_v2.get("ok") and verify_v1.get("ok") and verify_v2.get("ok")),
            "out_dir": str(out),
            "file_size": file_size,
            "codec": codec,
            "chunker": chunker,
            "server_url": srv.base_url,
            "repo_v1_url": repo_v1_url,
            "repo_v2_url": repo_v2_url,
            "repo_v1_local": str(repo_v1),
            "repo_v2_local": str(repo_v2),
            "local_diff": local_diff,
            "verify_v1_remote": verify_v1,
            "verify_v2_remote": verify_v2,
            "fetch_v1": fetch_v1,
            "fetch_v2": fetch_v2,
            "audit_v2": audit_v2,
        }
    (out / "network_pilot_result.json").write_text(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    (out / "network_pilot_report.md").write_text(_alpha33_network_report_markdown(result), encoding="utf-8")
    (out / "index.html").write_text(_alpha33_network_report_html(result), encoding="utf-8")
    # Compact CSV for quick inspection.
    with (out / "network_pilot_summary.csv").open("w", newline="", encoding="utf-8") as f:
        fieldnames = ["step", "ok", "downloaded_chunks", "cache_hit_chunks", "downloaded_pack_bytes", "reused_cache_raw_bytes", "range_requests", "head_requests", "if_range_used", "etag_observed"]
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for step in ["fetch_v1", "fetch_v2"]:
            fr = result.get(step, {})
            net = fr.get("network") or {}
            w.writerow({
                "step": step,
                "ok": fr.get("ok"),
                "downloaded_chunks": fr.get("downloaded_chunks"),
                "cache_hit_chunks": fr.get("cache_hit_chunks"),
                "downloaded_pack_bytes": fr.get("downloaded_pack_bytes"),
                "reused_cache_raw_bytes": fr.get("reused_cache_raw_bytes"),
                "range_requests": net.get("range_requests"),
                "head_requests": net.get("head_requests"),
                "if_range_used": net.get("if_range_used"),
                "etag_observed": net.get("etag_observed"),
            })
    if not keep_heavy:
        for heavy in [pair_root, repos, cache, install_v1, install_v2]:
            shutil.rmtree(heavy, ignore_errors=True)
    if make_review:
        result["review_zip"] = make_review_zip(out, str(out) + "_REVIEW.zip", max_mb=max_mb)
        # alpha34.1: ensure mirror pilot machine-readable reports are included.
        try:
            with zipfile.ZipFile(result["review_zip"], "a", compression=zipfile.ZIP_DEFLATED) as _zf:
                for _name in ["mirror_pilot_result.json", "mirror_pilot_summary.csv", "mirror_pilot_report.md"]:
                    _p = out / _name
                    if _p.exists() and _name not in _zf.namelist():
                        _zf.write(_p, arcname=_name)
        except Exception:
            pass
        (out / "network_pilot_result.json").write_text(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    return result


class _Alpha34HTTPServer:
    """Independent Range/ETag test server with per-instance fault injection."""
    def __init__(
        self,
        directory: Path,
        *,
        bind: str = "127.0.0.1",
        port: int = 0,
        name: str = "mirror",
        fail_every: int = 0,
        fail_status: int = 503,
        truncate_every: int = 0,
        delay_ms: int = 0,
    ):
        self.directory = Path(directory)
        self.bind = bind
        self.name = name
        import functools

        class Alpha34RangeHandler(RangeRequestHandler):
            pass

        Alpha34RangeHandler.fail_every = int(fail_every or 0)
        Alpha34RangeHandler.fail_status = int(fail_status or 503)
        Alpha34RangeHandler.truncate_every = int(truncate_every or 0)
        Alpha34RangeHandler.delay_ms = int(delay_ms or 0)
        Alpha34RangeHandler.request_counter = 0

        handler = functools.partial(Alpha34RangeHandler, directory=str(self.directory))
        self.httpd = ThreadingHTTPServer((bind, int(port)), handler)
        self.port = int(self.httpd.server_address[1])
        self.thread = threading.Thread(target=self.httpd.serve_forever, name=f"cld2-alpha34-{name}", daemon=True)
        self.handler_class = Alpha34RangeHandler
        self.config = {
            "name": name,
            "directory": str(self.directory),
            "bind": bind,
            "port": self.port,
            "fail_every": int(fail_every or 0),
            "fail_status": int(fail_status or 503),
            "truncate_every": int(truncate_every or 0),
            "delay_ms": int(delay_ms or 0),
        }

    @property
    def base_url(self) -> str:
        return f"http://{self.url_host}:{self.port}/"

    def __enter__(self):
        self.thread.start()
        time.sleep(0.05)
        return self

    def __exit__(self, exc_type, exc, tb):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2.0)


def _alpha34_mirror_report_markdown(result: dict) -> str:
    f1 = result.get("fetch_v1", {})
    f2 = result.get("fetch_v2", {})
    n1 = f1.get("network") or {}
    n2 = f2.get("network") or {}
    audit = result.get("audit_v2") or {}

    def mirror_line(net: dict) -> str:
        mirrors = net.get("mirrors") or {}
        if not mirrors:
            return "_none_"
        lines = []
        for key, value in sorted(mirrors.items()):
            lines.append(f"- `{key}`: successes={value.get('successes', 0)} failures={value.get('failures', 0)} score={value.get('score', 0)} blacklists={value.get('blacklist_events', 0)} last_error=`{value.get('last_error', '')}`")
        return "\n".join(lines)

    return f"""# CLD2 alpha34 mirror robustness report

## Summary

- ok: `{result.get('ok')}`
- primary_url: `{result.get('primary_url')}`
- mirror_url: `{result.get('mirror_url')}`
- file_size: `{result.get('file_size')}`
- codec: `{result.get('codec')}`
- chunker: `{result.get('chunker')}`
- mirror_policy: `{result.get('mirror_policy')}`
- primary_truncate_every: `{result.get('primary_truncate_every')}`

## Fetch/install results

| Step | OK | Downloaded chunks | Cache-hit chunks | Downloaded pack bytes | Reused cache raw bytes | Range requests | Range retries | Range failures | If-Range used | ETag observed | Mirror blacklist events |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| v1 initial HTTP fetch via degraded primary + mirror | {f1.get('ok')} | {f1.get('downloaded_chunks')} | {f1.get('cache_hit_chunks')} | {f1.get('downloaded_pack_bytes')} | {f1.get('reused_cache_raw_bytes')} | {n1.get('range_requests')} | {n1.get('range_retries')} | {n1.get('range_failures')} | {n1.get('if_range_used')} | {n1.get('etag_observed')} | {n1.get('mirror_blacklist_events', 0)} |
| v2 HTTP update via degraded primary + mirror | {f2.get('ok')} | {f2.get('downloaded_chunks')} | {f2.get('cache_hit_chunks')} | {f2.get('downloaded_pack_bytes')} | {f2.get('reused_cache_raw_bytes')} | {n2.get('range_requests')} | {n2.get('range_retries')} | {n2.get('range_failures')} | {n2.get('if_range_used')} | {n2.get('etag_observed')} | {n2.get('mirror_blacklist_events', 0)} |

## Mirror stats: fetch_v1

{mirror_line(n1)}

## Mirror stats: fetch_v2

{mirror_line(n2)}

## Verify/audit

- audit_v2_ok: `{audit.get('ok')}`
- files_expected: `{audit.get('files_expected')}`
- files_ok: `{audit.get('files_ok')}`
- missing_count: `{audit.get('missing_count')}`
- corrupt_count: `{audit.get('corrupt_count')}`

## Interpretation

This is a local robustness pilot, not a CDN benchmark. The primary mirror intentionally returns truncated Range responses. The success condition is that CLD2 still completes remote fetch/install/update by falling back to the healthy mirror, verifies the installation, and records enough network/mirror stats to explain what happened.
"""


def _alpha34_mirror_report_html(result: dict) -> str:
    import html
    md = _alpha34_mirror_report_markdown(result)
    rows = []
    for name in ["fetch_v1", "fetch_v2"]:
        f = result.get(name, {})
        net = f.get("network") or {}
        rows.append(
            "<tr>"
            f"<td><code>{html.escape(name)}</code></td>"
            f"<td>{html.escape(str(f.get('ok')))}</td>"
            f"<td>{html.escape(str(f.get('downloaded_chunks')))}</td>"
            f"<td>{html.escape(str(f.get('cache_hit_chunks')))}</td>"
            f"<td>{html.escape(str(f.get('downloaded_pack_bytes')))}</td>"
            f"<td>{html.escape(str(f.get('reused_cache_raw_bytes')))}</td>"
            f"<td>{html.escape(str(net.get('range_requests')))}</td>"
            f"<td>{html.escape(str(net.get('range_retries')))}</td>"
            f"<td>{html.escape(str(net.get('range_failures')))}</td>"
            f"<td>{html.escape(str(net.get('mirror_blacklist_events', 0)))}</td>"
            "</tr>"
        )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>CLD2 alpha34 mirror robustness</title>
<style>
body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:2rem;line-height:1.45}}
table{{border-collapse:collapse;width:100%;margin:1rem 0}}
td,th{{border:1px solid #ddd;padding:.45rem;text-align:left}}
th{{background:#f4f4f4}}
code{{background:#f6f6f6;padding:.1rem .25rem;border-radius:.25rem}}
pre{{background:#111;color:#eee;padding:1rem;overflow:auto}}
.card{{border:1px solid #ddd;border-radius:.75rem;padding:1rem;margin:1rem 0}}
</style></head>
<body>
<h1>CLD2 alpha34 mirror robustness</h1>
<div class="card">
<p><b>Status:</b> {html.escape(str(result.get('ok')))}</p>
<p><b>Primary:</b> <code>{html.escape(str(result.get('primary_url')))}</code></p>
<p><b>Mirror:</b> <code>{html.escape(str(result.get('mirror_url')))}</code></p>
<p><b>Policy:</b> <code>{html.escape(str(result.get('mirror_policy')))}</code></p>
</div>
<table>
<thead><tr><th>Step</th><th>OK</th><th>Downloaded chunks</th><th>Cache-hit chunks</th><th>Downloaded pack bytes</th><th>Reused cache raw bytes</th><th>Range requests</th><th>Range retries</th><th>Range failures</th><th>Blacklist events</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
<h2>Interpretation</h2>
<p>The primary mirror intentionally returns truncated Range responses. A successful run means CLD2 completed remote fetch/install/update via the healthy mirror and recorded mirror stats.</p>
<h2>Markdown source</h2>
<pre>{html.escape(md)}</pre>
</body></html>
"""


def run_mirror_pilot(
    out_dir: str | Path,
    *,
    file_size: str = "8MiB",
    bind: str = "127.0.0.1",
    primary_port: int = 0,
    mirror_port: int = 0,
    codec: str = "zstd",
    chunker: str = "fastcdc",
    fixed_size: str = "1MiB",
    chunk_min: str = "64KiB",
    chunk_avg: str = "256KiB",
    chunk_max: str = "1MiB",
    fastcdc_stride: int = 4,
    http_retries: int = 1,
    http_backoff: float = 0.02,
    parallel: int = 1,
    primary_truncate_every: int = 1,
    primary_delay_ms: int = 0,
    mirror_delay_ms: int = 0,
    mirror_policy: str = "ordered",
    hedge_delay: float = 0.025,
    make_review: bool = False,
    keep_heavy: bool = True,
    max_mb: float = 10.0,
) -> dict:
    """Run alpha34 local degraded-primary + healthy-mirror HTTP pilot."""
    out = Path(out_dir)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    pair_root = out / "pairs"
    v1, v2 = _alpha33_write_network_pair(pair_root, file_size=file_size)

    repos_src = out / "repos_src"
    repos_src.mkdir(parents=True)
    repo_v1_src = repos_src / "release_v1.cldrepo"
    repo_v2_src = repos_src / "release_v2.cldrepo"
    make_repo(v1, repo_v1_src, release_id="alpha34-mirror-v1", release_seq=1, chunker=chunker, fixed_size=fixed_size, chunk_min=chunk_min, chunk_avg=chunk_avg, chunk_max=chunk_max, fastcdc_stride=fastcdc_stride, codec=codec, force=True)
    make_repo(v2, repo_v2_src, release_id="alpha34-mirror-v2", release_seq=2, chunker=chunker, fixed_size=fixed_size, chunk_min=chunk_min, chunk_avg=chunk_avg, chunk_max=chunk_max, fastcdc_stride=fastcdc_stride, codec=codec, force=True)

    primary_root = out / "primary_root"
    mirror_root = out / "mirror_root"
    primary_root.mkdir()
    mirror_root.mkdir()
    shutil.copytree(repo_v1_src, primary_root / "release_v1.cldrepo")
    shutil.copytree(repo_v2_src, primary_root / "release_v2.cldrepo")
    shutil.copytree(repo_v1_src, mirror_root / "release_v1.cldrepo")
    shutil.copytree(repo_v2_src, mirror_root / "release_v2.cldrepo")

    cache = out / "cache"
    install_v1 = out / "install_v1"
    install_v2 = out / "install_v2"

    with _Alpha34HTTPServer(primary_root, bind=bind, port=primary_port, name="primary", truncate_every=primary_truncate_every, delay_ms=primary_delay_ms) as primary, \
         _Alpha34HTTPServer(mirror_root, bind=bind, port=mirror_port, name="mirror", truncate_every=0, delay_ms=mirror_delay_ms) as mirror:
        primary_v1 = primary.base_url + "release_v1.cldrepo/"
        primary_v2 = primary.base_url + "release_v2.cldrepo/"
        mirror_v1 = mirror.base_url + "release_v1.cldrepo/"
        mirror_v2 = mirror.base_url + "release_v2.cldrepo/"

        fetch_v1 = fetch_install(primary_v1, install_v1, cache_dir=cache, verify=True, mirrors=[mirror_v1], mirror_policy=mirror_policy, hedge_delay=hedge_delay, http_retries=http_retries, http_backoff=http_backoff, mirror_blacklist_threshold=1, mirror_blacklist_seconds=60.0, parallel=parallel)
        fetch_v2 = fetch_install(primary_v2, install_v2, cache_dir=cache, from_installed=install_v1, verify=True, mirrors=[mirror_v2], mirror_policy=mirror_policy, hedge_delay=hedge_delay, http_retries=http_retries, http_backoff=http_backoff, mirror_blacklist_threshold=1, mirror_blacklist_seconds=60.0, parallel=parallel)
        audit_v2 = audit_install(primary_v2, install_v2)
        result = {
            "schema": "CoreLangDistribution/MirrorRobustnessPilot",
            "version": "2.0-alpha34",
            "ok": bool(fetch_v1.get("ok") and fetch_v2.get("ok") and audit_v2.get("ok")),
            "out_dir": str(out),
            "file_size": file_size,
            "codec": codec,
            "chunker": chunker,
            "mirror_policy": mirror_policy,
            "primary_truncate_every": int(primary_truncate_every or 0),
            "primary_url": primary.base_url,
            "mirror_url": mirror.base_url,
            "repo_v1_url": primary_v1,
            "repo_v2_url": primary_v2,
            "mirror_v1_url": mirror_v1,
            "mirror_v2_url": mirror_v2,
            "primary_server": primary.config,
            "mirror_server": mirror.config,
            "fetch_v1": fetch_v1,
            "fetch_v2": fetch_v2,
            "audit_v2": audit_v2,
        }

    (out / "mirror_pilot_result.json").write_text(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    (out / "mirror_pilot_report.md").write_text(_alpha34_mirror_report_markdown(result), encoding="utf-8")
    (out / "index.html").write_text(_alpha34_mirror_report_html(result), encoding="utf-8")
    with (out / "mirror_pilot_summary.csv").open("w", newline="", encoding="utf-8") as f:
        fieldnames = ["step", "ok", "downloaded_chunks", "cache_hit_chunks", "downloaded_pack_bytes", "reused_cache_raw_bytes", "range_requests", "range_retries", "range_failures", "if_range_used", "etag_observed", "mirror_blacklist_events"]
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for step in ["fetch_v1", "fetch_v2"]:
            fr = result.get(step, {})
            net = fr.get("network") or {}
            w.writerow({
                "step": step,
                "ok": fr.get("ok"),
                "downloaded_chunks": fr.get("downloaded_chunks"),
                "cache_hit_chunks": fr.get("cache_hit_chunks"),
                "downloaded_pack_bytes": fr.get("downloaded_pack_bytes"),
                "reused_cache_raw_bytes": fr.get("reused_cache_raw_bytes"),
                "range_requests": net.get("range_requests"),
                "range_retries": net.get("range_retries"),
                "range_failures": net.get("range_failures"),
                "if_range_used": net.get("if_range_used"),
                "etag_observed": net.get("etag_observed"),
                "mirror_blacklist_events": net.get("mirror_blacklist_events", 0),
            })
    if not keep_heavy:
        for heavy in [pair_root, repos_src, primary_root, mirror_root, cache, install_v1, install_v2]:
            shutil.rmtree(heavy, ignore_errors=True)
    if make_review:
        result["review_zip"] = make_review_zip(out, str(out) + "_REVIEW.zip", max_mb=max_mb)
        # alpha34.1: write the final result and ensure machine-readable mirror reports are included.
        (out / "mirror_pilot_result.json").write_text(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
        try:
            _zip_out = result["review_zip"].get("zip_out") if isinstance(result.get("review_zip"), dict) else result.get("review_zip")
            if _zip_out:
                with zipfile.ZipFile(_zip_out, "a", compression=zipfile.ZIP_DEFLATED) as _zf:
                    for _name in ["mirror_pilot_result.json", "mirror_pilot_summary.csv", "mirror_pilot_report.md"]:
                        _p = out / _name
                        if _p.exists() and _name not in _zf.namelist():
                            _zf.write(_p, arcname=_name)
        except Exception:
            pass
    return result



def _alpha35_object_store_report_markdown(result: dict) -> str:
    f1 = result.get("fetch_v1", {})
    f2 = result.get("fetch_v2", {})
    n1 = f1.get("network") or {}
    n2 = f2.get("network") or {}
    audit = result.get("audit_v2") or {}
    return f"""# CLD2 alpha35 object-store layout pilot report

## Summary

- ok: `{result.get('ok')}`
- mode: `{result.get('mode')}`
- server_url: `{result.get('server_url')}`
- bucket: `{result.get('bucket')}`
- prefix: `{result.get('prefix')}`
- repo_v1_url: `{result.get('repo_v1_url')}`
- repo_v2_url: `{result.get('repo_v2_url')}`
- file_size: `{result.get('file_size')}`
- codec: `{result.get('codec')}`
- chunker: `{result.get('chunker')}`

## Fetch/install results

| Step | OK | Downloaded chunks | Cache-hit chunks | Downloaded pack bytes | Reused cache raw bytes | Range requests | HEAD requests | If-Range used | ETag observed |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| v1 initial object-store fetch | {f1.get('ok')} | {f1.get('downloaded_chunks')} | {f1.get('cache_hit_chunks')} | {f1.get('downloaded_pack_bytes')} | {f1.get('reused_cache_raw_bytes')} | {n1.get('range_requests')} | {n1.get('head_requests')} | {n1.get('if_range_used')} | {n1.get('etag_observed')} |
| v2 object-store update from installed v1 | {f2.get('ok')} | {f2.get('downloaded_chunks')} | {f2.get('cache_hit_chunks')} | {f2.get('downloaded_pack_bytes')} | {f2.get('reused_cache_raw_bytes')} | {n2.get('range_requests')} | {n2.get('head_requests')} | {n2.get('if_range_used')} | {n2.get('etag_observed')} |

## Object layout

- bucket_root: `{result.get('bucket_root')}`
- object_manifest: `{result.get('object_manifest_path')}`
- release_v1_prefix: `{result.get('release_v1_prefix')}`
- release_v2_prefix: `{result.get('release_v2_prefix')}`
- object_count: `{result.get('object_count')}`

## Verify/audit

- verify_v1_remote_ok: `{(result.get('verify_v1_remote') or {}).get('ok')}`
- verify_v2_remote_ok: `{(result.get('verify_v2_remote') or {}).get('ok')}`
- audit_v2_ok: `{audit.get('ok')}`
- files_expected: `{audit.get('files_expected')}`
- files_ok: `{audit.get('files_ok')}`
- missing_count: `{audit.get('missing_count')}`
- corrupt_count: `{audit.get('corrupt_count')}`

## Interpretation

This is a local object-store layout pilot, not a real AWS S3/MinIO test. It maps CLD2 repos into a bucket/prefix/object-key layout and serves that layout over local HTTP. The success signal is correct URL addressing, Range/ETag-aware fetch, install verification, update from v1 to v2, and a complete review package.
"""


def _alpha35_object_store_report_html(result: dict) -> str:
    import html
    report_md = _alpha35_object_store_report_markdown(result)
    rows = []
    for name in ["fetch_v1", "fetch_v2"]:
        f = result.get(name, {})
        net = f.get("network") or {}
        rows.append(
            "<tr>"
            f"<td><code>{html.escape(name)}</code></td>"
            f"<td>{html.escape(str(f.get('ok')))}</td>"
            f"<td>{html.escape(str(f.get('downloaded_chunks')))}</td>"
            f"<td>{html.escape(str(f.get('cache_hit_chunks')))}</td>"
            f"<td>{html.escape(str(f.get('downloaded_pack_bytes')))}</td>"
            f"<td>{html.escape(str(f.get('reused_cache_raw_bytes')))}</td>"
            f"<td>{html.escape(str(net.get('range_requests')))}</td>"
            f"<td>{html.escape(str(net.get('head_requests')))}</td>"
            f"<td>{html.escape(str(net.get('if_range_used')))}</td>"
            f"<td>{html.escape(str(net.get('etag_observed')))}</td>"
            "</tr>"
        )
    ok_class = "ok" if result.get("ok") else "bad"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>CLD2 alpha35 object-store pilot</title>
<style>body{{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:2rem;background:#fafafa;color:#111}}code{{background:#f3f3f3;padding:.12rem .25rem;border-radius:4px}}table{{border-collapse:collapse;width:100%;background:white}}th,td{{border:1px solid #ddd;padding:.55rem;text-align:left}}th{{background:#eee}}.badge{{display:inline-block;padding:.25rem .7rem;border-radius:999px;font-weight:700}}.ok{{background:#e7f7ed;color:#126b35}}.bad{{background:#fdeaea;color:#8b1a1a}}.card{{background:white;border:1px solid #ddd;border-radius:12px;padding:1rem;margin:1rem 0}}</style></head>
<body><h1>CLD2 alpha35 object-store layout pilot</h1><p>Status: <span class="badge {ok_class}">{html.escape(str(result.get('ok')))}</span></p>
<div class="card"><p>Mode: <code>{html.escape(str(result.get('mode')))}</code></p><p>Server: <code>{html.escape(str(result.get('server_url')))}</code></p><p>Bucket: <code>{html.escape(str(result.get('bucket')))}</code></p><p>Prefix: <code>{html.escape(str(result.get('prefix')))}</code></p><p>Repo v1: <code>{html.escape(str(result.get('repo_v1_url')))}</code></p><p>Repo v2: <code>{html.escape(str(result.get('repo_v2_url')))}</code></p></div>
<table><thead><tr><th>Step</th><th>OK</th><th>Downloaded chunks</th><th>Cache-hit chunks</th><th>Downloaded pack bytes</th><th>Reused cache raw bytes</th><th>Range requests</th><th>HEAD requests</th><th>If-Range used</th><th>ETag observed</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
<h2>Interpretation</h2><p>This is a local object-store layout pilot, not a real cloud benchmark. It checks bucket/prefix URL addressing, Range/ETag-aware fetch, install verification and cache reuse.</p>
<h2>Markdown source</h2><pre>{html.escape(report_md)}</pre>
</body></html>"""


def _alpha35_count_objects(root: Path) -> int:
    return sum(1 for p in Path(root).rglob("*") if p.is_file())


def run_object_store_pilot(
    out_dir: str | Path,
    *,
    file_size: str = "8MiB",
    bind: str = "127.0.0.1",
    port: int = 0,
    bucket: str = "cld2-alpha35-bucket",
    prefix: str = "releases",
    codec: str = "zstd",
    chunker: str = "fastcdc",
    fixed_size: str = "1MiB",
    chunk_min: str = "64KiB",
    chunk_avg: str = "256KiB",
    chunk_max: str = "1MiB",
    fastcdc_stride: int = 4,
    http_retries: int = 3,
    http_backoff: float = 0.05,
    parallel: int = 1,
    cld2_attach_retries: int = 4,
    cld2_attach_backoff: float = 2.0,
    make_review: bool = False,
    keep_heavy: bool = True,
    max_mb: float = 10.0,
) -> dict:
    """Run alpha35 local object-store/S3-style layout pilot.

    This is not a real S3 client. It validates that CLD2 repos can be addressed as
    object-store keys under bucket/prefix paths and fetched through HTTP Range/ETag.
    """
    out = Path(out_dir)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    pair_root = out / "pairs"
    v1, v2 = _alpha33_write_network_pair(pair_root, file_size=file_size)

    repos_src = out / "repos_src"
    repos_src.mkdir(parents=True)
    repo_v1_src = repos_src / "release_v1.cldrepo"
    repo_v2_src = repos_src / "release_v2.cldrepo"
    make_repo(v1, repo_v1_src, release_id="alpha35-object-v1", release_seq=1, chunker=chunker, fixed_size=fixed_size, chunk_min=chunk_min, chunk_avg=chunk_avg, chunk_max=chunk_max, fastcdc_stride=fastcdc_stride, codec=codec, force=True)
    make_repo(v2, repo_v2_src, release_id="alpha35-object-v2", release_seq=2, chunker=chunker, fixed_size=fixed_size, chunk_min=chunk_min, chunk_avg=chunk_avg, chunk_max=chunk_max, fastcdc_stride=fastcdc_stride, codec=codec, force=True)
    local_diff = diff_repos(repo_v1_src, repo_v2_src)

    bucket_root = out / "object_store_root"
    release_v1_prefix = f"{bucket.strip('/')}/{prefix.strip('/')}/release_v1.cldrepo"
    release_v2_prefix = f"{bucket.strip('/')}/{prefix.strip('/')}/release_v2.cldrepo"
    release_v1_path = bucket_root / release_v1_prefix
    release_v2_path = bucket_root / release_v2_prefix
    release_v1_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(repo_v1_src, release_v1_path)
    shutil.copytree(repo_v2_src, release_v2_path)

    object_manifest = {
        "schema": "CoreLangDistribution/ObjectStoreLayout",
        "version": "2.0-alpha35",
        "mode": "local-http-object-layout",
        "bucket": bucket,
        "prefix": prefix,
        "release_v1_prefix": release_v1_prefix,
        "release_v2_prefix": release_v2_prefix,
        "note": "Local filesystem bucket/prefix layout served via HTTP; not real AWS S3/MinIO API.",
        "objects": [],
    }
    for p in sorted(bucket_root.rglob("*")):
        if p.is_file():
            rel = p.relative_to(bucket_root).as_posix()
            object_manifest["objects"].append({"key": rel, "bytes": p.stat().st_size, "sha256": sha256_file(p)})
    object_manifest_path = out / "object_store_manifest.json"
    object_manifest_path.write_text(json.dumps(object_manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")

    cache = out / "cache"
    install_v1 = out / "install_v1"
    install_v2 = out / "install_v2"

    with _Alpha33HTTPServer(bucket_root, bind=bind, port=port) as srv:
        base_url = srv.base_url
        repo_v1_url = base_url + release_v1_prefix + "/"
        repo_v2_url = base_url + release_v2_prefix + "/"
        verify_v1 = verify_repo(repo_v1_url, deep=False)
        verify_v2 = verify_repo(repo_v2_url, deep=False)
        fetch_v1 = fetch_install(repo_v1_url, install_v1, cache_dir=cache, verify=True, http_retries=http_retries, http_backoff=http_backoff, parallel=parallel)
        fetch_v2 = fetch_install(repo_v2_url, install_v2, cache_dir=cache, from_installed=install_v1, verify=True, http_retries=http_retries, http_backoff=http_backoff, parallel=parallel)
        audit_v2 = audit_install(repo_v2_url, install_v2)
        result = {
            "schema": "CoreLangDistribution/ObjectStorePilot",
            "version": "2.0-alpha35",
            "ok": bool(fetch_v1.get("ok") and fetch_v2.get("ok") and audit_v2.get("ok") and verify_v1.get("ok") and verify_v2.get("ok")),
            "mode": "local-http-object-layout",
            "out_dir": str(out),
            "file_size": file_size,
            "codec": codec,
            "chunker": chunker,
            "server_url": base_url,
            "bucket": bucket,
            "prefix": prefix,
            "bucket_root": str(bucket_root),
            "repo_v1_url": repo_v1_url,
            "repo_v2_url": repo_v2_url,
            "release_v1_prefix": release_v1_prefix,
            "release_v2_prefix": release_v2_prefix,
            "object_manifest_path": str(object_manifest_path),
            "object_count": _alpha35_count_objects(bucket_root),
            "local_diff": local_diff,
            "verify_v1_remote": verify_v1,
            "verify_v2_remote": verify_v2,
            "fetch_v1": fetch_v1,
            "fetch_v2": fetch_v2,
            "audit_v2": audit_v2,
        }

    (out / "object_store_pilot_result.json").write_text(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    (out / "object_store_pilot_report.md").write_text(_alpha35_object_store_report_markdown(result), encoding="utf-8")
    (out / "index.html").write_text(_alpha35_object_store_report_html(result), encoding="utf-8")
    with (out / "object_store_pilot_summary.csv").open("w", newline="", encoding="utf-8") as f:
        fieldnames = ["step", "ok", "downloaded_chunks", "cache_hit_chunks", "downloaded_pack_bytes", "reused_cache_raw_bytes", "range_requests", "head_requests", "if_range_used", "etag_observed"]
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for step in ["fetch_v1", "fetch_v2"]:
            fr = result.get(step, {})
            net = fr.get("network") or {}
            w.writerow({
                "step": step,
                "ok": fr.get("ok"),
                "downloaded_chunks": fr.get("downloaded_chunks"),
                "cache_hit_chunks": fr.get("cache_hit_chunks"),
                "downloaded_pack_bytes": fr.get("downloaded_pack_bytes"),
                "reused_cache_raw_bytes": fr.get("reused_cache_raw_bytes"),
                "range_requests": net.get("range_requests"),
                "head_requests": net.get("head_requests"),
                "if_range_used": net.get("if_range_used"),
                "etag_observed": net.get("etag_observed"),
            })
    if not keep_heavy:
        for heavy in [pair_root, repos_src, bucket_root, cache, install_v1, install_v2]:
            shutil.rmtree(heavy, ignore_errors=True)
    if make_review:
        result["review_zip"] = make_review_zip(out, str(out) + "_REVIEW.zip", max_mb=max_mb)
        (out / "object_store_pilot_result.json").write_text(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
        try:
            _zip_out = result["review_zip"].get("zip_out") if isinstance(result.get("review_zip"), dict) else result.get("review_zip")
            if _zip_out:
                with zipfile.ZipFile(_zip_out, "a", compression=zipfile.ZIP_DEFLATED) as _zf:
                    for _name in ["object_store_pilot_result.json", "object_store_pilot_summary.csv", "object_store_pilot_report.md", "object_store_manifest.json"]:
                        _p = out / _name
                        if _p.exists() and _name not in _zf.namelist():
                            _zf.write(_p, arcname=_name)
        except Exception:
            pass
    return result



def _alpha36_hmac_token(secret: bytes, prefix: str, expires_unix: int) -> str:
    import hmac
    import hashlib
    payload = f"{prefix.rstrip('/')}\n{int(expires_unix)}".encode("utf-8", "surrogatepass")
    sig = hmac.new(secret, payload, hashlib.sha256).hexdigest()
    return f"{int(expires_unix)}.{sig}"


class _Alpha36SignedURLServer:
    """Local signed-prefix HTTP server.

    This is not a cloud/S3 presigned URL implementation. It is a local gateway
    that only serves paths under /signed/<token>/<authorized-prefix>/... when
    token = HMAC(secret, authorized_prefix + expiry).
    """
    def __init__(
        self,
        directory: Path,
        *,
        bind: str = "127.0.0.1",
        port: int = 0,
        authorized_prefix: str,
        token: str,
        secret: bytes,
    ):
        self.directory = Path(directory)
        self.bind = bind
        self.authorized_prefix = authorized_prefix.strip("/")
        self.token = token
        self.secret = secret
        import functools
        import posixpath
        import urllib.parse

        server_self = self

        class SignedRangeHandler(RangeRequestHandler):
            def _auth_parts(self):
                parsed = urllib.parse.urlsplit(self.path)
                parts = [p for p in parsed.path.split("/") if p]
                if len(parts) < 3 or parts[0] != "signed":
                    return False, "", ""
                token = parts[1]
                rel = "/".join(parts[2:])
                return token == server_self.token and rel.startswith(server_self.authorized_prefix.rstrip("/") + "/"), token, rel

            def _token_is_valid(self) -> bool:
                try:
                    exp_s, _sig = server_self.token.split(".", 1)
                    expires = int(exp_s)
                except Exception:
                    return False
                if time.time() > expires:
                    return False
                expected = _alpha36_hmac_token(server_self.secret, server_self.authorized_prefix, expires)
                return expected == server_self.token

            def send_head(self):  # noqa: N802
                ok, _token, _rel = self._auth_parts()
                if not ok or not self._token_is_valid():
                    self.send_error(403, "Invalid or expired alpha36 signed URL token")
                    return None
                return super().send_head()

            def translate_path(self, path):  # noqa: N802
                parsed = urllib.parse.urlsplit(path)
                parts = [p for p in parsed.path.split("/") if p]
                if len(parts) >= 3 and parts[0] == "signed":
                    rel = "/".join(parts[2:])
                else:
                    rel = ""
                # Keep it simple and safe: normalize and drop dangerous path parts.
                rel = posixpath.normpath(urllib.parse.unquote(rel))
                rel_parts = [p for p in rel.split("/") if p and p not in (".", "..")]
                local = server_self.directory
                for part in rel_parts:
                    local = local / part
                return str(local)

        handler = functools.partial(SignedRangeHandler, directory=str(self.directory))
        self.httpd = ThreadingHTTPServer((bind, int(port)), handler)
        self.port = int(self.httpd.server_address[1])
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="cld2-alpha36-signed-url", daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://{self.bind}:{self.port}/signed/{self.token}/"

    def __enter__(self):
        self.thread.start()
        time.sleep(0.05)
        return self

    def __exit__(self, exc_type, exc, tb):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2.0)


def _alpha36_signed_url_report_markdown(result: dict) -> str:
    f1 = result.get("fetch_v1", {})
    f2 = result.get("fetch_v2", {})
    n1 = f1.get("network") or {}
    n2 = f2.get("network") or {}
    audit = result.get("audit_v2") or {}
    pol = result.get("signed_url_policy") or {}
    return f"""# CLD2 alpha36 signed URL policy pilot report

## Summary

- ok: `{result.get('ok')}`
- mode: `{result.get('mode')}`
- server_url: `{result.get('server_url')}`
- signed_base_url: `{result.get('signed_base_url')}`
- bucket: `{result.get('bucket')}`
- prefix: `{result.get('prefix')}`
- authorized_prefix: `{pol.get('authorized_prefix')}`
- expires_unix: `{pol.get('expires_unix')}`
- token_sha256: `{pol.get('token_sha256')}`
- file_size: `{result.get('file_size')}`
- codec: `{result.get('codec')}`
- chunker: `{result.get('chunker')}`

## Fetch/install results

| Step | OK | Downloaded chunks | Cache-hit chunks | Downloaded pack bytes | Reused cache raw bytes | Range requests | HEAD requests | If-Range used | ETag observed |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| v1 initial signed URL fetch | {f1.get('ok')} | {f1.get('downloaded_chunks')} | {f1.get('cache_hit_chunks')} | {f1.get('downloaded_pack_bytes')} | {f1.get('reused_cache_raw_bytes')} | {n1.get('range_requests')} | {n1.get('head_requests')} | {n1.get('if_range_used')} | {n1.get('etag_observed')} |
| v2 signed URL update from installed v1 | {f2.get('ok')} | {f2.get('downloaded_chunks')} | {f2.get('cache_hit_chunks')} | {f2.get('downloaded_pack_bytes')} | {f2.get('reused_cache_raw_bytes')} | {n2.get('range_requests')} | {n2.get('head_requests')} | {n2.get('if_range_used')} | {n2.get('etag_observed')} |

## Verify/audit

- verify_v1_remote_ok: `{(result.get('verify_v1_remote') or {}).get('ok')}`
- verify_v2_remote_ok: `{(result.get('verify_v2_remote') or {}).get('ok')}`
- audit_v2_ok: `{audit.get('ok')}`
- files_expected: `{audit.get('files_expected')}`
- files_ok: `{audit.get('files_ok')}`
- missing_count: `{audit.get('missing_count')}`
- corrupt_count: `{audit.get('corrupt_count')}`

## Interpretation

This is a local signed-prefix URL pilot. It is not an AWS S3 presigned URL test. It checks that CLD2 can fetch and update through a URL prefix authorized by an HMAC token, while preserving Range/ETag-aware reads, verification and cache reuse.
"""


def _alpha36_signed_url_report_html(result: dict) -> str:
    import html
    md = _alpha36_signed_url_report_markdown(result)
    pol = result.get("signed_url_policy") or {}
    rows = []
    for name in ["fetch_v1", "fetch_v2"]:
        f = result.get(name, {})
        net = f.get("network") or {}
        rows.append(
            "<tr>"
            f"<td><code>{html.escape(name)}</code></td>"
            f"<td>{html.escape(str(f.get('ok')))}</td>"
            f"<td>{html.escape(str(f.get('downloaded_chunks')))}</td>"
            f"<td>{html.escape(str(f.get('cache_hit_chunks')))}</td>"
            f"<td>{html.escape(str(f.get('downloaded_pack_bytes')))}</td>"
            f"<td>{html.escape(str(f.get('reused_cache_raw_bytes')))}</td>"
            f"<td>{html.escape(str(net.get('range_requests')))}</td>"
            f"<td>{html.escape(str(net.get('head_requests')))}</td>"
            f"<td>{html.escape(str(net.get('if_range_used')))}</td>"
            f"<td>{html.escape(str(net.get('etag_observed')))}</td>"
            "</tr>"
        )
    ok_class = "ok" if result.get("ok") else "bad"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>CLD2 alpha36 signed URL policy pilot</title>
<style>body{{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:2rem;background:#fafafa;color:#111}}code{{background:#f3f3f3;padding:.12rem .25rem;border-radius:4px}}table{{border-collapse:collapse;width:100%;background:white}}th,td{{border:1px solid #ddd;padding:.55rem;text-align:left}}th{{background:#eee}}.badge{{display:inline-block;padding:.25rem .7rem;border-radius:999px;font-weight:700}}.ok{{background:#e7f7ed;color:#126b35}}.bad{{background:#fdeaea;color:#8b1a1a}}.card{{background:white;border:1px solid #ddd;border-radius:12px;padding:1rem;margin:1rem 0}}</style></head>
<body><h1>CLD2 alpha36 signed URL policy pilot</h1><p>Status: <span class="badge {ok_class}">{html.escape(str(result.get('ok')))}</span></p>
<div class="card"><p>Mode: <code>{html.escape(str(result.get('mode')))}</code></p><p>Server: <code>{html.escape(str(result.get('server_url')))}</code></p><p>Signed base: <code>{html.escape(str(result.get('signed_base_url')))}</code></p><p>Authorized prefix: <code>{html.escape(str(pol.get('authorized_prefix')))}</code></p><p>Token SHA256: <code>{html.escape(str(pol.get('token_sha256')))}</code></p></div>
<table><thead><tr><th>Step</th><th>OK</th><th>Downloaded chunks</th><th>Cache-hit chunks</th><th>Downloaded pack bytes</th><th>Reused cache raw bytes</th><th>Range requests</th><th>HEAD requests</th><th>If-Range used</th><th>ETag observed</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
<h2>Interpretation</h2><p>This is a local signed-prefix URL pilot, not a cloud presigned URL test. It checks HMAC-authorized prefix fetch/update with Range/ETag and verification.</p>
<h2>Markdown source</h2><pre>{html.escape(md)}</pre>
</body></html>"""


def run_signed_url_pilot(
    out_dir: str | Path,
    *,
    file_size: str = "8MiB",
    bind: str = "127.0.0.1",
    port: int = 0,
    bucket: str = "cld2-alpha36-bucket",
    prefix: str = "releases",
    ttl_seconds: int = 3600,
    codec: str = "zstd",
    chunker: str = "fastcdc",
    fixed_size: str = "1MiB",
    chunk_min: str = "64KiB",
    chunk_avg: str = "256KiB",
    chunk_max: str = "1MiB",
    fastcdc_stride: int = 4,
    http_retries: int = 3,
    http_backoff: float = 0.05,
    parallel: int = 1,
    cld2_attach_retries: int = 4,
    cld2_attach_backoff: float = 2.0,
    make_review: bool = False,
    keep_heavy: bool = True,
    max_mb: float = 10.0,
) -> dict:
    """Run alpha36 local signed-prefix URL policy pilot."""
    import secrets
    import hashlib

    out = Path(out_dir)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    pair_root = out / "pairs"
    v1, v2 = _alpha33_write_network_pair(pair_root, file_size=file_size)

    repos_src = out / "repos_src"
    repos_src.mkdir(parents=True)
    repo_v1_src = repos_src / "release_v1.cldrepo"
    repo_v2_src = repos_src / "release_v2.cldrepo"
    make_repo(v1, repo_v1_src, release_id="alpha36-signed-v1", release_seq=1, chunker=chunker, fixed_size=fixed_size, chunk_min=chunk_min, chunk_avg=chunk_avg, chunk_max=chunk_max, fastcdc_stride=fastcdc_stride, codec=codec, force=True)
    make_repo(v2, repo_v2_src, release_id="alpha36-signed-v2", release_seq=2, chunker=chunker, fixed_size=fixed_size, chunk_min=chunk_min, chunk_avg=chunk_avg, chunk_max=chunk_max, fastcdc_stride=fastcdc_stride, codec=codec, force=True)

    bucket_root = out / "signed_object_root"
    release_v1_prefix = f"{bucket.strip('/')}/{prefix.strip('/')}/release_v1.cldrepo"
    release_v2_prefix = f"{bucket.strip('/')}/{prefix.strip('/')}/release_v2.cldrepo"
    authorized_prefix = f"{bucket.strip('/')}/{prefix.strip('/')}"
    shutil.copytree(repo_v1_src, bucket_root / release_v1_prefix)
    shutil.copytree(repo_v2_src, bucket_root / release_v2_prefix)

    secret = secrets.token_bytes(32)
    expires_unix = int(time.time()) + int(ttl_seconds)
    token = _alpha36_hmac_token(secret, authorized_prefix, expires_unix)
    token_sha256 = hashlib.sha256(token.encode("utf-8")).hexdigest()
    issued_unix = int(time.time())
    issued_at = datetime.fromtimestamp(issued_unix, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    expires_at = datetime.fromtimestamp(expires_unix, tz=timezone.utc).isoformat().replace("+00:00", "Z")

    signed_url_policy = {
        "schema": "CoreLangDistribution/SignedURLPolicy",
        "version": "2.0-alpha36.1",
        "mode": "local-signed-prefix-url",
        "algorithm": "HMAC-SHA256",
        "secret_stored": False,
        "bucket": bucket,
        "prefix": prefix,
        "authorized_prefix": authorized_prefix,
        "release_v1_prefix": release_v1_prefix,
        "release_v2_prefix": release_v2_prefix,
        "issued_unix": issued_unix,
        "issued_at": issued_at,
        "expires_unix": expires_unix,
        "expires_at": expires_at,
        "ttl_seconds": int(ttl_seconds),
        "token_present": True,
        "token_sha256": token_sha256,
        "scope": {
            "type": "prefix",
            "bucket": bucket,
            "prefix": prefix,
            "authorized_prefix": authorized_prefix,
        },
        "security_note": "The raw token and secret are not stored in this policy file; only token_sha256 is recorded for audit.",
        "note": "Local signed-prefix URL pilot. Not AWS S3/MinIO presigned URL.",
        "objects": [],
    }
    for p in sorted(bucket_root.rglob("*")):
        if p.is_file():
            rel = p.relative_to(bucket_root).as_posix()
            signed_url_policy["objects"].append({"key": rel, "bytes": p.stat().st_size, "sha256": sha256_file(p)})
    policy_path = out / "signed_url_policy.json"
    policy_path.write_text(json.dumps(signed_url_policy, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")

    cache = out / "cache"
    install_v1 = out / "install_v1"
    install_v2 = out / "install_v2"

    with _Alpha36SignedURLServer(bucket_root, bind=bind, port=port, authorized_prefix=authorized_prefix, token=token, secret=secret) as srv:
        server_url = f"http://{bind}:{srv.port}/"
        signed_base_url = srv.base_url
        repo_v1_url = signed_base_url + release_v1_prefix + "/"
        repo_v2_url = signed_base_url + release_v2_prefix + "/"
        verify_v1 = verify_repo(repo_v1_url, deep=False)
        verify_v2 = verify_repo(repo_v2_url, deep=False)
        fetch_v1 = fetch_install(repo_v1_url, install_v1, cache_dir=cache, verify=True, http_retries=http_retries, http_backoff=http_backoff, parallel=parallel)
        fetch_v2 = fetch_install(repo_v2_url, install_v2, cache_dir=cache, from_installed=install_v1, verify=True, http_retries=http_retries, http_backoff=http_backoff, parallel=parallel)
        audit_v2 = audit_install(repo_v2_url, install_v2)
        result = {
            "schema": "CoreLangDistribution/SignedURLPilot",
            "version": "2.0-alpha36.1",
            "ok": bool(fetch_v1.get("ok") and fetch_v2.get("ok") and audit_v2.get("ok") and verify_v1.get("ok") and verify_v2.get("ok")),
            "mode": "local-signed-prefix-url",
            "out_dir": str(out),
            "file_size": file_size,
            "codec": codec,
            "chunker": chunker,
            "server_url": server_url,
            "signed_base_url": signed_base_url,
            "bucket": bucket,
            "prefix": prefix,
            "authorized_prefix": authorized_prefix,
            "repo_v1_url": repo_v1_url,
            "repo_v2_url": repo_v2_url,
            "signed_url_policy": signed_url_policy,
            "signed_url_policy_path": str(policy_path),
            "verify_v1_remote": verify_v1,
            "verify_v2_remote": verify_v2,
            "fetch_v1": fetch_v1,
            "fetch_v2": fetch_v2,
            "audit_v2": audit_v2,
        }

    (out / "signed_url_pilot_result.json").write_text(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    (out / "signed_url_pilot_report.md").write_text(_alpha36_signed_url_report_markdown(result), encoding="utf-8")
    (out / "index.html").write_text(_alpha36_signed_url_report_html(result), encoding="utf-8")
    with (out / "signed_url_pilot_summary.csv").open("w", newline="", encoding="utf-8") as f:
        fieldnames = ["step", "ok", "downloaded_chunks", "cache_hit_chunks", "downloaded_pack_bytes", "reused_cache_raw_bytes", "range_requests", "head_requests", "if_range_used", "etag_observed"]
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for step in ["fetch_v1", "fetch_v2"]:
            fr = result.get(step, {})
            net = fr.get("network") or {}
            w.writerow({
                "step": step,
                "ok": fr.get("ok"),
                "downloaded_chunks": fr.get("downloaded_chunks"),
                "cache_hit_chunks": fr.get("cache_hit_chunks"),
                "downloaded_pack_bytes": fr.get("downloaded_pack_bytes"),
                "reused_cache_raw_bytes": fr.get("reused_cache_raw_bytes"),
                "range_requests": net.get("range_requests"),
                "head_requests": net.get("head_requests"),
                "if_range_used": net.get("if_range_used"),
                "etag_observed": net.get("etag_observed"),
            })
    if not keep_heavy:
        for heavy in [pair_root, repos_src, bucket_root, cache, install_v1, install_v2]:
            shutil.rmtree(heavy, ignore_errors=True)
    if make_review:
        result["review_zip"] = make_review_zip(out, str(out) + "_REVIEW.zip", max_mb=max_mb)
        (out / "signed_url_pilot_result.json").write_text(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
        try:
            _zip_out = result["review_zip"].get("zip_out") if isinstance(result.get("review_zip"), dict) else result.get("review_zip")
            if _zip_out:
                with zipfile.ZipFile(_zip_out, "a", compression=zipfile.ZIP_DEFLATED) as _zf:
                    for _name in ["signed_url_pilot_result.json", "signed_url_pilot_summary.csv", "signed_url_pilot_report.md", "signed_url_policy.json"]:
                        _p = out / _name
                        if _p.exists() and _name not in _zf.namelist():
                            _zf.write(_p, arcname=_name)
        except Exception:
            pass
    return result



def _alpha37_run_process(cmd: list[str], *, timeout: int = 120, env: dict | None = None) -> dict:
    try:
        p = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, env=env)
        return {"cmd": cmd, "returncode": p.returncode, "stdout": p.stdout[-4000:], "stderr": p.stderr[-4000:], "ok": p.returncode == 0}
    except FileNotFoundError as e:
        return {"cmd": cmd, "returncode": 127, "stdout": "", "stderr": str(e), "ok": False}
    except subprocess.TimeoutExpired as e:
        return {"cmd": cmd, "returncode": 124, "stdout": (e.stdout or "")[-4000:] if isinstance(e.stdout, str) else "", "stderr": (e.stderr or "")[-4000:] if isinstance(e.stderr, str) else "timeout", "ok": False}


def _alpha37_minio_report_markdown(result: dict) -> str:
    f1 = result.get("fetch_v1") or {}
    f2 = result.get("fetch_v2") or {}
    n1 = f1.get("network") or {}
    n2 = f2.get("network") or {}
    audit = result.get("audit_v2") or {}
    pre = result.get("preflight") or {}
    return f"""# CLD2 alpha37 MinIO/S3-compatible pilot report

## Summary

- ok: `{result.get('ok')}`
- mode: `{result.get('mode')}`
- preflight_only: `{result.get('preflight_only')}`
- endpoint: `{result.get('endpoint')}`
- bucket: `{result.get('bucket')}`
- prefix: `{result.get('prefix')}`
- public_base_url: `{result.get('public_base_url')}`
- repo_v1_url: `{result.get('repo_v1_url')}`
- repo_v2_url: `{result.get('repo_v2_url')}`

## Preflight

- mc_found: `{pre.get('mc_found')}`
- endpoint_configured: `{pre.get('endpoint_configured')}`
- access_key_configured: `{pre.get('access_key_configured')}`
- secret_key_configured: `{pre.get('secret_key_configured')}`
- can_run_real_pilot: `{pre.get('can_run_real_pilot')}`
- note: `{pre.get('note')}`

## Upload/config commands

| Command | OK | Return code |
|---|---:|---:|
""" + "\n".join([f"| `{(' '.join(c.get('cmd', [])))[:120]}` | {c.get('ok')} | {c.get('returncode')} |" for c in result.get("mc_commands", [])]) + f"""

## Fetch/install results

| Step | OK | Downloaded chunks | Cache-hit chunks | Downloaded pack bytes | Reused cache raw bytes | Range requests | HEAD requests | If-Range used | ETag observed |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| v1 MinIO/S3-compatible fetch | {f1.get('ok')} | {f1.get('downloaded_chunks')} | {f1.get('cache_hit_chunks')} | {f1.get('downloaded_pack_bytes')} | {f1.get('reused_cache_raw_bytes')} | {n1.get('range_requests')} | {n1.get('head_requests')} | {n1.get('if_range_used')} | {n1.get('etag_observed')} |
| v2 MinIO/S3-compatible update | {f2.get('ok')} | {f2.get('downloaded_chunks')} | {f2.get('cache_hit_chunks')} | {f2.get('downloaded_pack_bytes')} | {f2.get('reused_cache_raw_bytes')} | {n2.get('range_requests')} | {n2.get('head_requests')} | {n2.get('if_range_used')} | {n2.get('etag_observed')} |

## Verify/audit

- verify_v1_remote_ok: `{(result.get('verify_v1_remote') or {}).get('ok')}`
- verify_v2_remote_ok: `{(result.get('verify_v2_remote') or {}).get('ok')}`
- audit_v2_ok: `{audit.get('ok')}`
- files_expected: `{audit.get('files_expected')}`
- files_ok: `{audit.get('files_ok')}`
- missing_count: `{audit.get('missing_count')}`
- corrupt_count: `{audit.get('corrupt_count')}`

## Interpretation

This is a MinIO/S3-compatible pilot through the MinIO `mc` CLI. If `preflight_only` is true or MinIO/mc is not configured, the report is a readiness report, not a successful cloud/storage run. A real success requires object upload, anonymous/public GET configuration or equivalent public_base_url, remote verify, remote fetch/update, and audit OK.
"""


def _alpha37_minio_report_html(result: dict) -> str:
    import html
    md = _alpha37_minio_report_markdown(result)
    f1 = result.get("fetch_v1") or {}
    f2 = result.get("fetch_v2") or {}
    n1 = f1.get("network") or {}
    n2 = f2.get("network") or {}
    rows = []
    for name, f, n in [("fetch_v1", f1, n1), ("fetch_v2", f2, n2)]:
        rows.append(
            "<tr>"
            f"<td><code>{html.escape(name)}</code></td>"
            f"<td>{html.escape(str(f.get('ok')))}</td>"
            f"<td>{html.escape(str(f.get('downloaded_chunks')))}</td>"
            f"<td>{html.escape(str(f.get('cache_hit_chunks')))}</td>"
            f"<td>{html.escape(str(f.get('downloaded_pack_bytes')))}</td>"
            f"<td>{html.escape(str(f.get('reused_cache_raw_bytes')))}</td>"
            f"<td>{html.escape(str(n.get('range_requests')))}</td>"
            f"<td>{html.escape(str(n.get('head_requests')))}</td>"
            f"<td>{html.escape(str(n.get('if_range_used')))}</td>"
            f"<td>{html.escape(str(n.get('etag_observed')))}</td>"
            "</tr>"
        )
    ok_class = "ok" if result.get("ok") else "bad"
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>CLD2 alpha37 MinIO/S3 pilot</title>
<style>body{{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:2rem;line-height:1.45;background:#fafafa}}table{{border-collapse:collapse;width:100%;background:white}}td,th{{border:1px solid #ddd;padding:.5rem;text-align:left}}th{{background:#eee}}code{{background:#f3f3f3;padding:.1rem .25rem;border-radius:4px}}pre{{background:#111;color:#eee;padding:1rem;overflow:auto}}.badge{{padding:.25rem .7rem;border-radius:999px;font-weight:700}}.ok{{background:#e7f7ed;color:#126b35}}.bad{{background:#fdeaea;color:#8b1a1a}}.card{{background:white;border:1px solid #ddd;border-radius:12px;padding:1rem;margin:1rem 0}}</style></head>
<body><h1>CLD2 alpha37 MinIO/S3-compatible pilot</h1><p>Status: <span class="badge {ok_class}">{html.escape(str(result.get('ok')))}</span></p>
<div class="card"><p>Mode: <code>{html.escape(str(result.get('mode')))}</code></p><p>Preflight only: <code>{html.escape(str(result.get('preflight_only')))}</code></p><p>Endpoint: <code>{html.escape(str(result.get('endpoint')))}</code></p><p>Bucket: <code>{html.escape(str(result.get('bucket')))}</code></p><p>Prefix: <code>{html.escape(str(result.get('prefix')))}</code></p></div>
<table><thead><tr><th>Step</th><th>OK</th><th>Downloaded chunks</th><th>Cache-hit chunks</th><th>Downloaded pack bytes</th><th>Reused cache raw bytes</th><th>Range requests</th><th>HEAD requests</th><th>If-Range used</th><th>ETag observed</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
<h2>Markdown source</h2><pre>{html.escape(md)}</pre></body></html>"""


def run_minio_pilot(
    out_dir: str | Path,
    *,
    file_size: str = "8MiB",
    endpoint: str = "",
    access_key: str = "",
    secret_key: str = "",
    bucket: str = "cld2-alpha37-bucket",
    prefix: str = "releases",
    alias: str = "cld2alpha37",
    mc_path: str = "mc",
    public_base_url: str = "",
    preflight_only: bool = False,
    skip_upload: bool = False,
    codec: str = "zstd",
    chunker: str = "fastcdc",
    fixed_size: str = "1MiB",
    chunk_min: str = "64KiB",
    chunk_avg: str = "256KiB",
    chunk_max: str = "1MiB",
    fastcdc_stride: int = 4,
    http_retries: int = 3,
    http_backoff: float = 0.05,
    parallel: int = 1,
    cld2_attach_retries: int = 4,
    cld2_attach_backoff: float = 2.0,
    make_review: bool = False,
    keep_heavy: bool = True,
    max_mb: float = 10.0,
) -> dict:
    """Run alpha37 MinIO/S3-compatible pilot via MinIO mc CLI.

    Real pilot requirements:
      - MinIO server reachable at endpoint
      - MinIO mc CLI installed
      - access/secret keys
      - anonymous download configured by mc anonymous set download
    """
    out = Path(out_dir)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    mc_found = shutil.which(mc_path) is not None
    preflight = {
        "mc_path": mc_path,
        "mc_found": mc_found,
        "endpoint_configured": bool(endpoint),
        "access_key_configured": bool(access_key),
        "secret_key_configured": bool(secret_key),
        "can_run_real_pilot": bool(mc_found and endpoint and access_key and secret_key),
        "note": "",
    }
    if not preflight["can_run_real_pilot"]:
        preflight["note"] = "MinIO/mc not fully configured; this run is a readiness/preflight report, not a real object-storage benchmark."

    pair_root = out / "pairs"
    v1, v2 = _alpha33_write_network_pair(pair_root, file_size=file_size)
    repos_src = out / "repos_src"
    repos_src.mkdir(parents=True)
    repo_v1_src = repos_src / "release_v1.cldrepo"
    repo_v2_src = repos_src / "release_v2.cldrepo"
    make_repo(v1, repo_v1_src, release_id="alpha37-minio-v1", release_seq=1, chunker=chunker, fixed_size=fixed_size, chunk_min=chunk_min, chunk_avg=chunk_avg, chunk_max=chunk_max, fastcdc_stride=fastcdc_stride, codec=codec, force=True)
    make_repo(v2, repo_v2_src, release_id="alpha37-minio-v2", release_seq=2, chunker=chunker, fixed_size=fixed_size, chunk_min=chunk_min, chunk_avg=chunk_avg, chunk_max=chunk_max, fastcdc_stride=fastcdc_stride, codec=codec, force=True)
    local_diff = diff_repos(repo_v1_src, repo_v2_src)

    endpoint_clean = endpoint.rstrip("/")
    public_root = (public_base_url.rstrip("/") if public_base_url else (f"{endpoint_clean}/{bucket}" if endpoint_clean else ""))
    prefix_clean = prefix.strip("/")
    repo_v1_key = f"{prefix_clean}/release_v1.cldrepo"
    repo_v2_key = f"{prefix_clean}/release_v2.cldrepo"
    repo_v1_url = f"{public_root}/{repo_v1_key}/" if public_root else ""
    repo_v2_url = f"{public_root}/{repo_v2_key}/" if public_root else ""

    mc_commands = []
    fetch_v1 = {}
    fetch_v2 = {}
    audit_v2 = {}
    verify_v1 = {}
    verify_v2 = {}
    cache = out / "cache"
    install_v1 = out / "install_v1"
    install_v2 = out / "install_v2"

    if preflight["can_run_real_pilot"] and not preflight_only:
        mc_commands.append(_alpha37_run_process([mc_path, "alias", "set", alias, endpoint_clean, access_key, secret_key], timeout=60))
        mc_commands.append(_alpha37_run_process([mc_path, "mb", "--ignore-existing", f"{alias}/{bucket}"], timeout=60))
        if not skip_upload:
            mc_commands.append(_alpha37_run_process([mc_path, "cp", "--recursive", str(repo_v1_src), f"{alias}/{bucket}/{prefix_clean}/"], timeout=240))
            mc_commands.append(_alpha37_run_process([mc_path, "cp", "--recursive", str(repo_v2_src), f"{alias}/{bucket}/{prefix_clean}/"], timeout=240))
        mc_commands.append(_alpha37_run_process([mc_path, "anonymous", "set", "download", f"{alias}/{bucket}/{prefix_clean}"], timeout=60))

        # Only attempt remote fetch if upload/public config commands succeeded enough.
        upload_ok = all(c.get("ok") for c in mc_commands)
        if upload_ok and repo_v1_url and repo_v2_url:
            verify_v1 = verify_repo(repo_v1_url, deep=False)
            verify_v2 = verify_repo(repo_v2_url, deep=False)
            fetch_v1 = fetch_install(repo_v1_url, install_v1, cache_dir=cache, verify=True, http_retries=http_retries, http_backoff=http_backoff, parallel=parallel)
            fetch_v2 = fetch_install(repo_v2_url, install_v2, cache_dir=cache, from_installed=install_v1, verify=True, http_retries=http_retries, http_backoff=http_backoff, parallel=parallel)
            audit_v2 = audit_install(repo_v2_url, install_v2)
    else:
        mc_commands.append({"cmd": [mc_path, "--version"], "returncode": 0 if mc_found else 127, "stdout": "", "stderr": "" if mc_found else "mc not found", "ok": bool(mc_found)})

    result = {
        "schema": "CoreLangDistribution/MinioS3Pilot",
        "version": "2.0-alpha37",
        "ok": bool((not preflight_only) and preflight["can_run_real_pilot"] and verify_v1.get("ok") and verify_v2.get("ok") and fetch_v1.get("ok") and fetch_v2.get("ok") and audit_v2.get("ok")),
        "mode": "minio-s3-compatible-via-mc",
        "preflight_only": bool(preflight_only),
        "preflight": preflight,
        "out_dir": str(out),
        "file_size": file_size,
        "codec": codec,
        "chunker": chunker,
        "endpoint": endpoint,
        "bucket": bucket,
        "prefix": prefix,
        "alias": alias,
        "public_base_url": public_root,
        "repo_v1_url": repo_v1_url,
        "repo_v2_url": repo_v2_url,
        "repo_v1_key": repo_v1_key,
        "repo_v2_key": repo_v2_key,
        "mc_commands": mc_commands,
        "local_diff": local_diff,
        "verify_v1_remote": verify_v1,
        "verify_v2_remote": verify_v2,
        "fetch_v1": fetch_v1,
        "fetch_v2": fetch_v2,
        "audit_v2": audit_v2,
    }

    (out / "minio_pilot_result.json").write_text(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    (out / "minio_pilot_report.md").write_text(_alpha37_minio_report_markdown(result), encoding="utf-8")
    (out / "index.html").write_text(_alpha37_minio_report_html(result), encoding="utf-8")
    with (out / "minio_pilot_summary.csv").open("w", newline="", encoding="utf-8") as f:
        fieldnames = ["step", "ok", "downloaded_chunks", "cache_hit_chunks", "downloaded_pack_bytes", "reused_cache_raw_bytes", "range_requests", "head_requests", "if_range_used", "etag_observed"]
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for step in ["fetch_v1", "fetch_v2"]:
            fr = result.get(step, {}) or {}
            net = fr.get("network") or {}
            w.writerow({
                "step": step,
                "ok": fr.get("ok"),
                "downloaded_chunks": fr.get("downloaded_chunks"),
                "cache_hit_chunks": fr.get("cache_hit_chunks"),
                "downloaded_pack_bytes": fr.get("downloaded_pack_bytes"),
                "reused_cache_raw_bytes": fr.get("reused_cache_raw_bytes"),
                "range_requests": net.get("range_requests"),
                "head_requests": net.get("head_requests"),
                "if_range_used": net.get("if_range_used"),
                "etag_observed": net.get("etag_observed"),
            })
    if not keep_heavy:
        for heavy in [pair_root, repos_src, cache, install_v1, install_v2]:
            shutil.rmtree(heavy, ignore_errors=True)
    if make_review:
        result["review_zip"] = make_review_zip(out, str(out) + "_REVIEW.zip", max_mb=max_mb)
        (out / "minio_pilot_result.json").write_text(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
        try:
            _zip_out = result["review_zip"].get("zip_out") if isinstance(result.get("review_zip"), dict) else result.get("review_zip")
            if _zip_out:
                with zipfile.ZipFile(_zip_out, "a", compression=zipfile.ZIP_DEFLATED) as _zf:
                    for _name in ["minio_pilot_result.json", "minio_pilot_summary.csv", "minio_pilot_report.md"]:
                        _p = out / _name
                        if _p.exists() and _name not in _zf.namelist():
                            _zf.write(_p, arcname=_name)
        except Exception:
            pass
    return result



def _alpha38_sum_file_bytes(path: Path) -> int:
    path = Path(path)
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def _alpha38_step_metrics(fetch: dict) -> dict:
    fetch = fetch or {}
    net = fetch.get("network") or {}
    downloaded_chunks = int(fetch.get("downloaded_chunks") or 0)
    cache_hit_chunks = int(fetch.get("cache_hit_chunks") or 0)
    total_chunks = downloaded_chunks + cache_hit_chunks
    downloaded_pack_bytes = int(fetch.get("downloaded_pack_bytes") or 0)
    reused_cache_raw_bytes = int(fetch.get("reused_cache_raw_bytes") or 0)
    raw_total = downloaded_pack_bytes + reused_cache_raw_bytes
    return {
        "ok": bool(fetch.get("ok")),
        "downloaded_chunks": downloaded_chunks,
        "cache_hit_chunks": cache_hit_chunks,
        "total_chunks_seen": total_chunks,
        "cache_hit_ratio": (cache_hit_chunks / total_chunks) if total_chunks else None,
        "downloaded_pack_bytes": downloaded_pack_bytes,
        "reused_cache_raw_bytes": reused_cache_raw_bytes,
        "downloaded_vs_reused_ratio": (downloaded_pack_bytes / raw_total) if raw_total else None,
        "range_requests": net.get("range_requests"),
        "head_requests": net.get("head_requests"),
        "if_range_used": net.get("if_range_used"),
        "etag_observed": net.get("etag_observed"),
    }


def _alpha38_full_repo_ratio(downloaded_pack_bytes: int, full_repo_bytes: int) -> float | None:
    if not full_repo_bytes:
        return None
    return downloaded_pack_bytes / full_repo_bytes


def _alpha38_minio_robustness_markdown(result: dict) -> str:
    cold = result.get("cold_fetch") or {}
    warm = result.get("warm_update") or {}
    full = result.get("full_transfer_estimate") or {}
    base = result.get("minio_pilot") or {}
    return f"""# CLD2 alpha38 MinIO robustness / cold-warm report

## Summary

- ok: `{result.get('ok')}`
- real_run: `{result.get('real_run')}`
- endpoint: `{base.get('endpoint')}`
- bucket: `{base.get('bucket')}`
- prefix: `{base.get('prefix')}`
- repo_v1_url: `{base.get('repo_v1_url')}`
- repo_v2_url: `{base.get('repo_v2_url')}`

## Cold vs warm

| Phase | OK | Downloaded chunks | Cache-hit chunks | Cache hit ratio | Downloaded pack bytes | Reused cache raw bytes | Range requests | HEAD requests | If-Range | ETag |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| cold fetch v1 | {cold.get('ok')} | {cold.get('downloaded_chunks')} | {cold.get('cache_hit_chunks')} | {cold.get('cache_hit_ratio')} | {cold.get('downloaded_pack_bytes')} | {cold.get('reused_cache_raw_bytes')} | {cold.get('range_requests')} | {cold.get('head_requests')} | {cold.get('if_range_used')} | {cold.get('etag_observed')} |
| warm update v2 | {warm.get('ok')} | {warm.get('downloaded_chunks')} | {warm.get('cache_hit_chunks')} | {warm.get('cache_hit_ratio')} | {warm.get('downloaded_pack_bytes')} | {warm.get('reused_cache_raw_bytes')} | {warm.get('range_requests')} | {warm.get('head_requests')} | {warm.get('if_range_used')} | {warm.get('etag_observed')} |

## Full transfer estimate

| Metric | Bytes / Ratio |
|---|---:|
| repo_v1_full_bytes | {full.get('repo_v1_full_bytes')} |
| repo_v2_full_bytes | {full.get('repo_v2_full_bytes')} |
| cold_downloaded_pack_bytes | {full.get('cold_downloaded_pack_bytes')} |
| warm_downloaded_pack_bytes | {full.get('warm_downloaded_pack_bytes')} |
| cold_vs_full_repo_ratio | {full.get('cold_vs_full_repo_ratio')} |
| warm_vs_full_repo_ratio | {full.get('warm_vs_full_repo_ratio')} |

## Verify/audit

- verify_v1_remote_ok: `{(base.get('verify_v1_remote') or {}).get('ok')}`
- verify_v2_remote_ok: `{(base.get('verify_v2_remote') or {}).get('ok')}`
- audit_v2_ok: `{(base.get('audit_v2') or {}).get('ok')}`

## Interpretation

This report does not replace the raw MinIO pilot. It makes it easier to read the object-storage behavior:

- cold fetch is the first install;
- warm update is v2 installed from an existing v1 plus cache;
- cache hit ratio explains how much of the update reused previous chunks;
- full transfer estimate compares CLD2 downloaded pack bytes to the local repo object set size.

This is still a local MinIO pilot, not AWS S3 production evidence.
"""


def _alpha38_minio_robustness_html(result: dict) -> str:
    import html
    md = _alpha38_minio_robustness_markdown(result)
    cold = result.get("cold_fetch") or {}
    warm = result.get("warm_update") or {}
    full = result.get("full_transfer_estimate") or {}
    rows = []
    for name, data in [("cold_fetch_v1", cold), ("warm_update_v2", warm)]:
        rows.append(
            "<tr>"
            f"<td><code>{html.escape(name)}</code></td>"
            f"<td>{html.escape(str(data.get('ok')))}</td>"
            f"<td>{html.escape(str(data.get('downloaded_chunks')))}</td>"
            f"<td>{html.escape(str(data.get('cache_hit_chunks')))}</td>"
            f"<td>{html.escape(str(data.get('cache_hit_ratio')))}</td>"
            f"<td>{html.escape(str(data.get('downloaded_pack_bytes')))}</td>"
            f"<td>{html.escape(str(data.get('reused_cache_raw_bytes')))}</td>"
            f"<td>{html.escape(str(data.get('range_requests')))}</td>"
            f"<td>{html.escape(str(data.get('etag_observed')))}</td>"
            "</tr>"
        )
    ok_class = "ok" if result.get("ok") else "bad"
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>CLD2 alpha38 MinIO robustness</title>
<style>body{{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:2rem;line-height:1.45;background:#fafafa}}table{{border-collapse:collapse;width:100%;background:white;margin:1rem 0}}td,th{{border:1px solid #ddd;padding:.5rem;text-align:left}}th{{background:#eee}}code{{background:#f3f3f3;padding:.1rem .25rem;border-radius:4px}}pre{{background:#111;color:#eee;padding:1rem;overflow:auto}}.badge{{padding:.25rem .7rem;border-radius:999px;font-weight:700}}.ok{{background:#e7f7ed;color:#126b35}}.bad{{background:#fdeaea;color:#8b1a1a}}.card{{background:white;border:1px solid #ddd;border-radius:12px;padding:1rem;margin:1rem 0}}</style></head>
<body><h1>CLD2 alpha38 MinIO robustness / cold-warm</h1><p>Status: <span class="badge {ok_class}">{html.escape(str(result.get('ok')))}</span></p>
<div class="card"><p>Real run: <code>{html.escape(str(result.get('real_run')))}</code></p><p>Warm cache hit ratio: <code>{html.escape(str(warm.get('cache_hit_ratio')))}</code></p><p>Warm vs full repo ratio: <code>{html.escape(str(full.get('warm_vs_full_repo_ratio')))}</code></p></div>
<table><thead><tr><th>Phase</th><th>OK</th><th>Downloaded chunks</th><th>Cache-hit chunks</th><th>Cache hit ratio</th><th>Downloaded pack bytes</th><th>Reused cache raw bytes</th><th>Range requests</th><th>ETag</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
<h2>Markdown source</h2><pre>{html.escape(md)}</pre></body></html>"""


def run_minio_robustness_pilot(
    out_dir: str | Path,
    *,
    file_size: str = "8MiB",
    endpoint: str = "",
    access_key: str = "",
    secret_key: str = "",
    bucket: str = "cld2-alpha38-bucket",
    prefix: str = "releases",
    alias: str = "cld2alpha38",
    mc_path: str = "mc",
    public_base_url: str = "",
    preflight_only: bool = False,
    skip_upload: bool = False,
    codec: str = "zstd",
    chunker: str = "fastcdc",
    fixed_size: str = "1MiB",
    chunk_min: str = "64KiB",
    chunk_avg: str = "256KiB",
    chunk_max: str = "1MiB",
    fastcdc_stride: int = 4,
    http_retries: int = 3,
    http_backoff: float = 0.05,
    parallel: int = 1,
    cld2_attach_retries: int = 4,
    cld2_attach_backoff: float = 2.0,
    make_review: bool = False,
    keep_heavy: bool = True,
    max_mb: float = 10.0,
) -> dict:
    """Run alpha38 MinIO pilot and add cold/warm/cache/full-transfer metrics."""
    out = Path(out_dir)
    base_result = run_minio_pilot(
        out,
        file_size=file_size,
        endpoint=endpoint,
        access_key=access_key,
        secret_key=secret_key,
        bucket=bucket,
        prefix=prefix,
        alias=alias,
        mc_path=mc_path,
        public_base_url=public_base_url,
        preflight_only=preflight_only,
        skip_upload=skip_upload,
        codec=codec,
        chunker=chunker,
        fixed_size=fixed_size,
        chunk_min=chunk_min,
        chunk_avg=chunk_avg,
        chunk_max=chunk_max,
        fastcdc_stride=fastcdc_stride,
        http_retries=http_retries,
        http_backoff=http_backoff,
        parallel=parallel,
        make_review=False,
        keep_heavy=True,
        max_mb=max_mb,
    )
    repo_v1_full_bytes = _alpha38_sum_file_bytes(out / "repos_src" / "release_v1.cldrepo")
    repo_v2_full_bytes = _alpha38_sum_file_bytes(out / "repos_src" / "release_v2.cldrepo")
    cold = _alpha38_step_metrics(base_result.get("fetch_v1") or {})
    warm = _alpha38_step_metrics(base_result.get("fetch_v2") or {})
    full = {
        "repo_v1_full_bytes": repo_v1_full_bytes,
        "repo_v2_full_bytes": repo_v2_full_bytes,
        "cold_downloaded_pack_bytes": cold.get("downloaded_pack_bytes"),
        "warm_downloaded_pack_bytes": warm.get("downloaded_pack_bytes"),
        "cold_vs_full_repo_ratio": _alpha38_full_repo_ratio(cold.get("downloaded_pack_bytes") or 0, repo_v1_full_bytes),
        "warm_vs_full_repo_ratio": _alpha38_full_repo_ratio(warm.get("downloaded_pack_bytes") or 0, repo_v2_full_bytes),
    }
    real_run = bool(
        base_result.get("ok")
        and not base_result.get("preflight_only")
        and (base_result.get("preflight") or {}).get("can_run_real_pilot")
        and (base_result.get("verify_v1_remote") or {}).get("ok")
        and (base_result.get("verify_v2_remote") or {}).get("ok")
        and (base_result.get("audit_v2") or {}).get("ok")
    )
    result = {
        "schema": "CoreLangDistribution/MinioRobustnessPilot",
        "version": "2.0-alpha38",
        "ok": bool(real_run),
        "real_run": bool(real_run),
        "out_dir": str(out),
        "minio_pilot": base_result,
        "cold_fetch": cold,
        "warm_update": warm,
        "full_transfer_estimate": full,
        "notes": [
            "full_transfer_estimate uses local repo object-set bytes, not a separately downloaded full object transfer",
            "this remains a local MinIO pilot, not AWS S3 production evidence",
        ],
    }
    (out / "minio_robustness_result.json").write_text(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    (out / "minio_robustness_report.md").write_text(_alpha38_minio_robustness_markdown(result), encoding="utf-8")
    (out / "index.html").write_text(_alpha38_minio_robustness_html(result), encoding="utf-8")
    with (out / "minio_robustness_summary.csv").open("w", newline="", encoding="utf-8") as f:
        fieldnames = ["phase", "ok", "downloaded_chunks", "cache_hit_chunks", "cache_hit_ratio", "downloaded_pack_bytes", "reused_cache_raw_bytes", "range_requests", "head_requests", "if_range_used", "etag_observed"]
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for phase, data in [("cold_fetch_v1", cold), ("warm_update_v2", warm)]:
            row = {"phase": phase}
            row.update({k: data.get(k) for k in fieldnames if k != "phase"})
            w.writerow(row)
    if not keep_heavy:
        for heavy in [out / "pairs", out / "repos_src", out / "cache", out / "install_v1", out / "install_v2"]:
            shutil.rmtree(heavy, ignore_errors=True)
    if make_review:
        result["review_zip"] = make_review_zip(out, str(out) + "_REVIEW.zip", max_mb=max_mb)
        (out / "minio_robustness_result.json").write_text(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
        try:
            _zip_out = result["review_zip"].get("zip_out") if isinstance(result.get("review_zip"), dict) else result.get("review_zip")
            if _zip_out:
                with zipfile.ZipFile(_zip_out, "a", compression=zipfile.ZIP_DEFLATED) as _zf:
                    for _name in ["minio_robustness_result.json", "minio_robustness_summary.csv", "minio_robustness_report.md", "minio_pilot_result.json", "minio_pilot_summary.csv", "minio_pilot_report.md"]:
                        _p = out / _name
                        if _p.exists() and _name not in _zf.namelist():
                            _zf.write(_p, arcname=_name)
        except Exception:
            pass
    return result



def _alpha39_parse_counts(value: str | list[int] | tuple[int, ...]) -> list[int]:
    if isinstance(value, (list, tuple)):
        return [int(x) for x in value]
    out = []
    for part in str(value).split(","):
        part = part.strip()
        if part:
            out.append(int(part))
    return out or [1]


def _alpha39_repo_file_list(repo_dir: Path) -> list[dict]:
    repo_dir = Path(repo_dir)
    items = []
    if not repo_dir.exists():
        return items
    for p in sorted(repo_dir.rglob("*")):
        if p.is_file():
            rel = p.relative_to(repo_dir).as_posix()
            items.append({"path": rel, "bytes": p.stat().st_size})
    return items


def _alpha39_download_url(url: str, dest: Path, *, timeout: float = 60.0) -> dict:
    import urllib.request
    from time import perf_counter
    dest.parent.mkdir(parents=True, exist_ok=True)
    t0 = perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = r.read()
        dest.write_bytes(data)
        dt = perf_counter() - t0
        return {"url": url, "ok": True, "bytes": len(data), "seconds": dt, "error": ""}
    except Exception as e:
        dt = perf_counter() - t0
        return {"url": url, "ok": False, "bytes": 0, "seconds": dt, "error": str(e)}


def _alpha39_full_download_repo(base_url: str, repo_key: str, repo_src_dir: Path, dest_dir: Path) -> dict:
    files = _alpha39_repo_file_list(repo_src_dir)
    results = []
    total_bytes = 0
    ok_count = 0
    base = base_url.rstrip("/")
    key = repo_key.strip("/")
    for item in files:
        rel = item["path"]
        url = f"{base}/{key}/{rel}"
        res = _alpha39_download_url(url, dest_dir / rel)
        res["path"] = rel
        res["expected_bytes"] = item["bytes"]
        res["bytes_match"] = bool(res.get("bytes") == item["bytes"])
        results.append(res)
        if res.get("ok") and res.get("bytes_match"):
            ok_count += 1
            total_bytes += int(res.get("bytes") or 0)
    return {
        "ok": bool(ok_count == len(files) and files),
        "file_count": len(files),
        "ok_count": ok_count,
        "downloaded_bytes": total_bytes,
        "files": results,
    }


def _alpha39_cost_model(full_v2_bytes: int, cld2_warm_bytes: int, *, cost_per_gb: float, counts: list[int], currency: str) -> dict:
    gb = 1024 ** 3
    rows = []
    for n in counts:
        full_cost = (full_v2_bytes * n / gb) * cost_per_gb
        cld2_cost = (cld2_warm_bytes * n / gb) * cost_per_gb
        rows.append({
            "download_count": int(n),
            "full_bytes_total": int(full_v2_bytes * n),
            "cld2_warm_bytes_total": int(cld2_warm_bytes * n),
            "full_cost": full_cost,
            "cld2_warm_cost": cld2_cost,
            "savings_cost": full_cost - cld2_cost,
            "savings_ratio": (1.0 - (cld2_warm_bytes / full_v2_bytes)) if full_v2_bytes else None,
        })
    return {"currency": currency, "cost_per_gb": cost_per_gb, "rows": rows}


def _alpha39_markdown(result: dict) -> str:
    robust = result.get("minio_robustness") or {}
    cold = robust.get("cold_fetch") or {}
    warm = robust.get("warm_update") or {}
    baseline = result.get("full_object_baseline") or {}
    cost = result.get("cost_model") or {}
    rows = "\n".join([
        f"| {r.get('download_count')} | {r.get('full_bytes_total')} | {r.get('cld2_warm_bytes_total')} | {r.get('full_cost'):.8f} | {r.get('cld2_warm_cost'):.8f} | {r.get('savings_cost'):.8f} |"
        for r in cost.get("rows", [])
    ])
    return f"""# CLD2 alpha39 MinIO full-object baseline + cost report

## Summary

- ok: `{result.get('ok')}`
- real_run: `{result.get('real_run')}`
- endpoint: `{(robust.get('minio_pilot') or {}).get('endpoint')}`
- bucket: `{(robust.get('minio_pilot') or {}).get('bucket')}`
- prefix: `{(robust.get('minio_pilot') or {}).get('prefix')}`

## CLD2 cold/warm

| Phase | OK | Downloaded chunks | Cache-hit chunks | Cache hit ratio | Downloaded pack bytes | Reused cache raw bytes |
|---|---:|---:|---:|---:|---:|---:|
| cold fetch v1 | {cold.get('ok')} | {cold.get('downloaded_chunks')} | {cold.get('cache_hit_chunks')} | {cold.get('cache_hit_ratio')} | {cold.get('downloaded_pack_bytes')} | {cold.get('reused_cache_raw_bytes')} |
| warm update v2 | {warm.get('ok')} | {warm.get('downloaded_chunks')} | {warm.get('cache_hit_chunks')} | {warm.get('cache_hit_ratio')} | {warm.get('downloaded_pack_bytes')} | {warm.get('reused_cache_raw_bytes')} |

## Real full-object baseline

| Baseline | OK | File count | OK count | Downloaded bytes |
|---|---:|---:|---:|---:|
| full v1 repo download | {(baseline.get('v1') or {}).get('ok')} | {(baseline.get('v1') or {}).get('file_count')} | {(baseline.get('v1') or {}).get('ok_count')} | {(baseline.get('v1') or {}).get('downloaded_bytes')} |
| full v2 repo download | {(baseline.get('v2') or {}).get('ok')} | {(baseline.get('v2') or {}).get('file_count')} | {(baseline.get('v2') or {}).get('ok_count')} | {(baseline.get('v2') or {}).get('downloaded_bytes')} |

## Ratios

- cold_vs_full_v1_ratio: `{result.get('cold_vs_full_v1_ratio')}`
- warm_vs_full_v2_ratio: `{result.get('warm_vs_full_v2_ratio')}`
- warm_savings_vs_full_v2_ratio: `{result.get('warm_savings_vs_full_v2_ratio')}`

## Cost model

Currency: `{cost.get('currency')}`  
Cost per GB: `{cost.get('cost_per_gb')}`

| Downloads | Full bytes total | CLD2 warm bytes total | Full cost | CLD2 warm cost | Savings |
|---:|---:|---:|---:|---:|---:|
{rows}

## Interpretation

Alpha39 replaces the alpha38 local full-transfer estimate with an actual full-object download baseline from MinIO. It still runs on local MinIO, not AWS S3, but it gives a cleaner comparison between a full repo-object download and a CLD2 warm update.
"""


def _alpha39_html(result: dict) -> str:
    import html
    md = _alpha39_markdown(result)
    ok_class = "ok" if result.get("ok") else "bad"
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>CLD2 alpha39 full baseline cost</title>
<style>body{{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:2rem;line-height:1.45;background:#fafafa}}pre{{background:#111;color:#eee;padding:1rem;overflow:auto}}code{{background:#f3f3f3;padding:.1rem .25rem;border-radius:4px}}.badge{{padding:.25rem .7rem;border-radius:999px;font-weight:700}}.ok{{background:#e7f7ed;color:#126b35}}.bad{{background:#fdeaea;color:#8b1a1a}}</style></head>
<body><h1>CLD2 alpha39 full-object baseline + cost</h1><p>Status: <span class="badge {ok_class}">{html.escape(str(result.get('ok')))}</span></p>
<p>Warm vs full v2 ratio: <code>{html.escape(str(result.get('warm_vs_full_v2_ratio')))}</code></p>
<p>Warm savings vs full v2: <code>{html.escape(str(result.get('warm_savings_vs_full_v2_ratio')))}</code></p>
<h2>Markdown source</h2><pre>{html.escape(md)}</pre></body></html>"""


def run_minio_full_baseline_pilot(
    out_dir: str | Path,
    *,
    file_size: str = "8MiB",
    endpoint: str = "",
    access_key: str = "",
    secret_key: str = "",
    bucket: str = "cld2-alpha39-bucket",
    prefix: str = "releases",
    alias: str = "cld2alpha39",
    mc_path: str = "mc",
    public_base_url: str = "",
    preflight_only: bool = False,
    skip_upload: bool = False,
    codec: str = "zstd",
    chunker: str = "fastcdc",
    fixed_size: str = "1MiB",
    chunk_min: str = "64KiB",
    chunk_avg: str = "256KiB",
    chunk_max: str = "1MiB",
    fastcdc_stride: int = 4,
    http_retries: int = 3,
    http_backoff: float = 0.05,
    parallel: int = 1,
    cost_per_gb: float = 0.04,
    download_counts: str | list[int] = "1,10,1000,10000",
    currency: str = "EUR",
    make_review: bool = False,
    keep_heavy: bool = True,
    max_mb: float = 10.0,
) -> dict:
    """Run alpha39 MinIO CLD2 warm update + real full-object baseline + cost model."""
    out = Path(out_dir)
    robust = run_minio_robustness_pilot(
        out,
        file_size=file_size,
        endpoint=endpoint,
        access_key=access_key,
        secret_key=secret_key,
        bucket=bucket,
        prefix=prefix,
        alias=alias,
        mc_path=mc_path,
        public_base_url=public_base_url,
        preflight_only=preflight_only,
        skip_upload=skip_upload,
        codec=codec,
        chunker=chunker,
        fixed_size=fixed_size,
        chunk_min=chunk_min,
        chunk_avg=chunk_avg,
        chunk_max=chunk_max,
        fastcdc_stride=fastcdc_stride,
        http_retries=http_retries,
        http_backoff=http_backoff,
        parallel=parallel,
        make_review=False,
        keep_heavy=True,
        max_mb=max_mb,
    )
    pilot = robust.get("minio_pilot") or {}
    public_root = (pilot.get("public_base_url") or "").rstrip("/")
    repo_v1_key = (pilot.get("repo_v1_key") or "").strip("/")
    repo_v2_key = (pilot.get("repo_v2_key") or "").strip("/")
    baseline = {"v1": {}, "v2": {}}
    if robust.get("real_run") and public_root and repo_v1_key and repo_v2_key:
        baseline["v1"] = _alpha39_full_download_repo(public_root, repo_v1_key, out / "repos_src" / "release_v1.cldrepo", out / "full_download_v1")
        baseline["v2"] = _alpha39_full_download_repo(public_root, repo_v2_key, out / "repos_src" / "release_v2.cldrepo", out / "full_download_v2")
    cold = robust.get("cold_fetch") or {}
    warm = robust.get("warm_update") or {}
    full_v1 = int((baseline.get("v1") or {}).get("downloaded_bytes") or 0)
    full_v2 = int((baseline.get("v2") or {}).get("downloaded_bytes") or 0)
    cold_bytes = int(cold.get("downloaded_pack_bytes") or 0)
    warm_bytes = int(warm.get("downloaded_pack_bytes") or 0)
    result = {
        "schema": "CoreLangDistribution/MinioFullBaselineCostPilot",
        "version": "2.0-alpha39",
        "ok": bool(robust.get("real_run") and (baseline.get("v1") or {}).get("ok") and (baseline.get("v2") or {}).get("ok")),
        "real_run": bool(robust.get("real_run")),
        "out_dir": str(out),
        "minio_robustness": robust,
        "full_object_baseline": baseline,
        "cold_vs_full_v1_ratio": (cold_bytes / full_v1) if full_v1 else None,
        "warm_vs_full_v2_ratio": (warm_bytes / full_v2) if full_v2 else None,
        "warm_savings_vs_full_v2_ratio": (1.0 - (warm_bytes / full_v2)) if full_v2 else None,
        "cost_model": _alpha39_cost_model(full_v2, warm_bytes, cost_per_gb=cost_per_gb, counts=_alpha39_parse_counts(download_counts), currency=currency),
        "notes": [
            "full baseline downloads every file/object listed in the local generated repo tree via MinIO public HTTP URLs",
            "still local MinIO, not AWS S3 production evidence",
        ],
    }
    (out / "minio_full_baseline_result.json").write_text(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    (out / "minio_full_baseline_report.md").write_text(_alpha39_markdown(result), encoding="utf-8")
    (out / "index.html").write_text(_alpha39_html(result), encoding="utf-8")
    with (out / "minio_full_baseline_summary.csv").open("w", newline="", encoding="utf-8") as f:
        fieldnames = ["metric", "value"]
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for k in ["cold_vs_full_v1_ratio", "warm_vs_full_v2_ratio", "warm_savings_vs_full_v2_ratio"]:
            w.writerow({"metric": k, "value": result.get(k)})
        w.writerow({"metric": "full_v1_downloaded_bytes", "value": full_v1})
        w.writerow({"metric": "full_v2_downloaded_bytes", "value": full_v2})
        w.writerow({"metric": "cld2_cold_downloaded_pack_bytes", "value": cold_bytes})
        w.writerow({"metric": "cld2_warm_downloaded_pack_bytes", "value": warm_bytes})
    with (out / "minio_cost_model.csv").open("w", newline="", encoding="utf-8") as f:
        rows = result["cost_model"]["rows"]
        fieldnames = ["download_count", "full_bytes_total", "cld2_warm_bytes_total", "full_cost", "cld2_warm_cost", "savings_cost", "savings_ratio"]
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    if not keep_heavy:
        for heavy in [out / "pairs", out / "repos_src", out / "cache", out / "install_v1", out / "install_v2", out / "full_download_v1", out / "full_download_v2"]:
            shutil.rmtree(heavy, ignore_errors=True)
    if make_review:
        result["review_zip"] = make_review_zip(out, str(out) + "_REVIEW.zip", max_mb=max_mb)
        (out / "minio_full_baseline_result.json").write_text(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
        try:
            _zip_out = result["review_zip"].get("zip_out") if isinstance(result.get("review_zip"), dict) else result.get("review_zip")
            if _zip_out:
                with zipfile.ZipFile(_zip_out, "a", compression=zipfile.ZIP_DEFLATED) as _zf:
                    for _name in [
                        "minio_full_baseline_result.json",
                        "minio_full_baseline_summary.csv",
                        "minio_full_baseline_report.md",
                        "minio_cost_model.csv",
                        "minio_robustness_result.json",
                        "minio_robustness_summary.csv",
                        "minio_robustness_report.md",
                        "minio_pilot_result.json",
                        "minio_pilot_summary.csv",
                        "minio_pilot_report.md",
                    ]:
                        _p = out / _name
                        if _p.exists() and _name not in _zf.namelist():
                            _zf.write(_p, arcname=_name)
        except Exception:
            pass
    return result



def _alpha40_parse_scenarios(value: str | list[str] | tuple[str, ...]) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(x).strip() for x in value if str(x).strip()]
    return [x.strip() for x in str(value).split(",") if x.strip()]


def _alpha40_fill_file(path: Path, size: int, *, seed: int = 1, pattern: bytes | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rnd = random.Random(seed)
    remaining = int(size)
    with path.open("wb") as f:
        if pattern:
            while remaining > 0:
                chunk = pattern[: min(len(pattern), remaining)]
                f.write(chunk)
                remaining -= len(chunk)
        else:
            # Deterministic pseudo-random bytes, no secrets, no os.urandom dependency.
            block_size = 1024 * 1024
            while remaining > 0:
                n = min(block_size, remaining)
                f.write(bytes(rnd.getrandbits(8) for _ in range(n)))
                remaining -= n


def _alpha40_mutate_sparse(src: Path, dst: Path, *, seed: int = 2, edits: int = 8, edit_size: int = 4096) -> None:
    shutil.copy2(src, dst)
    size = dst.stat().st_size
    if size <= 0:
        return
    rnd = random.Random(seed)
    with dst.open("r+b") as f:
        for _ in range(max(1, edits)):
            offset = rnd.randrange(0, max(1, size - min(edit_size, size)))
            f.seek(offset)
            f.write(bytes(rnd.getrandbits(8) for _ in range(min(edit_size, size - offset))))


def _alpha40_write_pair(root: Path, *, scenario: str, file_size: str | int = "64MiB", small_file_count: int = 256) -> tuple[Path, Path]:
    """Generate deterministic scenario pairs for alpha40 matrix.

    Scenarios:
    - normal: structured/repetitive with sparse update, expected friendly case.
    - high-entropy: unrelated pseudo-random v1/v2, expected adversarial/negative.
    - small-files: many small files with partial changes/additions.
    - heavy-change: structured but large changed region.
    """
    size = parse_size(file_size)
    scenario = scenario.strip().lower()
    v1 = Path(root) / "v1"
    v2 = Path(root) / "v2"
    if root.exists():
        shutil.rmtree(root)
    v1.mkdir(parents=True)
    v2.mkdir(parents=True)

    if scenario == "normal":
        # Use the already-validated friendly network pair.
        return _alpha33_write_network_pair(root, file_size=file_size)

    if scenario == "high-entropy":
        _alpha40_fill_file(v1 / "entropy.bin", size, seed=404, pattern=None)
        _alpha40_fill_file(v2 / "entropy.bin", size, seed=405, pattern=None)
        (v1 / "meta.json").write_text(json.dumps({"scenario": scenario, "version": 1}) + "\n", encoding="utf-8")
        (v2 / "meta.json").write_text(json.dumps({"scenario": scenario, "version": 2, "note": "unrelated entropy"}) + "\n", encoding="utf-8")
        return v1, v2

    if scenario == "heavy-change":
        pattern = (b"CLD2_ALPHA40_HEAVY_CHANGE_STRUCTURED_BLOCK\n" * 4096)
        _alpha40_fill_file(v1 / "data.bin", size, seed=1, pattern=pattern)
        shutil.copytree(v1, v2, dirs_exist_ok=True)
        # Rewrite the middle 40% with deterministic random bytes: intentionally hard update.
        p = v2 / "data.bin"
        start = size // 3
        length = max(1, int(size * 0.40))
        rnd = random.Random(909)
        with p.open("r+b") as f:
            f.seek(start)
            remaining = min(length, size - start)
            while remaining > 0:
                n = min(1024 * 1024, remaining)
                f.write(bytes(rnd.getrandbits(8) for _ in range(n)))
                remaining -= n
        (v2 / "change.log").write_text("heavy-change: middle region rewritten\n", encoding="utf-8")
        return v1, v2

    if scenario == "small-files":
        count = max(1, int(small_file_count))
        per = max(128, size // count)
        total = 0
        for i in range(count):
            group = f"group_{i % 16:02d}"
            name = f"file_{i:05d}.dat"
            payload = (f"CLD2 alpha40 small file {i}\n".encode("utf-8") * 256)
            this_size = min(per, max(128, size - total)) if total < size else per
            _alpha40_fill_file(v1 / group / name, this_size, seed=i, pattern=payload)
            total += this_size
        shutil.copytree(v1, v2, dirs_exist_ok=True)
        # Mutate about 10% of files and add 5% new files.
        mutate_count = max(1, count // 10)
        for i in range(mutate_count):
            p = v2 / f"group_{i % 16:02d}" / f"file_{i:05d}.dat"
            if p.exists():
                with p.open("ab") as f:
                    f.write((f"\nUPDATED small-file {i}\n".encode("utf-8") * 64))
        for j in range(max(1, count // 20)):
            idx = count + j
            _alpha40_fill_file(v2 / "added" / f"added_{idx:05d}.dat", per, seed=2000 + j, pattern=(b"ADDED_ALPHA40_SMALL_FILE\n" * 128))
        return v1, v2

    raise ValueError(f"unknown alpha40 scenario: {scenario}")


def _alpha40_run_matrix_scenario(
    scenario_out: Path,
    *,
    scenario: str,
    file_size: str,
    small_file_count: int,
    endpoint: str,
    access_key: str,
    secret_key: str,
    bucket: str,
    prefix: str,
    alias: str,
    mc_path: str,
    public_base_url: str,
    preflight_only: bool,
    skip_upload: bool,
    codec: str,
    chunker: str,
    fixed_size: str,
    chunk_min: str,
    chunk_avg: str,
    chunk_max: str,
    fastcdc_stride: int,
    http_retries: int,
    http_backoff: float,
    parallel: int,
    cost_per_gb: float,
    download_counts: str | list[int],
    currency: str,
) -> dict:
    if scenario_out.exists():
        shutil.rmtree(scenario_out)
    scenario_out.mkdir(parents=True)
    pair_root = scenario_out / "pairs"
    v1, v2 = _alpha40_write_pair(pair_root, scenario=scenario, file_size=file_size, small_file_count=small_file_count)

    repos_src = scenario_out / "repos_src"
    repos_src.mkdir(parents=True)
    repo_v1_src = repos_src / "release_v1.cldrepo"
    repo_v2_src = repos_src / "release_v2.cldrepo"
    make_repo(v1, repo_v1_src, release_id=f"alpha40-{scenario}-v1", release_seq=1, chunker=chunker, fixed_size=fixed_size, chunk_min=chunk_min, chunk_avg=chunk_avg, chunk_max=chunk_max, fastcdc_stride=fastcdc_stride, codec=codec, force=True)
    make_repo(v2, repo_v2_src, release_id=f"alpha40-{scenario}-v2", release_seq=2, chunker=chunker, fixed_size=fixed_size, chunk_min=chunk_min, chunk_avg=chunk_avg, chunk_max=chunk_max, fastcdc_stride=fastcdc_stride, codec=codec, force=True)
    local_diff = diff_repos(repo_v1_src, repo_v2_src)

    mc_found = shutil.which(mc_path) is not None
    preflight = {
        "mc_path": mc_path,
        "mc_found": mc_found,
        "endpoint_configured": bool(endpoint),
        "access_key_configured": bool(access_key),
        "secret_key_configured": bool(secret_key),
        "can_run_real_pilot": bool(mc_found and endpoint and access_key and secret_key),
        "note": "",
    }
    if not preflight["can_run_real_pilot"]:
        preflight["note"] = "MinIO/mc not fully configured; scenario is readiness/preflight only."

    endpoint_clean = endpoint.rstrip("/")
    public_root = (public_base_url.rstrip("/") if public_base_url else (f"{endpoint_clean}/{bucket}" if endpoint_clean else ""))
    prefix_clean = f"{prefix.strip('/')}/{scenario}".strip("/")
    repo_v1_key = f"{prefix_clean}/release_v1.cldrepo"
    repo_v2_key = f"{prefix_clean}/release_v2.cldrepo"
    repo_v1_url = f"{public_root}/{repo_v1_key}/" if public_root else ""
    repo_v2_url = f"{public_root}/{repo_v2_key}/" if public_root else ""

    mc_commands = []
    verify_v1 = {}
    verify_v2 = {}
    fetch_v1 = {}
    fetch_v2 = {}
    audit_v2 = {}
    baseline = {"v1": {}, "v2": {}}
    cache = scenario_out / "cache"
    install_v1 = scenario_out / "install_v1"
    install_v2 = scenario_out / "install_v2"

    if preflight["can_run_real_pilot"] and not preflight_only:
        mc_commands.append(_alpha37_run_process([mc_path, "alias", "set", alias, endpoint_clean, access_key, secret_key], timeout=60))
        mc_commands.append(_alpha37_run_process([mc_path, "mb", "--ignore-existing", f"{alias}/{bucket}"], timeout=60))
        if not skip_upload:
            mc_commands.append(_alpha37_run_process([mc_path, "cp", "--recursive", str(repo_v1_src), f"{alias}/{bucket}/{prefix_clean}/"], timeout=600))
            mc_commands.append(_alpha37_run_process([mc_path, "cp", "--recursive", str(repo_v2_src), f"{alias}/{bucket}/{prefix_clean}/"], timeout=600))
        mc_commands.append(_alpha37_run_process([mc_path, "anonymous", "set", "download", f"{alias}/{bucket}/{prefix_clean}"], timeout=60))
        upload_ok = all(c.get("ok") for c in mc_commands)
        if upload_ok and repo_v1_url and repo_v2_url:
            verify_v1 = verify_repo(repo_v1_url, deep=False)
            verify_v2 = verify_repo(repo_v2_url, deep=False)
            fetch_v1 = fetch_install(repo_v1_url, install_v1, cache_dir=cache, verify=True, http_retries=http_retries, http_backoff=http_backoff, parallel=parallel)
            fetch_v2 = fetch_install(repo_v2_url, install_v2, cache_dir=cache, from_installed=install_v1, verify=True, http_retries=http_retries, http_backoff=http_backoff, parallel=parallel)
            audit_v2 = audit_install(repo_v2_url, install_v2)
            baseline["v1"] = _alpha39_full_download_repo(public_root, repo_v1_key, repo_v1_src, scenario_out / "full_download_v1")
            baseline["v2"] = _alpha39_full_download_repo(public_root, repo_v2_key, repo_v2_src, scenario_out / "full_download_v2")
    else:
        mc_commands.append({"cmd": [mc_path, "--version"], "returncode": 0 if mc_found else 127, "stdout": "", "stderr": "" if mc_found else "mc not found", "ok": bool(mc_found)})

    cold = _alpha38_step_metrics(fetch_v1)
    warm = _alpha38_step_metrics(fetch_v2)
    full_v1 = int((baseline.get("v1") or {}).get("downloaded_bytes") or 0)
    full_v2 = int((baseline.get("v2") or {}).get("downloaded_bytes") or 0)
    cold_bytes = int(cold.get("downloaded_pack_bytes") or 0)
    warm_bytes = int(warm.get("downloaded_pack_bytes") or 0)
    cost_model = _alpha39_cost_model(full_v2, warm_bytes, cost_per_gb=cost_per_gb, counts=_alpha39_parse_counts(download_counts), currency=currency)
    result = {
        "scenario": scenario,
        "ok": bool(verify_v1.get("ok") and verify_v2.get("ok") and fetch_v1.get("ok") and fetch_v2.get("ok") and audit_v2.get("ok") and (baseline.get("v1") or {}).get("ok") and (baseline.get("v2") or {}).get("ok")),
        "real_run": bool(preflight["can_run_real_pilot"] and not preflight_only),
        "preflight_only": bool(preflight_only),
        "preflight": preflight,
        "file_size": file_size,
        "small_file_count": small_file_count,
        "codec": codec,
        "chunker": chunker,
        "bucket": bucket,
        "prefix": prefix_clean,
        "repo_v1_url": repo_v1_url,
        "repo_v2_url": repo_v2_url,
        "repo_v1_key": repo_v1_key,
        "repo_v2_key": repo_v2_key,
        "local_diff": local_diff,
        "mc_commands": mc_commands,
        "verify_v1_remote": verify_v1,
        "verify_v2_remote": verify_v2,
        "fetch_v1": fetch_v1,
        "fetch_v2": fetch_v2,
        "audit_v2": audit_v2,
        "cold_fetch": cold,
        "warm_update": warm,
        "full_object_baseline": baseline,
        "cold_vs_full_v1_ratio": (cold_bytes / full_v1) if full_v1 else None,
        "warm_vs_full_v2_ratio": (warm_bytes / full_v2) if full_v2 else None,
        "warm_savings_vs_full_v2_ratio": (1.0 - (warm_bytes / full_v2)) if full_v2 else None,
        "cost_model": cost_model,
    }
    (scenario_out / "scenario_result.json").write_text(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    return result


def _alpha40_matrix_markdown(result: dict) -> str:
    rows = []
    for item in result.get("scenarios", []):
        warm = item.get("warm_update") or {}
        base = item.get("full_object_baseline") or {}
        v2 = base.get("v2") or {}
        rows.append(
            f"| {item.get('scenario')} | {item.get('ok')} | {warm.get('downloaded_pack_bytes')} | {v2.get('downloaded_bytes')} | {item.get('warm_vs_full_v2_ratio')} | {item.get('warm_savings_vs_full_v2_ratio')} | {warm.get('cache_hit_ratio')} |"
        )
    rows_txt = "\n".join(rows)
    return f"""# CLD2 alpha40 MinIO cost matrix

## Summary

- ok: `{result.get('ok')}`
- real_run: `{result.get('real_run')}`
- file_size: `{result.get('file_size')}`
- scenarios: `{', '.join(result.get('scenario_names', []))}`
- bucket: `{result.get('bucket')}`
- prefix: `{result.get('prefix')}`

## Matrix

| Scenario | OK | CLD2 warm bytes | Full v2 bytes | Warm/full ratio | Warm savings ratio | Cache hit ratio |
|---|---:|---:|---:|---:|---:|---:|
{rows_txt}

## Interpretation

Alpha40 is a local MinIO/S3-compatible matrix, not AWS S3 production evidence.

The important point is not only the friendly result. High-entropy and heavy-change scenarios are expected to reduce or destroy the advantage. That is useful because CLD2 must report negative/worst-case behavior honestly.
"""


def _alpha40_matrix_html(result: dict) -> str:
    import html
    md = _alpha40_matrix_markdown(result)
    ok_class = "ok" if result.get("ok") else "bad"
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>CLD2 alpha40 MinIO cost matrix</title>
<style>body{{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:2rem;line-height:1.45;background:#fafafa}}pre{{background:#111;color:#eee;padding:1rem;overflow:auto}}code{{background:#f3f3f3;padding:.1rem .25rem;border-radius:4px}}.badge{{padding:.25rem .7rem;border-radius:999px;font-weight:700}}.ok{{background:#e7f7ed;color:#126b35}}.bad{{background:#fdeaea;color:#8b1a1a}}</style></head>
<body><h1>CLD2 alpha40 MinIO cost matrix</h1><p>Status: <span class="badge {ok_class}">{html.escape(str(result.get('ok')))}</span></p>
<h2>Markdown source</h2><pre>{html.escape(md)}</pre></body></html>"""


def run_minio_cost_matrix_pilot(
    out_dir: str | Path,
    *,
    file_size: str = "64MiB",
    scenarios: str | list[str] = "normal,high-entropy,small-files",
    small_file_count: int = 256,
    endpoint: str = "",
    access_key: str = "",
    secret_key: str = "",
    bucket: str = "cld2-alpha40-bucket",
    prefix: str = "releases",
    alias: str = "cld2alpha40",
    mc_path: str = "mc",
    public_base_url: str = "",
    preflight_only: bool = False,
    skip_upload: bool = False,
    codec: str = "zstd",
    chunker: str = "fastcdc",
    fixed_size: str = "1MiB",
    chunk_min: str = "64KiB",
    chunk_avg: str = "256KiB",
    chunk_max: str = "1MiB",
    fastcdc_stride: int = 4,
    http_retries: int = 3,
    http_backoff: float = 0.05,
    parallel: int = 1,
    cost_per_gb: float = 0.04,
    download_counts: str | list[int] = "1,10,1000,10000,100000",
    currency: str = "EUR",
    make_review: bool = False,
    keep_heavy: bool = True,
    max_mb: float = 10.0,
) -> dict:
    out = Path(out_dir)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    names = _alpha40_parse_scenarios(scenarios)
    results = []
    for idx, scenario in enumerate(names, 1):
        scenario_slug = scenario.replace("_", "-").lower()
        scenario_result = _alpha40_run_matrix_scenario(
            out / f"{idx:02d}_{scenario_slug}",
            scenario=scenario_slug,
            file_size=file_size,
            small_file_count=small_file_count,
            endpoint=endpoint,
            access_key=access_key,
            secret_key=secret_key,
            bucket=bucket,
            prefix=prefix,
            alias=alias,
            mc_path=mc_path,
            public_base_url=public_base_url,
            preflight_only=preflight_only,
            skip_upload=skip_upload,
            codec=codec,
            chunker=chunker,
            fixed_size=fixed_size,
            chunk_min=chunk_min,
            chunk_avg=chunk_avg,
            chunk_max=chunk_max,
            fastcdc_stride=fastcdc_stride,
            http_retries=http_retries,
            http_backoff=http_backoff,
            parallel=parallel,
            cost_per_gb=cost_per_gb,
            download_counts=download_counts,
            currency=currency,
        )
        results.append(scenario_result)
    real_run = bool(results and all(r.get("real_run") for r in results))
    ok = bool(results and all(r.get("ok") for r in results))
    result = {
        "schema": "CoreLangDistribution/MinioCostMatrixPilot",
        "version": "2.0-alpha40",
        "ok": ok,
        "real_run": real_run,
        "out_dir": str(out),
        "file_size": file_size,
        "scenario_names": names,
        "small_file_count": small_file_count,
        "bucket": bucket,
        "prefix": prefix,
        "codec": codec,
        "chunker": chunker,
        "cost_per_gb": cost_per_gb,
        "download_counts": _alpha39_parse_counts(download_counts),
        "currency": currency,
        "scenarios": results,
        "notes": [
            "local MinIO/S3-compatible matrix only, not AWS S3 production evidence",
            "high-entropy/heavy-change scenarios are expected to reduce or eliminate savings",
        ],
    }
    (out / "minio_cost_matrix_result.json").write_text(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    (out / "minio_cost_matrix_report.md").write_text(_alpha40_matrix_markdown(result), encoding="utf-8")
    (out / "index.html").write_text(_alpha40_matrix_html(result), encoding="utf-8")
    with (out / "minio_cost_matrix_summary.csv").open("w", newline="", encoding="utf-8") as f:
        fieldnames = ["scenario", "ok", "real_run", "cld2_warm_bytes", "full_v2_bytes", "warm_vs_full_v2_ratio", "warm_savings_vs_full_v2_ratio", "cache_hit_ratio"]
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in results:
            warm = r.get("warm_update") or {}
            baseline = r.get("full_object_baseline") or {}
            v2 = baseline.get("v2") or {}
            w.writerow({
                "scenario": r.get("scenario"),
                "ok": r.get("ok"),
                "real_run": r.get("real_run"),
                "cld2_warm_bytes": warm.get("downloaded_pack_bytes"),
                "full_v2_bytes": v2.get("downloaded_bytes"),
                "warm_vs_full_v2_ratio": r.get("warm_vs_full_v2_ratio"),
                "warm_savings_vs_full_v2_ratio": r.get("warm_savings_vs_full_v2_ratio"),
                "cache_hit_ratio": warm.get("cache_hit_ratio"),
            })
    with (out / "minio_cost_matrix_costs.csv").open("w", newline="", encoding="utf-8") as f:
        fieldnames = ["scenario", "download_count", "full_bytes_total", "cld2_warm_bytes_total", "full_cost", "cld2_warm_cost", "savings_cost", "savings_ratio"]
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in results:
            for row in (r.get("cost_model") or {}).get("rows", []):
                item = {"scenario": r.get("scenario")}
                item.update(row)
                w.writerow(item)
    if not keep_heavy:
        for child in out.iterdir():
            if child.is_dir():
                for heavy_name in ["pairs", "repos_src", "cache", "install_v1", "install_v2", "full_download_v1", "full_download_v2"]:
                    shutil.rmtree(child / heavy_name, ignore_errors=True)
    if make_review:
        result["review_zip"] = make_review_zip(out, str(out) + "_REVIEW.zip", max_mb=max_mb)
        (out / "minio_cost_matrix_result.json").write_text(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
        try:
            _zip_out = result["review_zip"].get("zip_out") if isinstance(result.get("review_zip"), dict) else result.get("review_zip")
            if _zip_out:
                with zipfile.ZipFile(_zip_out, "a", compression=zipfile.ZIP_DEFLATED) as _zf:
                    for _name in [
                        "minio_cost_matrix_result.json",
                        "minio_cost_matrix_summary.csv",
                        "minio_cost_matrix_costs.csv",
                        "minio_cost_matrix_report.md",
                    ]:
                        _p = out / _name
                        if _p.exists() and _name not in _zf.namelist():
                            _zf.write(_p, arcname=_name)
        except Exception:
            pass
    return result



def _alpha41_float(x, default: float | None = None) -> float | None:
    try:
        if x is None or x == "":
            return default
        return float(x)
    except Exception:
        return default


def _alpha41_classify_scenario(item: dict, *, strong_threshold: float, weak_threshold: float) -> dict:
    savings = _alpha41_float(item.get("warm_savings_vs_full_v2_ratio"), None)
    warm_ratio = _alpha41_float(item.get("warm_vs_full_v2_ratio"), None)
    cache_hit = _alpha41_float((item.get("warm_update") or {}).get("cache_hit_ratio"), None)
    scenario = item.get("scenario")
    if not item.get("ok"):
        label = "invalid"
        claim = "do_not_claim"
        explanation = "Scenario did not complete successfully; exclude from performance claims except as a failure."
    elif savings is None:
        label = "unknown"
        claim = "do_not_claim"
        explanation = "Missing savings ratio."
    elif savings >= strong_threshold:
        label = "strong_win"
        claim = "safe_positive_example"
        explanation = "Large savings versus full-object baseline; valid as a positive example for reusable/delta-friendly workloads."
    elif savings >= weak_threshold:
        label = "moderate_or_weak_win"
        claim = "qualified_positive_example"
        explanation = "Some savings, but should be described cautiously and workload-specifically."
    elif savings > 0:
        label = "near_no_win"
        claim = "negative_or_neutral_example"
        explanation = "Almost no savings; useful to show limits and avoid universal claims."
    else:
        label = "regression"
        claim = "negative_example"
        explanation = "CLD2 transferred as much or more than full baseline; must be disclosed as a negative case."
    return {
        "scenario": scenario,
        "ok": item.get("ok"),
        "label": label,
        "claim_bucket": claim,
        "warm_vs_full_v2_ratio": warm_ratio,
        "warm_savings_vs_full_v2_ratio": savings,
        "cache_hit_ratio": cache_hit,
        "explanation": explanation,
    }


def _alpha41_claims_from_classifications(classifications: list[dict]) -> dict:
    labels = {c.get("label") for c in classifications}
    has_strong = "strong_win" in labels
    has_near_no = "near_no_win" in labels or "regression" in labels
    safe = [
        "CLD2 can reduce object-storage update transfer when the new release reuses chunks already present from an older release.",
        "On local MinIO/S3-compatible tests, CLD2 can strongly outperform a full-object baseline for delta-friendly workloads.",
        "CLD2 should be evaluated per workload; benefits are dataset-dependent.",
    ]
    qualified = [
        "The current evidence is local MinIO/S3-compatible, not AWS S3 production.",
        "The matrix intentionally includes negative/adversarial cases.",
        "Savings should be reported per scenario, not as a universal average."
    ]
    avoid = [
        "Do not claim CLD2 is a universal compressor.",
        "Do not claim guaranteed 95% savings.",
        "Do not claim AWS S3 production validation yet.",
        "Do not hide high-entropy or heavy-change results.",
        "Do not compare against rsync/zsync/casync/ostree/DVC/lakeFS/Xet/OCI without running direct comparative tests."
    ]
    if has_strong and has_near_no:
        headline = "CLD2 shows strong gains on reusable workloads and little/no gain on adversarial workloads."
    elif has_strong:
        headline = "CLD2 shows strong gains in the tested matrix, but still requires broader validation."
    else:
        headline = "CLD2 did not show strong gains in this matrix; treat as a limit-finding run."
    return {"headline": headline, "safe_claims": safe, "qualified_claims": qualified, "claims_to_avoid": avoid}


def _alpha41_markdown(result: dict) -> str:
    rows = []
    for c in result.get("scenario_classification", []):
        rows.append(
            f"| {c.get('scenario')} | {c.get('label')} | {c.get('claim_bucket')} | {c.get('warm_vs_full_v2_ratio')} | {c.get('warm_savings_vs_full_v2_ratio')} | {c.get('cache_hit_ratio')} | {c.get('explanation')} |"
        )
    rows_txt = "\n".join(rows)
    claims = result.get("claims") or {}
    safe = "\n".join(f"- {x}" for x in claims.get("safe_claims", []))
    qualified = "\n".join(f"- {x}" for x in claims.get("qualified_claims", []))
    avoid = "\n".join(f"- {x}" for x in claims.get("claims_to_avoid", []))
    matrix = result.get("matrix") or {}
    return f"""# CLD2 alpha41 anti-cherry-picking report

## Headline

{claims.get('headline')}

## Status

- ok: `{result.get('ok')}`
- real_run: `{result.get('real_run')}`
- matrix_ok: `{matrix.get('ok')}`
- matrix_real_run: `{matrix.get('real_run')}`
- file_size: `{matrix.get('file_size')}`
- scenarios: `{', '.join(matrix.get('scenario_names', []))}`

## Scenario classification

| Scenario | Label | Claim bucket | Warm/full ratio | Savings ratio | Cache hit ratio | Explanation |
|---|---|---|---:|---:|---:|---|
{rows_txt}

## Safe claims

{safe}

## Qualified claims

{qualified}

## Claims to avoid

{avoid}

## Honest interpretation

CLD2 is not a magic compressor. It is a distribution/update strategy that is valuable when there is reusable structure, unchanged chunks, or many objects that can be reused across releases. When the content is unrelated, high-entropy, or heavily rewritten, the advantage can disappear. Alpha41 packages both sides into one report so the project can be presented without cherry-picking.
"""


def _alpha41_html(result: dict) -> str:
    import html
    md = _alpha41_markdown(result)
    ok_class = "ok" if result.get("ok") else "bad"
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>CLD2 alpha41 anti-cherry-picking report</title>
<style>body{{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:2rem;line-height:1.45;background:#fafafa}}pre{{background:#111;color:#eee;padding:1rem;overflow:auto}}code{{background:#f3f3f3;padding:.1rem .25rem;border-radius:4px}}.badge{{padding:.25rem .7rem;border-radius:999px;font-weight:700}}.ok{{background:#e7f7ed;color:#126b35}}.bad{{background:#fdeaea;color:#8b1a1a}}</style></head>
<body><h1>CLD2 alpha41 anti-cherry-picking report</h1><p>Status: <span class="badge {ok_class}">{html.escape(str(result.get('ok')))}</span></p>
<p><b>{html.escape((result.get('claims') or {}).get('headline', ''))}</b></p>
<h2>Markdown source</h2><pre>{html.escape(md)}</pre></body></html>"""


def run_anti_cherrypick_report_pilot(
    out_dir: str | Path,
    *,
    file_size: str = "64MiB",
    scenarios: str | list[str] = "normal,high-entropy,small-files,heavy-change",
    small_file_count: int = 256,
    endpoint: str = "",
    access_key: str = "",
    secret_key: str = "",
    bucket: str = "cld2-alpha41-bucket",
    prefix: str = "releases",
    alias: str = "cld2alpha41",
    mc_path: str = "mc",
    public_base_url: str = "",
    preflight_only: bool = False,
    skip_upload: bool = False,
    codec: str = "zstd",
    chunker: str = "fastcdc",
    fixed_size: str = "1MiB",
    chunk_min: str = "64KiB",
    chunk_avg: str = "256KiB",
    chunk_max: str = "1MiB",
    fastcdc_stride: int = 4,
    http_retries: int = 3,
    http_backoff: float = 0.05,
    parallel: int = 1,
    cost_per_gb: float = 0.04,
    download_counts: str | list[int] = "1,10,1000,10000,100000",
    currency: str = "EUR",
    strong_threshold: float = 0.50,
    weak_threshold: float = 0.10,
    make_review: bool = False,
    keep_heavy: bool = True,
    max_mb: float = 10.0,
) -> dict:
    out = Path(out_dir)
    matrix = run_minio_cost_matrix_pilot(
        out,
        file_size=file_size,
        scenarios=scenarios,
        small_file_count=small_file_count,
        endpoint=endpoint,
        access_key=access_key,
        secret_key=secret_key,
        bucket=bucket,
        prefix=prefix,
        alias=alias,
        mc_path=mc_path,
        public_base_url=public_base_url,
        preflight_only=preflight_only,
        skip_upload=skip_upload,
        codec=codec,
        chunker=chunker,
        fixed_size=fixed_size,
        chunk_min=chunk_min,
        chunk_avg=chunk_avg,
        chunk_max=chunk_max,
        fastcdc_stride=fastcdc_stride,
        http_retries=http_retries,
        http_backoff=http_backoff,
        parallel=parallel,
        cost_per_gb=cost_per_gb,
        download_counts=download_counts,
        currency=currency,
        make_review=False,
        keep_heavy=keep_heavy,
        max_mb=max_mb,
    )
    classifications = [
        _alpha41_classify_scenario(s, strong_threshold=strong_threshold, weak_threshold=weak_threshold)
        for s in matrix.get("scenarios", [])
    ]
    claims = _alpha41_claims_from_classifications(classifications)
    result = {
        "schema": "CoreLangDistribution/AntiCherrypickReport",
        "version": "2.0-alpha41",
        "ok": bool(matrix.get("ok") and classifications),
        "real_run": bool(matrix.get("real_run")),
        "out_dir": str(out),
        "strong_threshold": strong_threshold,
        "weak_threshold": weak_threshold,
        "matrix": matrix,
        "scenario_classification": classifications,
        "claims": claims,
    }
    (out / "anti_cherrypick_result.json").write_text(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    (out / "anti_cherrypick_report.md").write_text(_alpha41_markdown(result), encoding="utf-8")
    (out / "index.html").write_text(_alpha41_html(result), encoding="utf-8")
    with (out / "anti_cherrypick_classification.csv").open("w", newline="", encoding="utf-8") as f:
        fieldnames = ["scenario", "ok", "label", "claim_bucket", "warm_vs_full_v2_ratio", "warm_savings_vs_full_v2_ratio", "cache_hit_ratio", "explanation"]
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for c in classifications:
            w.writerow({k: c.get(k) for k in fieldnames})
    with (out / "anti_cherrypick_claims.md").open("w", encoding="utf-8") as f:
        f.write(_alpha41_markdown(result))
    if make_review:
        result["review_zip"] = make_review_zip(out, str(out) + "_REVIEW.zip", max_mb=max_mb)
        (out / "anti_cherrypick_result.json").write_text(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
        try:
            _zip_out = result["review_zip"].get("zip_out") if isinstance(result.get("review_zip"), dict) else result.get("review_zip")
            if _zip_out:
                with zipfile.ZipFile(_zip_out, "a", compression=zipfile.ZIP_DEFLATED) as _zf:
                    for _name in [
                        "anti_cherrypick_result.json",
                        "anti_cherrypick_report.md",
                        "anti_cherrypick_claims.md",
                        "anti_cherrypick_classification.csv",
                        "minio_cost_matrix_result.json",
                        "minio_cost_matrix_summary.csv",
                        "minio_cost_matrix_costs.csv",
                        "minio_cost_matrix_report.md",
                    ]:
                        _p = out / _name
                        if _p.exists() and _name not in _zf.namelist():
                            _zf.write(_p, arcname=_name)
        except Exception:
            pass
    return result



def _alpha42_pct(x) -> str:
    try:
        if x is None or x == "":
            return ""
        return f"{float(x) * 100:.2f}%"
    except Exception:
        return str(x)


def _alpha42_scenario_table(classifications: list[dict]) -> str:
    rows = []
    for c in classifications:
        rows.append(
            f"| {c.get('scenario')} | {c.get('label')} | {c.get('claim_bucket')} | {_alpha42_pct(c.get('warm_vs_full_v2_ratio'))} | {_alpha42_pct(c.get('warm_savings_vs_full_v2_ratio'))} | {_alpha42_pct(c.get('cache_hit_ratio'))} |"
        )
    return "\n".join(rows)


def _alpha42_readme(result: dict, *, project_name: str) -> str:
    anti = result.get("anti_cherrypick") or {}
    matrix = anti.get("matrix") or {}
    claims = anti.get("claims") or {}
    classifications = anti.get("scenario_classification") or []
    safe_claims = "\n".join(f"- {x}" for x in claims.get("safe_claims", []))
    avoid_claims = "\n".join(f"- {x}" for x in claims.get("claims_to_avoid", []))
    scenario_table = _alpha42_scenario_table(classifications)
    return f"""# {project_name}

`{project_name}` is an experimental distribution/update toolchain focused on reducing repeated download bytes when a new release reuses data already present in an older release.

It is **not** a universal compressor. It is most useful when releases share reusable chunks, files, or structured content.

## Current status

This report was generated by:

```text
CLD2 alpha42 — GitHub/Vetrina technical report generator
```

Benchmark backend:

```text
Local MinIO / S3-compatible object storage
```

Important: this is **not** AWS S3 production evidence.

## Headline

{claims.get('headline')}

## Benchmark matrix

| Scenario | Label | Claim bucket | Warm/full ratio | Savings ratio | Cache hit ratio |
|---|---|---|---:|---:|---:|
{scenario_table}

## What CLD2 does well

{safe_claims}

## Claims to avoid

{avoid_claims}

## How to interpret the benchmark

- `normal` and `small-files` represent reusable/delta-friendly workloads.
- `high-entropy` is an adversarial case: unrelated random data should not show meaningful savings.
- `heavy-change` shows what happens when a large part of the release is rewritten.
- A strong result in one workload does not imply strong results everywhere.

## Reproducibility

The review ZIP contains:

```text
anti_cherrypick_result.json
anti_cherrypick_report.md
anti_cherrypick_classification.csv
minio_cost_matrix_result.json
minio_cost_matrix_summary.csv
minio_cost_matrix_costs.csv
```

Use these files to audit the claims instead of relying only on this README.

## Current recommended next steps

1. Add direct comparison harnesses against established tools.
2. Test larger data sizes.
3. Add native S3/SigV4 or presigned URL support.
4. Keep negative/adversarial cases in public reports.
"""


def _alpha42_positioning(result: dict, *, project_name: str) -> str:
    return f"""# {project_name} — positioning note

## One-sentence positioning

CLD2 is a cost-aware release distribution/update prototype for workloads where new releases can reuse chunks or files from previous releases.

## Where it may have a place

- Internal release distribution.
- Game/asset patching experiments.
- Data/model artifact updates.
- CI/CD artifact distribution.
- Edge/cache-friendly update pipelines.
- GitHub/vetrina as an honest devtool/research prototype.

## Where it should not be oversold

- One-shot cold downloads with no prior cache.
- High-entropy data with no reuse.
- Releases where most bytes are rewritten.
- Claims of universal compression.
- Claims of AWS/cloud production readiness before real cloud tests.

## Monetization realism

Short term:
- GitHub/vetrina.
- Sponsors/donations.
- Consulting/support.

Later:
- Enterprise integration.
- Managed packaging/distribution service.
- FinOps reporting around artifact distribution.
"""


def _alpha42_limitations(result: dict) -> str:
    return """# Limitations

CLD2 alpha42 still has important limits:

1. Local MinIO is not AWS S3 production.
2. No native SigV4 client support yet.
3. No real CDN test yet.
4. No direct comparison harness against rsync/zsync/casync/ostree/DVC/lakeFS/Xet/OCI yet.
5. No large-scale multi-client benchmark yet.
6. No production security model.
7. Results are workload-dependent.
8. High-entropy and heavy-change cases show little or no advantage.

These limitations should remain visible in any public README.
"""


def _alpha42_reproduce_command(result: dict, *, project_name: str) -> str:
    matrix = (result.get("anti_cherrypick") or {}).get("matrix") or {}
    return f"""# How to reproduce alpha42 report

## Requirements

- Python 3.10+
- MinIO server running locally
- MinIO `mc.exe`
- CLD2 alpha42 package

## Windows PowerShell example

Run from the extracted alpha42 folder:

```powershell
python .\\cld2.py bench-github-report `
  --out-dir "C:\\\\Users\\\\Pietro\\\\Desktop\\\\Progetti\\\\Corelang\\\\CorelangDistribution 2.0\\\\17\\\\per test\\\\alpha42_github_report_64MiB" `
  --endpoint "http://127.0.0.1:9000" `
  --access-key "cld2admin" `
  --secret-key "cld2admin123456" `
  --bucket "cld2-alpha42-bucket" `
  --prefix "releases" `
  --alias "cld2alpha42" `
  --mc-path "C:\\\\Users\\\\Pietro\\\\Desktop\\\\Progetti\\\\Corelang\\\\CorelangDistribution 2.0\\\\tools\\\\minio\\\\mc.exe" `
  --file-size {matrix.get('file_size', '64MiB')} `
  --scenarios "{','.join(matrix.get('scenario_names', ['normal','high-entropy','small-files','heavy-change']))}" `
  --small-file-count 256 `
  --codec zstd `
  --chunker fastcdc `
  --cost-per-gb 0.04 `
  --download-counts "1,10,1000,10000,100000" `
  --currency EUR `
  --make-review-zip
```
"""


def _alpha42_index_html(result: dict, *, project_name: str) -> str:
    import html
    readme = _alpha42_readme(result, project_name=project_name)
    ok_class = "ok" if result.get("ok") else "bad"
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{html.escape(project_name)} alpha42 report</title>
<style>body{{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:2rem;line-height:1.45;background:#fafafa}}pre{{background:#111;color:#eee;padding:1rem;overflow:auto}}code{{background:#f3f3f3;padding:.1rem .25rem;border-radius:4px}}.badge{{padding:.25rem .7rem;border-radius:999px;font-weight:700}}.ok{{background:#e7f7ed;color:#126b35}}.bad{{background:#fdeaea;color:#8b1a1a}}</style></head>
<body><h1>{html.escape(project_name)} — alpha42 GitHub report</h1><p>Status: <span class="badge {ok_class}">{html.escape(str(result.get('ok')))}</span></p>
<h2>README preview</h2><pre>{html.escape(readme)}</pre></body></html>"""


def run_github_report_pilot(
    out_dir: str | Path,
    *,
    file_size: str = "64MiB",
    scenarios: str | list[str] = "normal,high-entropy,small-files,heavy-change",
    small_file_count: int = 256,
    endpoint: str = "",
    access_key: str = "",
    secret_key: str = "",
    bucket: str = "cld2-alpha42-bucket",
    prefix: str = "releases",
    alias: str = "cld2alpha42",
    mc_path: str = "mc",
    public_base_url: str = "",
    preflight_only: bool = False,
    skip_upload: bool = False,
    codec: str = "zstd",
    chunker: str = "fastcdc",
    fixed_size: str = "1MiB",
    chunk_min: str = "64KiB",
    chunk_avg: str = "256KiB",
    chunk_max: str = "1MiB",
    fastcdc_stride: int = 4,
    http_retries: int = 3,
    http_backoff: float = 0.05,
    parallel: int = 1,
    cost_per_gb: float = 0.04,
    download_counts: str | list[int] = "1,10,1000,10000,100000",
    currency: str = "EUR",
    project_name: str = "CoreLangDistribution 2.0",
    make_review: bool = False,
    keep_heavy: bool = True,
    max_mb: float = 10.0,
) -> dict:
    out = Path(out_dir)
    anti = run_anti_cherrypick_report_pilot(
        out,
        file_size=file_size,
        scenarios=scenarios,
        small_file_count=small_file_count,
        endpoint=endpoint,
        access_key=access_key,
        secret_key=secret_key,
        bucket=bucket,
        prefix=prefix,
        alias=alias,
        mc_path=mc_path,
        public_base_url=public_base_url,
        preflight_only=preflight_only,
        skip_upload=skip_upload,
        codec=codec,
        chunker=chunker,
        fixed_size=fixed_size,
        chunk_min=chunk_min,
        chunk_avg=chunk_avg,
        chunk_max=chunk_max,
        fastcdc_stride=fastcdc_stride,
        http_retries=http_retries,
        http_backoff=http_backoff,
        parallel=parallel,
        cost_per_gb=cost_per_gb,
        download_counts=download_counts,
        currency=currency,
        make_review=False,
        keep_heavy=keep_heavy,
        max_mb=max_mb,
    )
    result = {
        "schema": "CoreLangDistribution/GitHubReport",
        "version": "2.0-alpha42",
        "ok": bool(anti.get("ok")),
        "real_run": bool(anti.get("real_run")),
        "project_name": project_name,
        "out_dir": str(out),
        "anti_cherrypick": anti,
        "generated_files": [
            "README_GITHUB.md",
            "GITHUB_TECHNICAL_REPORT.md",
            "POSITIONING.md",
            "LIMITATIONS.md",
            "HOW_TO_REPRODUCE.md",
            "HONEST_CLAIMS.md",
        ],
    }
    readme = _alpha42_readme(result, project_name=project_name)
    report = _alpha41_markdown(anti)
    positioning = _alpha42_positioning(result, project_name=project_name)
    limitations = _alpha42_limitations(result)
    reproduce = _alpha42_reproduce_command(result, project_name=project_name)
    claims = (anti.get("claims") or {})
    honest_claims = "# Honest claims\n\n"
    honest_claims += "## Safe claims\n\n" + "\n".join(f"- {x}" for x in claims.get("safe_claims", [])) + "\n\n"
    honest_claims += "## Qualified claims\n\n" + "\n".join(f"- {x}" for x in claims.get("qualified_claims", [])) + "\n\n"
    honest_claims += "## Claims to avoid\n\n" + "\n".join(f"- {x}" for x in claims.get("claims_to_avoid", [])) + "\n"
    (out / "github_report_result.json").write_text(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    (out / "README_GITHUB.md").write_text(readme, encoding="utf-8")
    (out / "GITHUB_TECHNICAL_REPORT.md").write_text(report, encoding="utf-8")
    (out / "POSITIONING.md").write_text(positioning, encoding="utf-8")
    (out / "LIMITATIONS.md").write_text(limitations, encoding="utf-8")
    (out / "HOW_TO_REPRODUCE.md").write_text(reproduce, encoding="utf-8")
    (out / "HONEST_CLAIMS.md").write_text(honest_claims, encoding="utf-8")
    (out / "index.html").write_text(_alpha42_index_html(result, project_name=project_name), encoding="utf-8")
    if make_review:
        result["review_zip"] = make_review_zip(out, str(out) + "_REVIEW.zip", max_mb=max_mb)
        (out / "github_report_result.json").write_text(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
        try:
            _zip_out = result["review_zip"].get("zip_out") if isinstance(result.get("review_zip"), dict) else result.get("review_zip")
            if _zip_out:
                with zipfile.ZipFile(_zip_out, "a", compression=zipfile.ZIP_DEFLATED) as _zf:
                    for _name in [
                        "github_report_result.json",
                        "README_GITHUB.md",
                        "GITHUB_TECHNICAL_REPORT.md",
                        "POSITIONING.md",
                        "LIMITATIONS.md",
                        "HOW_TO_REPRODUCE.md",
                        "HONEST_CLAIMS.md",
                        "anti_cherrypick_result.json",
                        "anti_cherrypick_report.md",
                        "anti_cherrypick_claims.md",
                        "anti_cherrypick_classification.csv",
                        "minio_cost_matrix_result.json",
                        "minio_cost_matrix_summary.csv",
                        "minio_cost_matrix_costs.csv",
                        "minio_cost_matrix_report.md",
                    ]:
                        _p = out / _name
                        if _p.exists() and _name not in _zf.namelist():
                            _zf.write(_p, arcname=_name)
        except Exception:
            pass
    return result



def _alpha43_tool_specs() -> list[dict]:
    return [
        {
            "tool": "rsync",
            "category": "file delta/sync",
            "executables": ["rsync"],
            "version_args": ["--version"],
            "comparison_role": "directory-to-directory delta transfer baseline",
            "fairness_note": "Best compared on directory updates, not object-store public GET alone.",
            "status": "probe_only_in_alpha43",
        },
        {
            "tool": "zsync",
            "category": "HTTP delta update",
            "executables": ["zsync", "zsyncmake"],
            "version_args": ["--version"],
            "comparison_role": "HTTP client-side delta download baseline",
            "fairness_note": "Requires .zsync metadata generation and HTTP hosting.",
            "status": "probe_only_in_alpha43",
        },
        {
            "tool": "casync",
            "category": "content-addressed chunk store",
            "executables": ["casync"],
            "version_args": ["--version"],
            "comparison_role": "CAS/chunked update baseline",
            "fairness_note": "Conceptually close; needs native install or Linux/WSL.",
            "status": "probe_only_in_alpha43",
        },
        {
            "tool": "desync",
            "category": "content-addressed chunk store",
            "executables": ["desync"],
            "version_args": ["--version"],
            "comparison_role": "casync-compatible/desync CAS baseline",
            "fairness_note": "Needs desync binary and comparable chunk/store setup.",
            "status": "probe_only_in_alpha43",
        },
        {
            "tool": "ostree",
            "category": "content-addressed system/versioned tree",
            "executables": ["ostree"],
            "version_args": ["--version"],
            "comparison_role": "versioned filesystem/object update baseline",
            "fairness_note": "Mostly Linux-native; fair test likely via WSL/Linux.",
            "status": "probe_only_in_alpha43",
        },
        {
            "tool": "DVC",
            "category": "data/version artifact management",
            "executables": ["dvc"],
            "version_args": ["--version"],
            "comparison_role": "data artifact versioning/dedup baseline",
            "fairness_note": "Different product shape: workflow/versioning, not only byte-transfer.",
            "status": "probe_only_in_alpha43",
        },
        {
            "tool": "lakeFS",
            "category": "data lake versioning",
            "executables": ["lakectl", "lakefs"],
            "version_args": ["--version"],
            "comparison_role": "data lake object/version baseline",
            "fairness_note": "Server-backed workflow; not a direct patcher.",
            "status": "probe_only_in_alpha43",
        },
        {
            "tool": "Xet",
            "category": "content-defined chunking/data versioning",
            "executables": ["xet", "git-xet"],
            "version_args": ["--version"],
            "comparison_role": "chunked large-file/data repo baseline",
            "fairness_note": "Different workflow; fair comparison needs repo setup.",
            "status": "probe_only_in_alpha43",
        },
        {
            "tool": "Docker/OCI layering",
            "category": "container/image layering",
            "executables": ["docker", "podman", "oras"],
            "version_args": ["--version"],
            "comparison_role": "layered artifact distribution baseline",
            "fairness_note": "Best for container/layered artifacts, not arbitrary release folders.",
            "status": "probe_only_in_alpha43",
        },
    ]


def _alpha43_run_version(exe: str, args: list[str]) -> dict:
    path = shutil.which(exe)
    if not path:
        return {"executable": exe, "found": False, "path": "", "version_ok": False, "version": "", "returncode": None, "stderr": ""}
    try:
        p = subprocess.run([path] + list(args), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
        out = (p.stdout or p.stderr or "").strip().splitlines()
        version = out[0] if out else ""
        return {"executable": exe, "found": True, "path": path, "version_ok": p.returncode == 0, "version": version[:300], "returncode": p.returncode, "stderr": (p.stderr or "")[-300:]}
    except Exception as e:
        return {"executable": exe, "found": True, "path": path, "version_ok": False, "version": "", "returncode": -1, "stderr": str(e)}


def _alpha43_probe_baseline_tools() -> list[dict]:
    out = []
    for spec in _alpha43_tool_specs():
        probes = [_alpha43_run_version(exe, spec.get("version_args", ["--version"])) for exe in spec.get("executables", [])]
        installed = any(p.get("found") for p in probes)
        primary = next((p for p in probes if p.get("found")), probes[0] if probes else {})
        item = dict(spec)
        item.update({
            "installed": bool(installed),
            "primary_executable": primary.get("executable", ""),
            "primary_path": primary.get("path", ""),
            "primary_version": primary.get("version", ""),
            "probes": probes,
            "benchmark_status": "not_run_in_alpha43",
            "reason": "alpha43 is preflight/research pack only; no direct baseline performance claims are made",
        })
        out.append(item)
    return out


def _alpha43_tool_matrix_md(tools: list[dict]) -> str:
    rows = []
    for t in tools:
        rows.append(
            f"| {t.get('tool')} | {t.get('installed')} | `{t.get('primary_executable')}` | {t.get('primary_version')} | {t.get('comparison_role')} | {t.get('fairness_note')} |"
        )
    return "\n".join(rows)


def _alpha43_comparison_plan_md(result: dict) -> str:
    tools = result.get("baseline_tools") or []
    table = _alpha43_tool_matrix_md(tools)
    github = result.get("cld2_github_report") or {}
    return f"""# CLD2 alpha43 baseline comparison harness / research pack

## Status

- ok: `{result.get('ok')}`
- probe_only: `{result.get('probe_only')}`
- cld2_report_ok: `{github.get('ok')}`
- cld2_report_real_run: `{github.get('real_run')}`

## What alpha43 is

Alpha43 is a comparison **preflight and research pack**.

It does not claim CLD2 beats rsync, zsync, casync/desync, ostree, DVC/lakeFS/Xet, or Docker/OCI. It checks which tools are available and lays out fair comparison paths.

## Tool availability

| Tool | Installed | Executable | Version | Comparison role | Fairness note |
|---|---:|---|---|---|---|
{table}

## Fair comparison principles

1. Do not compare CLD2 warm-update against another tool's cold-download.
2. Keep dataset classes separated: normal, high-entropy, small-files, heavy-change.
3. Report negative cases.
4. Separate byte transfer, CPU time, setup complexity, and workflow fit.
5. Do not compare against tools that are conceptually different without explaining the difference.

## Recommended next direct benchmark order

1. rsync directory delta baseline.
2. zsync HTTP delta baseline.
3. casync/desync CAS baseline.
4. ostree if Linux/WSL is available.
5. DVC/Xet/lakeFS as workflow-level comparisons.
6. Docker/OCI layering for container/layered artifact workloads.
"""


def _alpha43_howto_md(result: dict) -> str:
    return """# How to run future direct baseline comparisons

## rsync

Goal: compare directory delta transfer.

Fair setup:
- release_v1 and release_v2 directories;
- receiver already has v1;
- rsync updates receiver to v2;
- collect sent bytes/stats.

Example shape:

```bash
rsync -a --stats --delete release_v2/ receiver/
```

## zsync

Goal: HTTP delta download with .zsync metadata.

Fair setup:
- generate .zsync for v2;
- client has old file(s) where applicable;
- download v2 via zsync;
- collect downloaded bytes.

## casync/desync

Goal: CAS/chunk-store comparison.

Fair setup:
- create store from v1/v2;
- client has v1 or store cache;
- fetch v2;
- measure transferred chunks/bytes.

## ostree

Goal: versioned tree update comparison.

Fair setup:
- commit v1 and v2;
- client pulls v2 from v1;
- collect object transfer.

## DVC / lakeFS / Xet

Goal: workflow-level data artifact comparison.

Fair setup:
- model them as data versioning systems, not just patchers;
- report UX/setup/storage semantics separately from byte transfer.

## Docker / OCI layering

Goal: layered artifact update comparison.

Fair setup:
- package releases as layers/images;
- client has v1 layers;
- pull v2;
- collect pulled bytes.
"""


def _alpha43_limitations_md(result: dict) -> str:
    return """# Alpha43 limitations

Alpha43 does not run direct baseline performance benchmarks yet.

It only:
- probes tool availability;
- generates comparison plan;
- keeps CLD2 alpha42 report attached;
- defines fairness rules.

No result in alpha43 should be worded as:
"CLD2 beats rsync/zsync/casync/ostree/DVC/lakeFS/Xet/Docker."

Correct wording:
"Alpha43 prepares a fair comparison harness and records which baseline tools are available. Direct baseline measurements are still pending."
"""


def _alpha43_index_html(result: dict) -> str:
    import html
    md = _alpha43_comparison_plan_md(result)
    ok_class = "ok" if result.get("ok") else "bad"
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>CLD2 alpha43 comparison harness</title>
<style>body{{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:2rem;line-height:1.45;background:#fafafa}}pre{{background:#111;color:#eee;padding:1rem;overflow:auto}}code{{background:#f3f3f3;padding:.1rem .25rem;border-radius:4px}}.badge{{padding:.25rem .7rem;border-radius:999px;font-weight:700}}.ok{{background:#e7f7ed;color:#126b35}}.bad{{background:#fdeaea;color:#8b1a1a}}</style></head>
<body><h1>CLD2 alpha43 comparison harness</h1><p>Status: <span class="badge {ok_class}">{html.escape(str(result.get('ok')))}</span></p>
<h2>Report</h2><pre>{html.escape(md)}</pre></body></html>"""


def run_comparison_harness_pilot(
    out_dir: str | Path,
    *,
    file_size: str = "64MiB",
    scenarios: str | list[str] = "normal,high-entropy,small-files,heavy-change",
    small_file_count: int = 256,
    endpoint: str = "",
    access_key: str = "",
    secret_key: str = "",
    bucket: str = "cld2-alpha43-bucket",
    prefix: str = "releases",
    alias: str = "cld2alpha43",
    mc_path: str = "mc",
    public_base_url: str = "",
    preflight_only: bool = False,
    skip_upload: bool = False,
    codec: str = "zstd",
    chunker: str = "fastcdc",
    fixed_size: str = "1MiB",
    chunk_min: str = "64KiB",
    chunk_avg: str = "256KiB",
    chunk_max: str = "1MiB",
    fastcdc_stride: int = 4,
    http_retries: int = 3,
    http_backoff: float = 0.05,
    parallel: int = 1,
    cost_per_gb: float = 0.04,
    download_counts: str | list[int] = "1,10,1000,10000,100000",
    currency: str = "EUR",
    project_name: str = "CoreLangDistribution 2.0",
    probe_only: bool = False,
    make_review: bool = False,
    keep_heavy: bool = True,
    max_mb: float = 10.0,
) -> dict:
    out = Path(out_dir)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    tools = _alpha43_probe_baseline_tools()
    github = {}
    if not probe_only:
        github = run_github_report_pilot(
            out,
            file_size=file_size,
            scenarios=scenarios,
            small_file_count=small_file_count,
            endpoint=endpoint,
            access_key=access_key,
            secret_key=secret_key,
            bucket=bucket,
            prefix=prefix,
            alias=alias,
            mc_path=mc_path,
            public_base_url=public_base_url,
            preflight_only=preflight_only,
            skip_upload=skip_upload,
            codec=codec,
            chunker=chunker,
            fixed_size=fixed_size,
            chunk_min=chunk_min,
            chunk_avg=chunk_avg,
            chunk_max=chunk_max,
            fastcdc_stride=fastcdc_stride,
            http_retries=http_retries,
            http_backoff=http_backoff,
            parallel=parallel,
            cost_per_gb=cost_per_gb,
            download_counts=download_counts,
            currency=currency,
            project_name=project_name,
            make_review=False,
            keep_heavy=keep_heavy,
            max_mb=max_mb,
        )
    result = {
        "schema": "CoreLangDistribution/ComparisonHarness",
        "version": "2.0-alpha43",
        "ok": bool(tools and (probe_only or github.get("ok") is not False)),
        "probe_only": bool(probe_only),
        "out_dir": str(out),
        "baseline_tools": tools,
        "cld2_github_report": github,
        "direct_baseline_benchmarks_run": False,
        "direct_baseline_benchmark_status": "pending",
        "notes": [
            "alpha43 does not make performance claims against baseline tools",
            "direct comparisons require tool-specific setup and fairness rules",
        ],
    }
    (out / "comparison_harness_result.json").write_text(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    (out / "BASELINE_TOOLS_MATRIX.md").write_text(_alpha43_comparison_plan_md(result), encoding="utf-8")
    (out / "DIRECT_COMPARISON_PLAN.md").write_text(_alpha43_comparison_plan_md(result), encoding="utf-8")
    (out / "HOW_TO_RUN_BASELINES.md").write_text(_alpha43_howto_md(result), encoding="utf-8")
    (out / "COMPARISON_LIMITATIONS.md").write_text(_alpha43_limitations_md(result), encoding="utf-8")
    (out / "index.html").write_text(_alpha43_index_html(result), encoding="utf-8")
    with (out / "baseline_tool_preflight.csv").open("w", newline="", encoding="utf-8") as f:
        fieldnames = ["tool", "installed", "primary_executable", "primary_path", "primary_version", "category", "comparison_role", "fairness_note", "benchmark_status"]
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for t in tools:
            w.writerow({k: t.get(k) for k in fieldnames})
    if make_review:
        result["review_zip"] = make_review_zip(out, str(out) + "_REVIEW.zip", max_mb=max_mb)
        (out / "comparison_harness_result.json").write_text(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
        try:
            _zip_out = result["review_zip"].get("zip_out") if isinstance(result.get("review_zip"), dict) else result.get("review_zip")
            if _zip_out:
                with zipfile.ZipFile(_zip_out, "a", compression=zipfile.ZIP_DEFLATED) as _zf:
                    for _name in [
                        "comparison_harness_result.json",
                        "baseline_tool_preflight.csv",
                        "BASELINE_TOOLS_MATRIX.md",
                        "DIRECT_COMPARISON_PLAN.md",
                        "HOW_TO_RUN_BASELINES.md",
                        "COMPARISON_LIMITATIONS.md",
                        "github_report_result.json",
                        "README_GITHUB.md",
                        "GITHUB_TECHNICAL_REPORT.md",
                        "POSITIONING.md",
                        "LIMITATIONS.md",
                        "HOW_TO_REPRODUCE.md",
                        "HONEST_CLAIMS.md",
                    ]:
                        _p = out / _name
                        if _p.exists() and _name not in _zf.namelist():
                            _zf.write(_p, arcname=_name)
        except Exception:
            pass
    return result



def _alpha44_rsync_probe(rsync_path: str = "rsync") -> dict:
    path = shutil.which(rsync_path) or (rsync_path if Path(rsync_path).exists() else "")
    if not path:
        return {
            "rsync_path_arg": rsync_path,
            "installed": False,
            "path": "",
            "version_ok": False,
            "version": "",
            "error": "rsync not found",
        }
    try:
        p = subprocess.run([path, "--version"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
        lines = (p.stdout or p.stderr or "").strip().splitlines()
        return {
            "rsync_path_arg": rsync_path,
            "installed": True,
            "path": path,
            "version_ok": p.returncode == 0,
            "version": lines[0] if lines else "",
            "returncode": p.returncode,
            "error": (p.stderr or "")[-500:],
        }
    except Exception as e:
        return {
            "rsync_path_arg": rsync_path,
            "installed": True,
            "path": path,
            "version_ok": False,
            "version": "",
            "returncode": -1,
            "error": str(e),
        }


def _alpha44_parse_rsync_stats(text: str) -> dict:
    stats = {}
    aliases = {
        "Number of files": "number_of_files",
        "Number of created files": "number_of_created_files",
        "Number of deleted files": "number_of_deleted_files",
        "Number of regular files transferred": "regular_files_transferred",
        "Total file size": "total_file_size",
        "Total transferred file size": "total_transferred_file_size",
        "Literal data": "literal_data",
        "Matched data": "matched_data",
        "File list size": "file_list_size",
        "File list generation time": "file_list_generation_time",
        "File list transfer time": "file_list_transfer_time",
        "Total bytes sent": "total_bytes_sent",
        "Total bytes received": "total_bytes_received",
        "sent": "sent_received_line",
    }
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("sent ") and " received " in line:
            # e.g. sent 1,234 bytes  received 567 bytes ...
            import re
            m = re.search(r"sent\\s+([0-9,]+)\\s+bytes\\s+received\\s+([0-9,]+)\\s+bytes", line)
            if m:
                stats["sent_summary_bytes"] = int(m.group(1).replace(",", ""))
                stats["received_summary_bytes"] = int(m.group(2).replace(",", ""))
            continue
        if ":" in line:
            key, val = line.split(":", 1)
            key = key.strip()
            norm = aliases.get(key)
            if norm:
                # Keep numeric bytes where possible.
                v = val.strip()
                first = v.split()[0].replace(",", "") if v.split() else ""
                try:
                    stats[norm] = int(first)
                except Exception:
                    try:
                        stats[norm] = float(first)
                    except Exception:
                        stats[norm] = v
    sent = int(stats.get("total_bytes_sent") or stats.get("sent_summary_bytes") or 0)
    received = int(stats.get("total_bytes_received") or stats.get("received_summary_bytes") or 0)
    stats["network_bytes_total"] = sent + received
    return stats


def _alpha44_run_rsync_update(rsync_path: str, src_v2: Path, receiver: Path, out_dir: Path) -> dict:
    path = shutil.which(rsync_path) or rsync_path
    out_dir.mkdir(parents=True, exist_ok=True)
    # Ensure trailing slash semantics: copy contents of v2 into receiver.
    src = str(Path(src_v2).resolve()) + os.sep
    dst = str(Path(receiver).resolve()) + os.sep
    cmd = [path, "-r", "--delete", "--stats", "--checksum", "--no-whole-file", "--inplace", "--no-perms", "--no-owner", "--no-group", "--omit-dir-times", src, dst]
    from time import perf_counter
    t0 = perf_counter()
    try:
        p = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600)
        dt = perf_counter() - t0
        combined = (p.stdout or "") + "\n" + (p.stderr or "")
        stats = _alpha44_parse_rsync_stats(combined)
        (out_dir / "rsync_stdout.txt").write_text(p.stdout or "", encoding="utf-8")
        (out_dir / "rsync_stderr.txt").write_text(p.stderr or "", encoding="utf-8")
        return {
            "ok": p.returncode == 0,
            "cmd": cmd,
            "returncode": p.returncode,
            "seconds": dt,
            "stdout_tail": (p.stdout or "")[-2000:],
            "stderr_tail": (p.stderr or "")[-2000:],
            "stats": stats,
        }
    except Exception as e:
        return {"ok": False, "cmd": cmd, "returncode": -1, "seconds": 0, "stdout_tail": "", "stderr_tail": str(e), "stats": {}}


def _alpha44_run_scenario(
    scenario_out: Path,
    *,
    scenario: str,
    file_size: str,
    small_file_count: int,
    rsync_path: str,
) -> dict:
    if scenario_out.exists():
        shutil.rmtree(scenario_out)
    scenario_out.mkdir(parents=True)
    pair_root = scenario_out / "pairs"
    v1, v2 = _alpha40_write_pair(pair_root, scenario=scenario, file_size=file_size, small_file_count=small_file_count)
    receiver = scenario_out / "receiver"
    shutil.copytree(v1, receiver)
    rsync_result = _alpha44_run_rsync_update(rsync_path, v2, receiver, scenario_out / "rsync_run")
    # Verify receiver now equals v2 by making temporary repos and comparing file hashes through audit-like direct hashing.
    compare = {"ok": False, "missing": [], "extra": [], "mismatch": []}
    try:
        v2_files = {p.relative_to(v2).as_posix(): sha256_file(p) for p in v2.rglob("*") if p.is_file()}
        rx_files = {p.relative_to(receiver).as_posix(): sha256_file(p) for p in receiver.rglob("*") if p.is_file()}
        compare["missing"] = sorted(set(v2_files) - set(rx_files))
        compare["extra"] = sorted(set(rx_files) - set(v2_files))
        compare["mismatch"] = sorted(k for k in set(v2_files) & set(rx_files) if v2_files[k] != rx_files[k])
        compare["ok"] = not compare["missing"] and not compare["extra"] and not compare["mismatch"]
    except Exception as e:
        compare["error"] = str(e)
    stats = rsync_result.get("stats") or {}
    result = {
        "scenario": scenario,
        "ok": bool(rsync_result.get("ok") and compare.get("ok")),
        "rsync": rsync_result,
        "receiver_matches_v2": compare,
        "rsync_network_bytes_total": stats.get("network_bytes_total"),
        "rsync_total_bytes_sent": stats.get("total_bytes_sent") or stats.get("sent_summary_bytes"),
        "rsync_total_bytes_received": stats.get("total_bytes_received") or stats.get("received_summary_bytes"),
        "rsync_literal_data": stats.get("literal_data"),
        "rsync_matched_data": stats.get("matched_data"),
        "rsync_total_transferred_file_size": stats.get("total_transferred_file_size"),
    }
    (scenario_out / "rsync_scenario_result.json").write_text(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    return result


def _alpha44_compare_with_cld2(rsync_results: list[dict], cld2_matrix: dict) -> list[dict]:
    by_name = {s.get("scenario"): s for s in (cld2_matrix.get("scenarios") or [])}
    rows = []
    for r in rsync_results:
        name = r.get("scenario")
        cld = by_name.get(name) or {}
        warm = cld.get("warm_update") or {}
        baseline = cld.get("full_object_baseline") or {}
        v2 = baseline.get("v2") or {}
        rsync_bytes = r.get("rsync_network_bytes_total")
        cld2_bytes = warm.get("downloaded_pack_bytes")
        full_v2 = v2.get("downloaded_bytes")
        ratio = None
        if rsync_bytes and cld2_bytes is not None:
            try:
                ratio = float(cld2_bytes) / float(rsync_bytes)
            except Exception:
                ratio = None
        rows.append({
            "scenario": name,
            "rsync_ok": r.get("ok"),
            "cld2_ok": cld.get("ok"),
            "rsync_network_bytes_total": rsync_bytes,
            "cld2_warm_bytes": cld2_bytes,
            "full_v2_bytes": full_v2,
            "cld2_vs_rsync_ratio": ratio,
            "note": "local rsync directory update vs CLD2 local MinIO warm update; not identical network model",
        })
    return rows


def _alpha44_md(result: dict) -> str:
    probe = result.get("rsync_probe") or {}
    rows = []
    for r in result.get("comparison_rows", []):
        rows.append(
            f"| {r.get('scenario')} | {r.get('rsync_ok')} | {r.get('cld2_ok')} | {r.get('rsync_network_bytes_total')} | {r.get('cld2_warm_bytes')} | {r.get('full_v2_bytes')} | {r.get('cld2_vs_rsync_ratio')} |"
        )
    if not rows:
        rows.append("| _no direct rsync benchmark_ |  |  |  |  |  |  |")
    return f"""# CLD2 alpha44 rsync direct baseline

## Status

- ok: `{result.get('ok')}`
- probe_only: `{result.get('probe_only')}`
- rsync_installed: `{probe.get('installed')}`
- rsync_path: `{probe.get('path')}`
- rsync_version: `{probe.get('version')}`
- cld2_matrix_ok: `{(result.get('cld2_matrix') or {}).get('ok')}`
- cld2_attach_attempt_count: `{len(result.get('cld2_attach_attempts') or [])}`

## Boundary

This is a first direct rsync baseline. In alpha44.3, rsync runs in WSL-safe checksum mode (`-r --delete --stats --checksum --no-whole-file --inplace --no-perms --no-owner --no-group --omit-dir-times`). `--checksum` prevents same-size/same-time false negatives in generated test data; `--no-whole-file` avoids local-copy whole-file shortcut so rsync's delta behavior is represented more fairly. It compares local rsync directory update stats with CLD2 local MinIO warm-update bytes.

This is useful but not perfect: rsync and CLD2 are different distribution models. The result must be described as a local baseline, not a universal win/loss.

## Results

| Scenario | rsync OK | CLD2 OK | rsync network bytes | CLD2 warm bytes | Full v2 bytes | CLD2/rsync ratio |
|---|---:|---:|---:|---:|---:|---:|
{chr(10).join(rows)}

## If rsync is missing

Install rsync through WSL/Linux, MSYS2, cwRsync, or another Windows-compatible package, then rerun this command.

Do not claim any rsync comparison if `rsync_installed=False`.
"""


def _alpha44_html(result: dict) -> str:
    import html
    md = _alpha44_md(result)
    ok_class = "ok" if result.get("ok") else "bad"
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>CLD2 alpha44 rsync baseline</title>
<style>body{{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:2rem;line-height:1.45;background:#fafafa}}pre{{background:#111;color:#eee;padding:1rem;overflow:auto}}code{{background:#f3f3f3;padding:.1rem .25rem;border-radius:4px}}.badge{{padding:.25rem .7rem;border-radius:999px;font-weight:700}}.ok{{background:#e7f7ed;color:#126b35}}.bad{{background:#fdeaea;color:#8b1a1a}}</style></head>
<body><h1>CLD2 alpha44 rsync direct baseline</h1><p>Status: <span class="badge {ok_class}">{html.escape(str(result.get('ok')))}</span></p>
<pre>{html.escape(md)}</pre></body></html>"""


def run_rsync_baseline_pilot(
    out_dir: str | Path,
    *,
    file_size: str = "64MiB",
    scenarios: str | list[str] = "normal,high-entropy,small-files,heavy-change",
    small_file_count: int = 256,
    rsync_path: str = "rsync",
    probe_only: bool = False,
    endpoint: str = "",
    access_key: str = "",
    secret_key: str = "",
    bucket: str = "cld2-alpha44-bucket",
    prefix: str = "releases",
    alias: str = "cld2alpha44",
    mc_path: str = "mc",
    public_base_url: str = "",
    preflight_only: bool = False,
    skip_upload: bool = False,
    codec: str = "zstd",
    chunker: str = "fastcdc",
    fixed_size: str = "1MiB",
    chunk_min: str = "64KiB",
    chunk_avg: str = "256KiB",
    chunk_max: str = "1MiB",
    fastcdc_stride: int = 4,
    http_retries: int = 3,
    http_backoff: float = 0.05,
    parallel: int = 1,
    cost_per_gb: float = 0.04,
    download_counts: str | list[int] = "1,10,1000,10000,100000",
    currency: str = "EUR",
    make_review: bool = False,
    keep_heavy: bool = True,
    max_mb: float = 10.0,
) -> dict:
    out = Path(out_dir)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    probe = _alpha44_rsync_probe(rsync_path)
    scenario_names = _alpha40_parse_scenarios(scenarios)
    rsync_results = []
    cld2_matrix = {}

    if not probe_only and probe.get("usable"):
        for scenario in scenario_names:
            rsync_results.append(_alpha44_run_scenario(out / f"rsync_{scenario}", scenario=scenario, file_size=file_size, small_file_count=small_file_count, rsync_path=rsync_path))
        cld2_matrix = run_minio_cost_matrix_pilot(
            out / "cld2_matrix",
            file_size=file_size,
            scenarios=scenarios,
            small_file_count=small_file_count,
            endpoint=endpoint,
            access_key=access_key,
            secret_key=secret_key,
            bucket=bucket,
            prefix=prefix,
            alias=alias,
            mc_path=mc_path,
            public_base_url=public_base_url,
            preflight_only=preflight_only,
            skip_upload=skip_upload,
            codec=codec,
            chunker=chunker,
            fixed_size=fixed_size,
            chunk_min=chunk_min,
            chunk_avg=chunk_avg,
            chunk_max=chunk_max,
            fastcdc_stride=fastcdc_stride,
            http_retries=http_retries,
            http_backoff=http_backoff,
            parallel=parallel,
            cost_per_gb=cost_per_gb,
            download_counts=download_counts,
            currency=currency,
            make_review=False,
            keep_heavy=keep_heavy,
            max_mb=max_mb,
        )
    comparison_rows = _alpha44_compare_with_cld2(rsync_results, cld2_matrix) if rsync_results and cld2_matrix else []
    result = {
        "schema": "CoreLangDistribution/RsyncBaseline",
        "version": "2.0-alpha44",
        "ok": bool((probe_only and probe) or (probe.get("installed") and rsync_results and all(r.get("ok") for r in rsync_results) and cld2_matrix.get("ok"))),
        "probe_only": bool(probe_only),
        "rsync_probe": probe,
        "out_dir": str(out),
        "file_size": file_size,
        "zsync_bind_host": zsync_bind_host,
        "zsync_url_host": zsync_url_host,
        "zsync_server_mode": zsync_server_mode,
        "zsync_effective_url_host": _alpha45_choose_url_host(zsync_path, zsync_url_host),
        "zsync_effective_server_mode": ("wsl-native" if (zsync_server_mode == "auto" and _alpha45_is_wsl_wrapper(zsync_path)) else zsync_server_mode),
        "scenario_names": scenario_names,
        "rsync_results": rsync_results,
        "cld2_matrix": cld2_matrix,
        "cld2_attach_attempts": cld2_attach_attempts,
        "comparison_rows": comparison_rows,
        "direct_baseline_benchmarks_run": bool(rsync_results),
        "notes": [
            "rsync local directory update and CLD2 MinIO warm update are different distribution models",
            "only claim rsync comparison when rsync is installed and direct_baseline_benchmarks_run=True",
        ],
    }
    (out / "rsync_baseline_result.json").write_text(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    (out / "RSYNC_BASELINE_REPORT.md").write_text(_alpha44_md(result), encoding="utf-8")
    (out / "index.html").write_text(_alpha44_html(result), encoding="utf-8")
    with (out / "rsync_baseline_summary.csv").open("w", newline="", encoding="utf-8") as f:
        fieldnames = ["scenario", "rsync_ok", "cld2_ok", "rsync_network_bytes_total", "cld2_warm_bytes", "full_v2_bytes", "cld2_vs_rsync_ratio", "note"]
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for row in comparison_rows:
            w.writerow({k: row.get(k) for k in fieldnames})
    with (out / "rsync_preflight.json").open("w", encoding="utf-8") as f:
        json.dump(probe, f, indent=2, sort_keys=True)
    if not keep_heavy:
        for p in out.iterdir():
            if p.is_dir() and (p.name.startswith("rsync_") or p.name == "cld2_matrix"):
                shutil.rmtree(p, ignore_errors=True)
    if make_review:
        result["review_zip"] = make_review_zip(out, str(out) + "_REVIEW.zip", max_mb=max_mb)
        (out / "rsync_baseline_result.json").write_text(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
        try:
            _zip_out = result["review_zip"].get("zip_out") if isinstance(result.get("review_zip"), dict) else result.get("review_zip")
            if _zip_out:
                with zipfile.ZipFile(_zip_out, "a", compression=zipfile.ZIP_DEFLATED) as _zf:
                    for _name in [
                        "rsync_baseline_result.json",
                        "rsync_baseline_summary.csv",
                        "rsync_preflight.json",
                        "RSYNC_BASELINE_REPORT.md",
                    ]:
                        _p = out / _name
                        if _p.exists() and _name not in _zf.namelist():
                            _zf.write(_p, arcname=_name)
        except Exception:
            pass
    return result



def _alpha45_tool_probe(exe: str) -> dict:
    """Non-blocking probe for zsync/zsyncmake.

    Alpha45.2 deliberately does NOT execute the tool during probe. Some Windows
    wrapper + WSL combinations can hang or wait after unsupported --version/help
    calls. For preflight, resolving the executable/wrapper path is enough.

    Direct benchmark mode will still execute zsync/zsyncmake with real arguments.
    """
    path = shutil.which(exe) or (exe if Path(exe).exists() else "")
    if not path:
        return {
            "path_arg": exe,
            "installed": False,
            "path": "",
            "version_ok": False,
            "usable": False,
            "version": "",
            "error": f"{exe} not found",
            "probe_mode": "nonblocking_path_only",
        }
    return {
        "path_arg": exe,
        "installed": True,
        "path": path,
        "version_ok": False,
        "usable": True,
        "version": "not executed during probe; alpha45.2 uses nonblocking path-only probe",
        "returncode": None,
        "error": "",
        "probe_mode": "nonblocking_path_only",
    }


def _alpha45_probe(zsync_path: str = "zsync", zsyncmake_path: str = "zsyncmake") -> dict:
    zsync = _alpha45_tool_probe(zsync_path)
    zsyncmake = _alpha45_tool_probe(zsyncmake_path)
    return {
        "zsync": zsync,
        "zsyncmake": zsyncmake,
        "installed": bool(zsync.get("installed") and zsyncmake.get("installed")),
        "version_ok": bool(zsync.get("version_ok") and zsyncmake.get("version_ok")),
        "usable": bool(zsync.get("usable") and zsyncmake.get("usable")),
        "note": "alpha45.1 treats zsync/zsyncmake as usable if executable paths resolve; some builds do not support --version",
    }


def _alpha45_make_deterministic_tar(src_dir: Path, tar_path: Path) -> dict:
    src = Path(src_dir)
    tar_path.parent.mkdir(parents=True, exist_ok=True)
    files = [p for p in sorted(src.rglob("*")) if p.is_file()]
    with tarfile.open(tar_path, "w") as tf:
        for p in files:
            rel = p.relative_to(src).as_posix()
            data = p.read_bytes()
            info = tarfile.TarInfo(rel)
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o644
            import io
            tf.addfile(info, io.BytesIO(data))
    return {"path": str(tar_path), "bytes": tar_path.stat().st_size, "sha256": sha256_file(tar_path), "file_count": len(files)}


class _Alpha45CountingRangeRequestHandler(RangeRequestHandler):
    total_bytes = 0
    range_requests = 0
    full_requests = 0
    request_count = 0
    status_counts: dict[str, int] = {}
    path_bytes: dict[str, int] = {}

    def send_response(self, code, message=None):  # noqa: N802
        self._alpha45_status = int(code)
        self._alpha45_content_length = 0
        return super().send_response(code, message)

    def send_header(self, keyword, value):  # noqa: N802
        if str(keyword).lower() == "content-length":
            try:
                self._alpha45_content_length = int(value)
            except Exception:
                self._alpha45_content_length = 0
        return super().send_header(keyword, value)

    def end_headers(self):  # noqa: N802
        status = int(getattr(self, "_alpha45_status", 0) or 0)
        length = int(getattr(self, "_alpha45_content_length", 0) or 0)
        type(self).request_count += 1
        type(self).status_counts[str(status)] = type(self).status_counts.get(str(status), 0) + 1
        if status in (200, 206):
            type(self).total_bytes += length
            rel_path = self.path.split("?", 1)[0]
            type(self).path_bytes[rel_path] = type(self).path_bytes.get(rel_path, 0) + length
            if status == 206:
                type(self).range_requests += 1
            else:
                type(self).full_requests += 1
        return super().end_headers()

    @classmethod
    def reset_alpha45_stats(cls):
        cls.total_bytes = 0
        cls.range_requests = 0
        cls.full_requests = 0
        cls.request_count = 0
        cls.status_counts = {}
        cls.path_bytes = {}



def _alpha45_detect_wsl_host_ip() -> str:
    """Return Windows host IP as seen from WSL, when available.

    In many WSL2 setups, Windows services bound to 0.0.0.0 are reachable from
    WSL through the nameserver listed in /etc/resolv.conf.
    """
    try:
        p = subprocess.run(
            ["wsl", "-e", "bash", "-lc", "awk '/nameserver/ {print $2; exit}' /etc/resolv.conf"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
        host = (p.stdout or "").strip().splitlines()[0].strip() if (p.stdout or "").strip() else ""
        if host and all(part.isdigit() for part in host.split(".") if part):
            return host
    except Exception:
        pass
    return "127.0.0.1"


def _alpha45_choose_url_host(zsync_path: str, requested: str) -> str:
    if requested and requested != "auto":
        return requested
    # Wrapper/cmd paths usually mean zsync runs inside WSL and cannot reliably
    # use Windows/Python 127.0.0.1. Use WSL's view of the Windows host.
    low = str(zsync_path).lower()
    if low.endswith(".cmd") or "wsl" in low:
        return _alpha45_detect_wsl_host_ip()
    return "127.0.0.1"


class _Alpha45HTTPServer:
    def __init__(self, directory: Path, bind: str = "0.0.0.0", port: int = 0, url_host: str | None = None):
        self.directory = Path(directory)
        RangeRequestHandler.fail_every = 0
        RangeRequestHandler.fail_status = 503
        RangeRequestHandler.truncate_every = 0
        RangeRequestHandler.delay_ms = 0
        RangeRequestHandler.request_counter = 0
        _Alpha45CountingRangeRequestHandler.reset_alpha45_stats()
        import functools
        handler = functools.partial(_Alpha45CountingRangeRequestHandler, directory=str(self.directory))
        self.httpd = ThreadingHTTPServer((bind, int(port)), handler)
        self.bind = bind
        self.url_host = url_host or ("127.0.0.1" if bind in ("0.0.0.0", "::") else bind)
        self.port = int(self.httpd.server_address[1])
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="cld2-alpha45-zsync-http", daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://{self.url_host}:{self.port}/"

    def stats(self) -> dict:
        cls = _Alpha45CountingRangeRequestHandler
        return {
            "total_bytes": cls.total_bytes,
            "range_requests": cls.range_requests,
            "full_requests": cls.full_requests,
            "request_count": cls.request_count,
            "status_counts": dict(cls.status_counts),
            "path_bytes": dict(cls.path_bytes),
        }

    def __enter__(self):
        self.thread.start()
        time.sleep(0.05)
        return self

    def __exit__(self, exc_type, exc, tb):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2.0)


def _alpha45_run_process(cmd: list[str], *, cwd: Path | None = None, timeout: int = 600) -> dict:
    from time import perf_counter
    t0 = perf_counter()
    try:
        p = subprocess.run(cmd, cwd=str(cwd) if cwd else None, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        return {"cmd": cmd, "ok": p.returncode == 0, "returncode": p.returncode, "seconds": perf_counter() - t0, "stdout_tail": (p.stdout or "")[-3000:], "stderr_tail": (p.stderr or "")[-3000:]}
    except Exception as e:
        return {"cmd": cmd, "ok": False, "returncode": -1, "seconds": perf_counter() - t0, "stdout_tail": "", "stderr_tail": str(e)}



def _alpha45_win_to_wsl_path(path: str | Path) -> str:
    p = str(path)
    if len(p) >= 3 and p[1] == ":" and p[2] in ("\\", "/"):
        drive = p[0].lower()
        rest = p[2:].replace("\\", "/")
        return f"/mnt/{drive}{rest}"
    return p.replace("\\", "/")


def _alpha45_is_wsl_wrapper(path: str) -> bool:
    low = str(path).lower()
    return low.endswith(".cmd") or "wsl" in low


def _alpha45_pick_port(seed: str) -> int:
    # Deterministic high ephemeral-ish port to avoid collisions among scenarios.
    h = int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8], 16)
    return 49152 + (h % 12000)


_ALPHA45_WSL_RANGE_SERVER = r"""
import argparse
import json
import os
from pathlib import Path
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

STATS = {
    "total_bytes": 0,
    "range_requests": 0,
    "full_requests": 0,
    "request_count": 0,
    "status_counts": {},
    "path_bytes": {},
}

def write_stats(path):
    try:
        Path(path).write_text(json.dumps(STATS, indent=2, sort_keys=True), encoding="utf-8")
    except Exception:
        pass

class Handler(SimpleHTTPRequestHandler):
    server_version = "CLD2Alpha45WSLRange/1.0"

    def log_message(self, fmt, *args):
        return

    def _record(self, code, length):
        STATS["request_count"] += 1
        STATS["status_counts"][str(code)] = STATS["status_counts"].get(str(code), 0) + 1
        if code in (200, 206):
            STATS["total_bytes"] += int(length or 0)
            if code == 206:
                STATS["range_requests"] += 1
            else:
                STATS["full_requests"] += 1
            p = self.path.split("?", 1)[0]
            STATS["path_bytes"][p] = STATS["path_bytes"].get(p, 0) + int(length or 0)
        write_stats(self.server.stats_path)

    def send_head(self):
        path = self.translate_path(self.path)
        if os.path.isdir(path):
            return super().send_head()
        try:
            f = open(path, "rb")
        except OSError:
            self.send_error(404, "File not found")
            self._record(404, 0)
            return None
        fs = os.fstat(f.fileno())
        size = fs.st_size
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            try:
                spec = rng.split("=", 1)[1].split(",", 1)[0].strip()
                start_s, end_s = (spec.split("-", 1) + [""])[:2]
                start = int(start_s) if start_s else 0
                end = int(end_s) if end_s else size - 1
                start = max(0, min(start, size - 1))
                end = max(start, min(end, size - 1))
                length = end - start + 1
                self.send_response(206)
                self.send_header("Content-type", self.guess_type(path))
                self.send_header("Content-Length", str(length))
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                f.seek(start)
                self._range_remaining = length
                self._record(206, length)
                return f
            except Exception:
                pass
        self.send_response(200)
        self.send_header("Content-type", self.guess_type(path))
        self.send_header("Content-Length", str(size))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        self._range_remaining = None
        self._record(200, size)
        return f

    def copyfile(self, source, outputfile):
        rem = getattr(self, "_range_remaining", None)
        if rem is None:
            return super().copyfile(source, outputfile)
        bufsize = 64 * 1024
        while rem > 0:
            chunk = source.read(min(bufsize, rem))
            if not chunk:
                break
            outputfile.write(chunk)
            rem -= len(chunk)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--dir", required=True)
    ap.add_argument("--stats", required=True)
    args = ap.parse_args()
    os.chdir(args.dir)
    Handler.directory = args.dir
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.stats_path = args.stats
    write_stats(args.stats)
    try:
        httpd.serve_forever()
    finally:
        write_stats(args.stats)

if __name__ == "__main__":
    main()
"""


class _Alpha45WSLHTTPServer:
    def __init__(self, directory: Path, scenario: str):
        self.directory = Path(directory)
        self.scenario = scenario
        self.port = _alpha45_pick_port(str(self.directory) + scenario)
        self.host = "127.0.0.1"
        self.stats_file = self.directory / "_alpha45_wsl_http_stats.json"
        self.script_file = self.directory / "_alpha45_wsl_range_server.py"
        self.proc = None
        self.script_file.write_text(_ALPHA45_WSL_RANGE_SERVER, encoding="utf-8")

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def __enter__(self):
        import shlex
        wsl_dir = _alpha45_win_to_wsl_path(self.directory)
        cmd = (
            "cd " + shlex.quote(wsl_dir) +
            " && exec python3 _alpha45_wsl_range_server.py --host 127.0.0.1 --port " + str(self.port) +
            " --dir . --stats _alpha45_wsl_http_stats.json"
        )
        self.proc = subprocess.Popen(["wsl", "-e", "bash", "-lc", cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Give WSL/python a moment to bind the port.
        time.sleep(0.8)
        return self

    def stats(self) -> dict:
        if self.stats_file.exists():
            try:
                return json.loads(self.stats_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"total_bytes": 0, "range_requests": 0, "full_requests": 0, "request_count": 0, "status_counts": {}, "path_bytes": {}}

    def __exit__(self, exc_type, exc, tb):
        if self.proc is not None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        time.sleep(0.1)




def _alpha45_run_wsl_zsync_native(
    *,
    seed_win: Path,
    output_win: Path,
    url: str,
    scenario: str,
    timeout: int = 900,
) -> dict:
    """Run zsync fully inside WSL using /tmp for seed/output.

    zsync on /mnt/c may fail after download with `ftruncate: No such file or
    directory`. This helper copies the seed tar to a native Linux temp directory,
    downloads to /tmp, then copies the verified output back to /mnt/c for Python
    SHA256 verification.
    """
    from time import perf_counter
    t0 = perf_counter()
    seed_wsl = _alpha45_win_to_wsl_path(seed_win)
    output_wsl = _alpha45_win_to_wsl_path(output_win)
    tmp_dir = f"/tmp/cld2_alpha45_7_{scenario.replace('-', '_')}_{os.getpid()}"
    seed_name = "seed_v1.tar"
    out_name = "downloaded_v2.tar"
    cmd_text = (
        "set -e; "
        f"rm -rf {shlex.quote(tmp_dir)}; "
        f"mkdir -p {shlex.quote(tmp_dir)}; "
        f"cp {shlex.quote(seed_wsl)} {shlex.quote(tmp_dir + '/' + seed_name)}; "
        f"cd {shlex.quote(tmp_dir)}; "
        f"zsync -i {shlex.quote(seed_name)} -o {shlex.quote(out_name)} {shlex.quote(url)}; "
        f"mkdir -p {shlex.quote(str(Path(output_wsl).parent).replace(chr(92), '/'))}; "
        f"cp {shlex.quote(tmp_dir + '/' + out_name)} {shlex.quote(output_wsl)}; "
        f"rm -rf {shlex.quote(tmp_dir)}"
    )
    try:
        p = subprocess.run(["wsl", "-e", "bash", "-lc", cmd_text], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        return {
            "cmd": ["wsl", "-e", "bash", "-lc", cmd_text],
            "mode": "wsl-native-tmp-output",
            "ok": p.returncode == 0,
            "returncode": p.returncode,
            "seconds": perf_counter() - t0,
            "stdout_tail": (p.stdout or "")[-3000:],
            "stderr_tail": (p.stderr or "")[-3000:],
            "tmp_dir": tmp_dir,
        }
    except Exception as e:
        return {
            "cmd": ["wsl", "-e", "bash", "-lc", cmd_text],
            "mode": "wsl-native-tmp-output",
            "ok": False,
            "returncode": -1,
            "seconds": perf_counter() - t0,
            "stdout_tail": "",
            "stderr_tail": str(e),
            "tmp_dir": tmp_dir,
        }



def _alpha45_run_scenario(
    scenario_out: Path,
    *,
    scenario: str,
    file_size: str,
    small_file_count: int,
    zsync_path: str,
    zsyncmake_path: str,
    zsync_bind_host: str = "0.0.0.0",
    zsync_url_host: str = "auto",
    zsync_server_mode: str = "auto",
) -> dict:
    if scenario_out.exists():
        shutil.rmtree(scenario_out)
    scenario_out.mkdir(parents=True)
    pair_root = scenario_out / "pairs"
    v1, v2 = _alpha40_write_pair(pair_root, scenario=scenario, file_size=file_size, small_file_count=small_file_count)
    http_root = scenario_out / "http"
    http_root.mkdir(parents=True)
    tar_v1 = http_root / f"{scenario}_v1.tar"
    tar_v2 = http_root / f"{scenario}_v2.tar"
    tar1_info = _alpha45_make_deterministic_tar(v1, tar_v1)
    tar2_info = _alpha45_make_deterministic_tar(v2, tar_v2)
    zsync_file = http_root / f"{scenario}_v2.tar.zsync"

    zsyncmake_exe = shutil.which(zsyncmake_path) or zsyncmake_path
    zsync_exe = shutil.which(zsync_path) or zsync_path
    make_cmd = [zsyncmake_exe, "-o", str(zsync_file), "-u", tar_v2.name, str(tar_v2)]
    make_res = _alpha45_run_process(make_cmd, timeout=600)

    download_dir = scenario_out / "download"
    download_dir.mkdir(parents=True)
    seed = download_dir / f"{scenario}_seed_v1.tar"
    shutil.copy2(tar_v1, seed)
    output = download_dir / f"{scenario}_downloaded_v2.tar"
    zsync_res = {}
    server_stats = {}
    url = ""
    if make_res.get("ok"):
        mode = zsync_server_mode
        if mode == "auto":
            mode = "wsl-native" if _alpha45_is_wsl_wrapper(zsync_path) else "windows"
        if mode == "wsl-native":
            with _Alpha45WSLHTTPServer(http_root, scenario) as server:
                url = server.base_url + zsync_file.name
                if _alpha45_is_wsl_wrapper(zsync_path):
                    zsync_res = _alpha45_run_wsl_zsync_native(seed_win=seed, output_win=output, url=url, scenario=scenario, timeout=900)
                else:
                    zsync_cmd = [zsync_exe, "-i", str(seed), "-o", str(output), url]
                    zsync_res = _alpha45_run_process(zsync_cmd, cwd=download_dir, timeout=900)
                time.sleep(0.05)
                server_stats = server.stats()
        else:
            chosen_url_host = _alpha45_choose_url_host(zsync_path, zsync_url_host)
            with _Alpha45HTTPServer(http_root, bind=zsync_bind_host, url_host=chosen_url_host) as server:
                url = server.base_url + zsync_file.name
                # zsync uses the seed file to reconstruct v2 and fetches ranges from the HTTP server.
                zsync_cmd = [zsync_exe, "-i", str(seed), "-o", str(output), url]
                zsync_res = _alpha45_run_process(zsync_cmd, cwd=download_dir, timeout=900)
                time.sleep(0.05)
                server_stats = server.stats()
    verify = {
        "ok": bool(output.exists() and sha256_file(output) == tar2_info.get("sha256")),
        "downloaded_exists": output.exists(),
        "downloaded_sha256": sha256_file(output) if output.exists() else "",
        "expected_sha256": tar2_info.get("sha256"),
    }
    full_bytes = int(tar2_info.get("bytes") or 0)
    zsync_bytes = int(server_stats.get("total_bytes") or 0)
    result = {
        "scenario": scenario,
        "ok": bool(make_res.get("ok") and zsync_res.get("ok") and verify.get("ok")),
        "tar_v1": tar1_info,
        "tar_v2": tar2_info,
        "zsyncmake": make_res,
        "zsync": zsync_res,
        "verify": verify,
        "server_url": url,
        "zsync_bind_host": zsync_bind_host,
        "zsync_url_host": _alpha45_choose_url_host(zsync_path, zsync_url_host),
        "zsync_server_mode": zsync_server_mode,
        "zsync_effective_server_mode": ("wsl-native" if (zsync_server_mode == "auto" and _alpha45_is_wsl_wrapper(zsync_path)) else zsync_server_mode),
        "http_stats": server_stats,
        "zsync_http_bytes": zsync_bytes,
        "full_v2_bytes": full_bytes,
        "zsync_vs_full_ratio": (zsync_bytes / full_bytes) if full_bytes else None,
        "zsync_savings_vs_full_ratio": (1.0 - (zsync_bytes / full_bytes)) if full_bytes else None,
    }
    (scenario_out / "zsync_scenario_result.json").write_text(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    return result


def _alpha45_compare_with_cld2(zsync_results: list[dict], cld2_matrix: dict) -> list[dict]:
    by_name = {s.get("scenario"): s for s in (cld2_matrix.get("scenarios") or [])}
    rows = []
    for r in zsync_results:
        name = r.get("scenario")
        cld = by_name.get(name) or {}
        warm = cld.get("warm_update") or {}
        baseline = cld.get("full_object_baseline") or {}
        v2 = baseline.get("v2") or {}
        zsync_bytes = r.get("zsync_http_bytes")
        cld2_bytes = warm.get("downloaded_pack_bytes")
        ratio = None
        if zsync_bytes and cld2_bytes is not None:
            try:
                ratio = float(cld2_bytes) / float(zsync_bytes)
            except Exception:
                ratio = None
        rows.append({
            "scenario": name,
            "zsync_ok": r.get("ok"),
            "cld2_ok": cld.get("ok"),
            "zsync_http_bytes": zsync_bytes,
            "zsync_full_v2_bytes": r.get("full_v2_bytes"),
            "zsync_vs_full_ratio": r.get("zsync_vs_full_ratio"),
            "cld2_warm_bytes": cld2_bytes,
            "cld2_full_v2_bytes": v2.get("downloaded_bytes"),
            "cld2_vs_zsync_ratio": ratio,
            "note": "zsync HTTP tar delta baseline vs CLD2 local MinIO warm update; workload model differs",
        })
    return rows


def _alpha45_md(result: dict) -> str:
    probe = result.get("zsync_probe") or {}
    rows = []
    for r in result.get("comparison_rows", []):
        rows.append(
            f"| {r.get('scenario')} | {r.get('zsync_ok')} | {r.get('cld2_ok')} | {r.get('zsync_http_bytes')} | {r.get('zsync_full_v2_bytes')} | {r.get('zsync_vs_full_ratio')} | {r.get('cld2_warm_bytes')} | {r.get('cld2_vs_zsync_ratio')} |"
        )
    if not rows:
        rows.append("| _no direct zsync benchmark_ |  |  |  |  |  |  |  |")
    return f"""# CLD2 alpha45 zsync HTTP delta baseline

## Status

- ok: `{result.get('ok')}`
- probe_only: `{result.get('probe_only')}`
- zsync_installed: `{probe.get('installed')}`
- zsync_stack_usable: `{probe.get('usable')}`
- probe_mode: `nonblocking_path_only`
- zsync_version: `{(probe.get('zsync') or {}).get('version')}`
- zsyncmake_version: `{(probe.get('zsyncmake') or {}).get('version')}`
- direct_baseline_benchmarks_run: `{result.get('direct_baseline_benchmarks_run')}`
- cld2_matrix_ok: `{(result.get('cld2_matrix') or {}).get('ok')}`
- cld2_attach_attempt_count: `{len(result.get('cld2_attach_attempts') or [])}`

## Boundary

This compares zsync HTTP delta over tar-packaged releases with CLD2 local MinIO warm-update bytes.

It is useful but not identical:
- zsync is file-oriented;
- CLD2 is repo/chunk/object-update oriented;
- this is local HTTP, not CDN/AWS.

## Results

| Scenario | zsync OK | CLD2 OK | zsync HTTP bytes | zsync full v2 tar bytes | zsync/full ratio | CLD2 warm bytes | CLD2/zsync ratio |
|---|---:|---:|---:|---:|---:|---:|---:|
{chr(10).join(rows)}

## If zsync is missing

Install `zsync` and `zsyncmake`, preferably in WSL/Linux, then rerun. Do not claim any zsync comparison if `zsync_installed=False`.
"""


def _alpha45_html(result: dict) -> str:
    import html
    md = _alpha45_md(result)
    ok_class = "ok" if result.get("ok") else "bad"
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>CLD2 alpha45 zsync baseline</title>
<style>body{{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:2rem;line-height:1.45;background:#fafafa}}pre{{background:#111;color:#eee;padding:1rem;overflow:auto}}code{{background:#f3f3f3;padding:.1rem .25rem;border-radius:4px}}.badge{{padding:.25rem .7rem;border-radius:999px;font-weight:700}}.ok{{background:#e7f7ed;color:#126b35}}.bad{{background:#fdeaea;color:#8b1a1a}}</style></head>
<body><h1>CLD2 alpha45 zsync HTTP baseline</h1><p>Status: <span class="badge {ok_class}">{html.escape(str(result.get('ok')))}</span></p>
<pre>{html.escape(md)}</pre></body></html>"""



def _alpha45_run_cld2_matrix_with_retry(
    *,
    out: Path,
    file_size: str,
    scenarios: str | list[str],
    small_file_count: int,
    endpoint: str,
    access_key: str,
    secret_key: str,
    bucket: str,
    prefix: str,
    alias: str,
    mc_path: str,
    public_base_url: str,
    preflight_only: bool,
    skip_upload: bool,
    codec: str,
    chunker: str,
    fixed_size: str,
    chunk_min: str,
    chunk_avg: str,
    chunk_max: str,
    fastcdc_stride: int,
    http_retries: int,
    http_backoff: float,
    parallel: int,
    cost_per_gb: float,
    download_counts: str | list[int],
    currency: str,
    keep_heavy: bool,
    max_mb: float,
    attach_retries: int = 4,
    attach_backoff: float = 2.0,
) -> tuple[dict, list[dict]]:
    """Run attached CLD2 MinIO matrix with retry/backoff.

    Alpha45.7 showed zsync could be valid while the attached CLD2 matrix failed
    with transient MinIO/object-store errors such as:
      "Resource requested is unwritable, please reduce your request rate"

    This helper retries with unique bucket/alias suffixes and records all attempts.
    """
    attempts: list[dict] = []
    attach_retries = max(1, int(attach_retries or 1))
    last: dict = {}
    transient_terms = [
        "unwritable",
        "reduce your request rate",
        "temporarily",
        "timeout",
        "timed out",
        "connection refused",
        "busy",
    ]

    for attempt in range(1, attach_retries + 1):
        attempt_bucket = bucket if attempt == 1 else f"{bucket}-r{attempt}"
        attempt_alias = alias if attempt == 1 else f"{alias}r{attempt}"
        attempt_out = out / ("cld2_matrix" if attempt == 1 else f"cld2_matrix_retry_{attempt}")
        started = time.time()
        try:
            res = run_minio_cost_matrix_pilot(
                attempt_out,
                file_size=file_size,
                scenarios=scenarios,
                small_file_count=small_file_count,
                endpoint=endpoint,
                access_key=access_key,
                secret_key=secret_key,
                bucket=attempt_bucket,
                prefix=prefix,
                alias=attempt_alias,
                mc_path=mc_path,
                public_base_url=public_base_url,
                preflight_only=preflight_only,
                skip_upload=skip_upload,
                codec=codec,
                chunker=chunker,
                fixed_size=fixed_size,
                chunk_min=chunk_min,
                chunk_avg=chunk_avg,
                chunk_max=chunk_max,
                fastcdc_stride=fastcdc_stride,
                http_retries=http_retries,
                http_backoff=http_backoff,
                parallel=parallel,
                cost_per_gb=cost_per_gb,
                download_counts=download_counts,
                currency=currency,
                make_review=False,
                keep_heavy=keep_heavy,
                max_mb=max_mb,
            )
        except Exception as e:
            res = {"ok": False, "error": str(e), "exception_type": type(e).__name__}
        last = res if isinstance(res, dict) else {"ok": False, "error": repr(res)}
        text = json.dumps(last, ensure_ascii=False, sort_keys=True)[:8000]
        transient = any(term in text.lower() for term in transient_terms)
        attempts.append({
            "attempt": attempt,
            "bucket": attempt_bucket,
            "alias": attempt_alias,
            "out_dir": str(attempt_out),
            "ok": bool(last.get("ok")),
            "seconds": time.time() - started,
            "transient_like_error": bool(transient),
            "error_excerpt": text[:1000] if not last.get("ok") else "",
        })
        if last.get("ok"):
            last["attach_retry"] = {
                "ok": True,
                "attempts": attempts,
                "selected_attempt": attempt,
            }
            return last, attempts
        if attempt < attach_retries:
            sleep_s = float(attach_backoff) * attempt
            time.sleep(sleep_s)

    if isinstance(last, dict):
        last["attach_retry"] = {
            "ok": False,
            "attempts": attempts,
            "selected_attempt": None,
        }
    return last, attempts



def run_zsync_baseline_pilot(
    out_dir: str | Path,
    *,
    file_size: str = "64MiB",
    scenarios: str | list[str] = "normal,high-entropy,small-files,heavy-change",
    small_file_count: int = 256,
    zsync_path: str = "zsync",
    zsyncmake_path: str = "zsyncmake",
    zsync_bind_host: str = "0.0.0.0",
    zsync_url_host: str = "auto",
    zsync_server_mode: str = "auto",
    probe_only: bool = False,
    codec: str = "zstd",
    chunker: str = "fastcdc",
    fixed_size: str = "1MiB",
    chunk_min: str = "64KiB",
    chunk_avg: str = "256KiB",
    chunk_max: str = "1MiB",
    fastcdc_stride: int = 4,
    cost_per_gb: float = 0.04,
    download_counts: str | list[int] = "1,10,1000,10000,100000",
    currency: str = "EUR",
    endpoint: str = "",
    access_key: str = "",
    secret_key: str = "",
    bucket: str = "cld2-alpha45-bucket",
    prefix: str = "releases",
    alias: str = "cld2alpha45",
    mc_path: str = "mc",
    public_base_url: str = "",
    preflight_only: bool = False,
    skip_upload: bool = False,
    http_retries: int = 3,
    http_backoff: float = 0.05,
    parallel: int = 1,
    cld2_attach_retries: int = 4,
    cld2_attach_backoff: float = 2.0,
    make_review: bool = False,
    keep_heavy: bool = True,
    max_mb: float = 10.0,
) -> dict:
    out = Path(out_dir)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    probe = _alpha45_probe(zsync_path, zsyncmake_path)
    scenario_names = _alpha40_parse_scenarios(scenarios)
    zsync_results = []
    cld2_matrix = {}
    cld2_attach_attempts = []

    if not probe_only and probe.get("usable"):
        for scenario in scenario_names:
            zsync_results.append(_alpha45_run_scenario(out / f"zsync_{scenario}", scenario=scenario, file_size=file_size, small_file_count=small_file_count, zsync_path=zsync_path, zsyncmake_path=zsyncmake_path, zsync_bind_host=zsync_bind_host, zsync_url_host=zsync_url_host, zsync_server_mode=zsync_server_mode))
        if endpoint and access_key and secret_key:
            cld2_matrix, cld2_attach_attempts = _alpha45_run_cld2_matrix_with_retry(
                out=out,
                file_size=file_size,
                scenarios=scenarios,
                small_file_count=small_file_count,
                endpoint=endpoint,
                access_key=access_key,
                secret_key=secret_key,
                bucket=bucket,
                prefix=prefix,
                alias=alias,
                mc_path=mc_path,
                public_base_url=public_base_url,
                preflight_only=preflight_only,
                skip_upload=skip_upload,
                codec=codec,
                chunker=chunker,
                fixed_size=fixed_size,
                chunk_min=chunk_min,
                chunk_avg=chunk_avg,
                chunk_max=chunk_max,
                fastcdc_stride=fastcdc_stride,
                http_retries=http_retries,
                http_backoff=http_backoff,
                parallel=parallel,
                cost_per_gb=cost_per_gb,
                download_counts=download_counts,
                currency=currency,
                keep_heavy=keep_heavy,
                max_mb=max_mb,
                attach_retries=cld2_attach_retries,
                attach_backoff=cld2_attach_backoff,
            )
    comparison_rows = _alpha45_compare_with_cld2(zsync_results, cld2_matrix) if zsync_results and cld2_matrix else []
    result = {
        "schema": "CoreLangDistribution/ZsyncBaseline",
        "version": "2.0-alpha45",
        "ok": bool((probe_only and probe) or (probe.get("usable") and zsync_results and all(r.get("ok") for r in zsync_results) and (not endpoint or cld2_matrix.get("ok")))),
        "probe_only": bool(probe_only),
        "zsync_probe": probe,
        "out_dir": str(out),
        "file_size": file_size,
        "zsync_bind_host": zsync_bind_host,
        "zsync_url_host": zsync_url_host,
        "zsync_server_mode": zsync_server_mode,
        "zsync_effective_url_host": _alpha45_choose_url_host(zsync_path, zsync_url_host),
        "zsync_effective_server_mode": ("wsl-native" if (zsync_server_mode == "auto" and _alpha45_is_wsl_wrapper(zsync_path)) else zsync_server_mode),
        "scenario_names": scenario_names,
        "zsync_results": zsync_results,
        "cld2_matrix": cld2_matrix,
        "cld2_attach_attempts": cld2_attach_attempts,
        "comparison_rows": comparison_rows,
        "direct_baseline_benchmarks_run": bool(zsync_results),
        "notes": [
            "zsync baseline is file/tar-oriented; CLD2 is repo/object/chunk-oriented",
            "only claim zsync comparison when zsync and zsyncmake are installed and direct_baseline_benchmarks_run=True",
        ],
    }
    (out / "zsync_baseline_result.json").write_text(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    (out / "ZSYNC_BASELINE_REPORT.md").write_text(_alpha45_md(result), encoding="utf-8")
    (out / "index.html").write_text(_alpha45_html(result), encoding="utf-8")
    with (out / "zsync_baseline_summary.csv").open("w", newline="", encoding="utf-8") as f:
        fieldnames = ["scenario", "zsync_ok", "cld2_ok", "zsync_http_bytes", "zsync_full_v2_bytes", "zsync_vs_full_ratio", "cld2_warm_bytes", "cld2_vs_zsync_ratio", "note"]
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for row in comparison_rows:
            w.writerow({k: row.get(k) for k in fieldnames})
    with (out / "zsync_preflight.json").open("w", encoding="utf-8") as f:
        json.dump(probe, f, indent=2, sort_keys=True)
    if not keep_heavy:
        for p in out.iterdir():
            if p.is_dir() and (p.name.startswith("zsync_") or p.name == "cld2_matrix"):
                shutil.rmtree(p, ignore_errors=True)
    if make_review:
        result["review_zip"] = make_review_zip(out, str(out) + "_REVIEW.zip", max_mb=max_mb)
        (out / "zsync_baseline_result.json").write_text(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
        try:
            _zip_out = result["review_zip"].get("zip_out") if isinstance(result.get("review_zip"), dict) else result.get("review_zip")
            if _zip_out:
                with zipfile.ZipFile(_zip_out, "a", compression=zipfile.ZIP_DEFLATED) as _zf:
                    for _name in [
                        "zsync_baseline_result.json",
                        "zsync_baseline_summary.csv",
                        "zsync_preflight.json",
                        "ZSYNC_BASELINE_REPORT.md",
                    ]:
                        _p = out / _name
                        if _p.exists() and _name not in _zf.namelist():
                            _zf.write(_p, arcname=_name)
        except Exception:
            pass
    return result


def render_static_report(src_dir: str | Path, out_dir: str | Path, *, title: str = "CLD2 benchmark report", make_review: bool = False, max_mb: float = 10.0) -> dict:
    """Render a dependency-free static HTML/Markdown report from CLD2 benchmark outputs.

    Supports corpus benchmark directories first, and falls back to scenario planner summaries when available.
    """
    from html import escape

    src = Path(src_dir)
    out = Path(out_dir)
    if not src.exists():
        raise FileNotFoundError(src)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    corpus_json_path = src / "corpus_benchmark_result.json"
    corpus_csv_path = src / "corpus_benchmark_summary.csv"
    scenario_json_path = src / "cost_aware_scenarios_result.json"
    rows = _alpha32_load_csv_rows(corpus_csv_path)
    currency = ""
    source_kind = "generic"
    result = None
    if corpus_json_path.exists():
        result = json.loads(corpus_json_path.read_text(encoding="utf-8"))
        currency = result.get("currency", "") or ""
        source_kind = "corpus"
    elif scenario_json_path.exists():
        result = json.loads(scenario_json_path.read_text(encoding="utf-8"))
        currency = result.get("currency", "") or ""
        source_kind = "scenario"

    cards: list[tuple[str, str]] = []
    table_html = ""
    interpretation = []
    markdown_lines = [f"# {title}", "", f"Source: `{src}`", "", f"Report type: `{source_kind}`", ""]

    if source_kind == "corpus" and rows:
        summary = result.get("summary", {}) if isinstance(result, dict) else {}
        cards = [
            ("Scenarios", str(summary.get("scenario_count", len(rows)))),
            ("Wins", str(summary.get("wins", sum(1 for r in rows if r.get("verdict") == "win")))),
            ("Near ties", str(summary.get("near_ties", sum(1 for r in rows if r.get("verdict") == "near_tie")))),
            ("Losses", str(summary.get("losses", sum(1 for r in rows if r.get("verdict") == "loss")))),
            ("Inconclusive", str(summary.get("inconclusive", sum(1 for r in rows if r.get("verdict") == "inconclusive")))),
        ]
        table_parts = [
            '<table><thead><tr><th>Scenario</th><th>Kind</th><th>Verdict</th><th>Public default</th><th>Best CLD2</th><th>Best external</th><th>Ratio</th><th>Reason</th></tr></thead><tbody>'
        ]
        markdown_lines += ["## Corpus summary", "", "| Scenario | Kind | Verdict | Public default | Best CLD2 | Best external | Ratio |", "|---|---|---|---|---|---|---:|"]
        for r in rows:
            verdict = r.get("verdict", "")
            ratio = r.get("cost_ratio_cld2_vs_best_external", "")
            try:
                ratio_s = f"{float(ratio):.4f}"
            except Exception:
                ratio_s = ""
            best_cld2 = f"{r.get('best_cld2_plan','')} ({_alpha32_fmt_bytes(r.get('best_cld2_bytes'))}, {_alpha32_fmt_money(r.get('best_cld2_total_cost'), currency)})"
            best_ext = f"{r.get('best_external_plan','')} ({_alpha32_fmt_bytes(r.get('best_external_bytes'))}, {_alpha32_fmt_money(r.get('best_external_total_cost'), currency)})"
            table_parts.append(
                "<tr>"
                f"<td><code>{escape(r.get('scenario',''))}</code></td>"
                f"<td>{escape(r.get('scenario_kind',''))}</td>"
                f"<td>{_alpha32_badge(verdict)}</td>"
                f"<td><code>{escape(r.get('public_default_plan',''))}</code></td>"
                f"<td><code>{escape(best_cld2)}</code></td>"
                f"<td><code>{escape(best_ext)}</code></td>"
                f"<td>{escape(ratio_s)}</td>"
                f"<td>{escape(r.get('reason',''))}</td>"
                "</tr>"
            )
            markdown_lines.append(f"| `{r.get('scenario','')}` | {r.get('scenario_kind','')} | {verdict} | `{r.get('public_default_plan','')}` | `{best_cld2}` | `{best_ext}` | {ratio_s} |")
        table_parts.append("</tbody></table>")
        table_html = "\n".join(table_parts)
        interpretation = [
            "This report is intentionally falsification-oriented: wins are useful, but losses and near ties define safe fallback boundaries.",
            "A win means CLD2 is materially cheaper than the best external baseline under the configured scenario. A loss means the external baseline should be preferred.",
            "Do not claim CLD2 is a universal compressor; claim that it is a cost-aware distribution planner that exploits real reuse when reuse exists.",
        ]
    elif source_kind == "scenario" and isinstance(result, dict):
        scenarios = result.get("scenarios", {})
        cards = [("Scenario profiles", str(len(scenarios)))]
        table_parts = ['<table><thead><tr><th>Scenario</th><th>Download count</th><th>Default plan</th><th>Bytes</th><th>Total cost</th></tr></thead><tbody>']
        markdown_lines += ["## Cost-aware scenarios", "", "| Scenario | Download count | Default plan | Bytes | Total cost |", "|---|---:|---|---:|---:|"]
        for name, sub in scenarios.items():
            rec = (sub.get("cost_aware_recommendations") or {}).get("cost_aware_default") or {}
            row = (
                "<tr>"
                f"<td><code>{escape(name)}</code></td>"
                f"<td>{escape(str(sub.get('download_count','')))}</td>"
                f"<td><code>{escape(rec.get('plan',''))}</code></td>"
                f"<td>{escape(_alpha32_fmt_bytes(rec.get('bytes')))}</td>"
                f"<td>{escape(_alpha32_fmt_money(rec.get('total_cost'), currency))}</td>"
                "</tr>"
            )
            table_parts.append(row)
            markdown_lines.append(f"| `{name}` | {sub.get('download_count','')} | `{rec.get('plan','')}` | {_alpha32_fmt_bytes(rec.get('bytes'))} | {_alpha32_fmt_money(rec.get('total_cost'), currency)} |")
        table_parts.append("</tbody></table>")
        table_html = "\n".join(table_parts)
        interpretation = ["This scenario report shows how CLD2 switches plans as download count changes."]
    else:
        report_files = sorted([p.relative_to(src).as_posix() for p in src.rglob("*") if p.is_file() and p.suffix.lower() in {".json", ".csv", ".md", ".txt"}])
        cards = [("Files", str(len(report_files)))]
        table_html = "<ul>" + "".join(f"<li><code>{escape(x)}</code></li>" for x in report_files[:200]) + "</ul>"
        interpretation = ["Generic report directory: no recognized CLD2 benchmark result JSON was found."]

    card_html = "\n".join(f'<div class="card"><div class="card-value">{escape(value)}</div><div class="card-label">{escape(label)}</div></div>' for label, value in cards)
    interp_html = "\n".join(f"<p>{escape(x)}</p>" for x in interpretation)
    html = f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>{escape(title)}</title>
  <style>
    body {{ font-family: system-ui, -apple-system, Segoe UI, sans-serif; margin: 2rem; color: #111; background: #fafafa; }}
    header {{ margin-bottom: 1.5rem; }}
    h1 {{ margin-bottom: .2rem; }}
    .sub {{ color: #555; }}
    .cards {{ display: flex; flex-wrap: wrap; gap: 1rem; margin: 1.2rem 0 1.6rem; }}
    .card {{ background: white; border: 1px solid #ddd; border-radius: 12px; padding: 1rem 1.2rem; min-width: 120px; box-shadow: 0 1px 2px rgba(0,0,0,.04); }}
    .card-value {{ font-size: 1.6rem; font-weight: 700; }}
    .card-label {{ color: #555; font-size: .9rem; }}
    table {{ border-collapse: collapse; width: 100%; background: white; border: 1px solid #ddd; }}
    th, td {{ border-bottom: 1px solid #eee; padding: .6rem .7rem; text-align: left; vertical-align: top; }}
    th {{ background: #f1f1f1; }}
    code {{ background: #f5f5f5; padding: .1rem .25rem; border-radius: 4px; }}
    .badge {{ display: inline-block; border-radius: 999px; padding: .15rem .55rem; font-weight: 700; font-size: .85rem; }}
    .win {{ background: #e7f7ed; color: #126b35; }}
    .loss {{ background: #fdeaea; color: #8b1a1a; }}
    .tie {{ background: #fff4d6; color: #755000; }}
    .unknown {{ background: #eeeeee; color: #444; }}
    .note {{ background: white; border-left: 5px solid #777; padding: .8rem 1rem; margin: 1rem 0; }}
    footer {{ margin-top: 2rem; color: #666; font-size: .9rem; }}
  </style>
</head>
<body>
<header>
  <h1>{escape(title)}</h1>
  <div class=\"sub\">CLD2 static report · source: <code>{escape(str(src))}</code></div>
</header>
<section class=\"cards\">{card_html}</section>
<section>
  <h2>Results</h2>
  {table_html}
</section>
<section class=\"note\">
  <h2>Interpretation guardrail</h2>
  {interp_html}
</section>
<footer>Generated by CoreLangDistribution 2.0.</footer>
</body>
</html>
"""
    (out / "index.html").write_text(html, encoding="utf-8")
    markdown_lines += ["", "## Interpretation guardrail", ""] + [f"- {x}" for x in interpretation]
    (out / "report.md").write_text("\n".join(markdown_lines) + "\n", encoding="utf-8")
    manifest = {
        "schema": "CoreLangDistribution/StaticReport",
        "version": "2.0.0-alpha50.2",
        "ok": True,
        "source_kind": source_kind,
        "src_dir": str(src),
        "out_dir": str(out),
        "title": title,
        "files": {
            "html": str(out / "index.html"),
            "markdown": str(out / "report.md"),
        },
        "cards": [{"label": label, "value": value} for label, value in cards],
    }
    if result is not None:
        (out / "source_result_excerpt.json").write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    if rows:
        with (out / "source_summary.csv").open("w", newline="", encoding="utf-8") as f:
            fieldnames = list(rows[0].keys())
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(rows)
    if make_review:
        zip_path = out.with_name(out.name + "_REVIEW.zip")
        manifest["review_zip"] = make_review_zip(out, zip_path, max_mb=max_mb)
    (out / "static_report_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return manifest


def _is_review_zip_candidate(rel: Path) -> bool:
    """Return True for high-value human/AI review artifacts, not generated repo metadata."""
    parts = {x.lower() for x in rel.parts}
    name = rel.name.lower()
    suffix = rel.suffix.lower()
    if suffix not in {".json", ".csv", ".md", ".txt", ".log", ".sha256", ".html"}:
        return False
    # Never include generated repository internals or pack metadata in review zips.
    if any(part.endswith(".cldrepo") for part in parts):
        return False
    if any(part.endswith(".egg-info") for part in parts):
        return False
    if "__pycache__" in parts:
        return False
    if "packs" in parts or "cache" in parts or "install" in parts:
        return False
    if name in {"chunks.idx.json", "files.idx.json", "release.json", "signatures.json"}:
        return False
    if suffix in {".sha256", ".log", ".txt", ".html"}:
        return True
    # Include root/summary scenario planner artifacts and hybrid planner summary only.
    allowed_exact = {
        "bench_real_result.json",
        "bench_real_summary.csv",
        "bench_real_technical_report.md",
        "cld2_savings_business_report.md",
        "review_file_manifest.csv",
        "cost_aware_scenarios_result.json",
        "cost_aware_scenarios_report.md",
        "cost_aware_planner_result.json",
        "cost_aware_planner_report.md",
        "cost_aware_planner_summary.csv",
        "hybrid_planner_result.json",
        "hybrid_planner_report.md",
        "hybrid_planner_summary.csv",
        "manifest.sha256",
        "index.html",
        "report.md",
        "static_report_manifest.json",
        "source_summary.csv",
        "source_result_excerpt.json",
        "corpus_benchmark_result.json",
        "corpus_benchmark_report.md",
        "corpus_benchmark_summary.csv",
        "mirror_pilot_result.json",
        "mirror_pilot_report.md",
        "mirror_pilot_summary.csv",
        "object_store_pilot_result.json",
        "object_store_pilot_report.md",
        "object_store_pilot_summary.csv",
        "object_store_manifest.json",
        "minio_pilot_result.json",
        "minio_pilot_report.md",
        "minio_pilot_summary.csv",
        "minio_robustness_result.json",
        "minio_robustness_report.md",
        "minio_robustness_summary.csv",
        "minio_full_baseline_result.json",
        "minio_full_baseline_report.md",
        "minio_full_baseline_summary.csv",
        "minio_cost_model.csv",
        "minio_cost_matrix_result.json",
        "minio_cost_matrix_report.md",
        "minio_cost_matrix_summary.csv",
        "minio_cost_matrix_costs.csv",
        "anti_cherrypick_result.json",
        "anti_cherrypick_report.md",
        "anti_cherrypick_claims.md",
        "anti_cherrypick_classification.csv",
        "github_report_result.json",
        "README_GITHUB.md",
        "GITHUB_TECHNICAL_REPORT.md",
        "POSITIONING.md",
        "LIMITATIONS.md",
        "HOW_TO_REPRODUCE.md",
        "HONEST_CLAIMS.md",
        "comparison_harness_result.json",
        "baseline_tool_preflight.csv",
        "BASELINE_TOOLS_MATRIX.md",
        "DIRECT_COMPARISON_PLAN.md",
        "HOW_TO_RUN_BASELINES.md",
        "COMPARISON_LIMITATIONS.md",
        "rsync_baseline_result.json",
        "rsync_baseline_summary.csv",
        "rsync_preflight.json",
        "RSYNC_BASELINE_REPORT.md",
        "zsync_baseline_result.json",
        "zsync_baseline_summary.csv",
        "zsync_preflight.json",
        "ZSYNC_BASELINE_REPORT.md",
        "signed_url_pilot_result.json",
        "signed_url_pilot_report.md",
        "signed_url_pilot_summary.csv",
        "signed_url_policy.json",
    }
    allowed_exact = {x.lower() for x in allowed_exact}
    if name in allowed_exact:
        return True
    if name.endswith("_metadata.csv") or name.endswith("_input_metadata.csv"):
        return True
    if name.endswith("_cost_aware_result.json") or name.endswith("_cost_aware_summary.csv"):
        return True
    if name.startswith("cld2_alpha") and name.endswith(("analysis.md", "report.md", "summary.json")):
        return True
    if name in {"readme.md", "index.md", "source_note.txt"}:
        return True
    return False


def make_review_zip(src_dir: str | Path, zip_out: str | Path, *, max_mb: float = 10.0) -> dict:
    """Create a compact review ZIP, excluding repo internals, packs and duplicated low-value metadata."""
    src = Path(src_dir)
    out = Path(zip_out)
    max_bytes = int(float(max_mb) * 1024 * 1024)
    files: list[Path] = []
    skipped_large: list[dict] = []
    skipped_nonreview = 0
    warnings: list[str] = []
    if not src.exists():
        raise FileNotFoundError(src)
    for p in src.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(src)
        if not _is_review_zip_candidate(rel):
            skipped_nonreview += 1
            continue
        size = p.stat().st_size
        if size > max_bytes:
            skipped_large.append({"path": rel.as_posix(), "bytes": size})
            continue
        files.append(p)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    if not files:
        warnings.append("review zip contains no files")
        return {
            "ok": False,
            "schema": "CoreLangDistribution/ReviewZip",
            "version": "2.0.0-alpha50.2",
            "src_dir": str(src),
            "zip_out": str(out),
            "included_files": 0,
            "excluded_files": skipped_nonreview + len(skipped_large),
            "files_included": 0,
            "skipped_large": skipped_large,
            "skipped_nonreview_files": skipped_nonreview,
            "warnings": warnings,
            "bytes": 0,
            "policy": "high-value JSON/CSV/Markdown reports only; excludes .cldrepo internals, packs, chunks.idx.json, files.idx.json and release.json",
        }
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        manifest_rows = ["path,bytes,sha256"]
        for p in sorted(files):
            rel = p.relative_to(src).as_posix()
            zf.write(p, rel)
            manifest_rows.append(f"{rel},{p.stat().st_size},{sha256_file(p)}")
        zf.writestr("REVIEW_FILE_MANIFEST.csv", "\n".join(manifest_rows) + "\n")
    manifest = {
        "schema": "CoreLangDistribution/ReviewZip",
        "version": "2.0.0-alpha50.2",
        "src_dir": str(src),
        "zip_out": str(out),
        "included_files": len(files) + 1,
        "excluded_files": skipped_nonreview + len(skipped_large),
        "files_included": len(files),
        "skipped_large": skipped_large,
        "skipped_nonreview_files": skipped_nonreview,
        "warnings": warnings,
        "bytes": out.stat().st_size if out.exists() else 0,
        "policy": "high-value JSON/CSV/Markdown reports only; excludes .cldrepo internals, packs, chunks.idx.json, files.idx.json and release.json",
    }
    return {"ok": True, **manifest}

def cleanup_heavy_artifacts(path: str | Path, *, min_mb: float = 100.0, apply: bool = False) -> dict:
    """Dry-run by default: list heavy generated files, optionally remove them."""
    root = Path(path)
    if not root.exists():
        raise FileNotFoundError(root)
    min_bytes = int(float(min_mb) * 1024 * 1024)
    keep_ext = {".json", ".csv", ".md", ".txt", ".log", ".sha256"}
    matches = []
    removed_bytes = 0
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        size = p.stat().st_size
        if size < min_bytes or p.suffix.lower() in keep_ext:
            continue
        matches.append({"path": str(p), "bytes": size})
        if apply:
            removed_bytes += size
            p.unlink()
    return {
        "ok": True,
        "schema": "CoreLangDistribution/CleanupHeavyArtifacts",
        "version": "2.0.0-alpha50.2",
        "path": str(root),
        "dry_run": not apply,
        "min_mb": min_mb,
        "matched_files": len(matches),
        "matched_bytes": sum(x["bytes"] for x in matches),
        "removed_bytes": removed_bytes,
        "files": matches[:200],
        "truncated": len(matches) > 200,
    }


def run_cost_aware_planner_bench(
    old_dir: str | Path,
    new_dir: str | Path,
    out_dir: str | Path,
    *,
    profiles: str | list[str] = "large-file-small,large-file-balanced,large-file-large",
    codecs: str | list[str] = "raw,zstd",
    scenario_name: str = "cost-aware-planner",
    scenario_kind: str | None = None,
    scenario_note: str | None = None,
    download_count: int = 1000,
    cost_per_gb: float = 0.05,
    currency: str = "USD",
    pack_cost_per_hour: float = 25.0,
    file_level_tar_zstd_bytes: int | None = None,
    full_tar_zstd_v2_bytes: int | None = None,
    rsync_bytes: int | None = None,
    zsync_bytes: int | None = None,
    casync_bytes: int | None = None,
    external_baseline_note: str | None = None,
    byte_tolerance_ratio: float = 0.05,
    download_tolerance_ratio: float = 0.10,
    download_tolerance_bytes: int = 256 * 1024 * 1024,
) -> dict:
    out = Path(out_dir)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    hybrid_dir = out / "hybrid"
    hybrid_summary = run_hybrid_planner_bench(
        old_dir,
        new_dir,
        hybrid_dir,
        profiles=profiles,
        codecs=codecs,
        scenario_name=scenario_name,
        scenario_kind=scenario_kind,
        scenario_note=scenario_note,
        cost_per_gb=cost_per_gb,
        download_count=download_count,
        currency=currency,
        file_level_tar_zstd_bytes=file_level_tar_zstd_bytes,
        full_tar_zstd_v2_bytes=full_tar_zstd_v2_bytes,
        rsync_bytes=rsync_bytes,
        zsync_bytes=zsync_bytes,
        casync_bytes=casync_bytes,
        external_baseline_note=external_baseline_note,
        download_tolerance_ratio=download_tolerance_ratio,
        download_tolerance_bytes=download_tolerance_bytes,
    )
    hybrid_result_path = hybrid_dir / "hybrid_planner_result.json"
    hybrid_result = json.loads(hybrid_result_path.read_text(encoding="utf-8"))

    enriched = _alpha28_enrich_rows_for_cost(
        hybrid_result.get("comparison_rows", []),
        download_count=download_count,
        cost_per_gb=cost_per_gb,
        pack_cost_per_hour=pack_cost_per_hour,
    )
    recommendations = _alpha28_recommend_cost_aware(enriched, byte_tolerance_ratio=byte_tolerance_ratio)
    break_even = _alpha29_compute_break_even(enriched, recommendations)

    csv_path = out / "cost_aware_planner_summary.csv"
    if enriched:
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(enriched[0].keys()))
            w.writeheader()
            w.writerows(enriched)

    result = {
        "schema": "CoreLangDistribution/CostAwarePlanner",
        "version": "2.0.0-alpha50.2",
        "ok": bool(hybrid_summary.get("ok")),
        "scenario": scenario_name,
        "scenario_kind": scenario_kind,
        "scenario_note": scenario_note,
        "download_count": download_count,
        "cost_per_gb": cost_per_gb,
        "currency": currency,
        "pack_cost_per_hour": pack_cost_per_hour,
        "byte_tolerance_ratio": byte_tolerance_ratio,
        "hybrid_summary": hybrid_summary,
        "cost_rows": enriched,
        "cost_aware_recommendations": recommendations,
        "break_even": break_even,
        "reports": {
            "json": str(out / "cost_aware_planner_result.json"),
            "csv": str(csv_path),
            "markdown": str(out / "cost_aware_planner_report.md"),
            "hybrid_json": str(hybrid_result_path),
            "hybrid_report": str(hybrid_dir / "hybrid_planner_report.md"),
        },
    }
    (out / "cost_aware_planner_result.json").write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    _alpha28_write_cost_report(result, out / "cost_aware_planner_report.md")
    return {
        "ok": result["ok"],
        "schema": result["schema"],
        "version": result["version"],
        "scenario": scenario_name,
        "download_count": download_count,
        "cost_per_gb": cost_per_gb,
        "pack_cost_per_hour": pack_cost_per_hour,
        "recommendations": recommendations,
        "break_even": break_even,
        "reports": result["reports"],
    }


def run_cost_aware_scenarios_bench(
    old_dir: str | Path,
    new_dir: str | Path,
    out_dir: str | Path,
    *,
    profiles: str | list[str] = "large-file-small,large-file-balanced,large-file-large",
    codecs: str | list[str] = "raw,zstd",
    scenario_name: str = "cost-aware-scenarios",
    scenario_kind: str | None = None,
    scenario_note: str | None = None,
    internal_download_count: int = 10,
    public_download_count: int = 10000,
    massive_download_count: int = 100000,
    cost_per_gb: float = 0.05,
    currency: str = "USD",
    pack_cost_per_hour: float = 25.0,
    file_level_tar_zstd_bytes: int | None = None,
    full_tar_zstd_v2_bytes: int | None = None,
    rsync_bytes: int | None = None,
    zsync_bytes: int | None = None,
    casync_bytes: int | None = None,
    external_baseline_note: str | None = None,
    byte_tolerance_ratio: float = 0.05,
    download_tolerance_ratio: float = 0.10,
    download_tolerance_bytes: int = 256 * 1024 * 1024,
    make_zip: bool = False,
    make_review: bool = False,
    light_zip_max_mb: float = 50.0,
) -> dict:
    out = Path(out_dir)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    hybrid_dir = out / "hybrid"
    hybrid_summary = run_hybrid_planner_bench(
        old_dir,
        new_dir,
        hybrid_dir,
        profiles=profiles,
        codecs=codecs,
        scenario_name=scenario_name,
        scenario_kind=scenario_kind,
        scenario_note=scenario_note,
        cost_per_gb=cost_per_gb,
        download_count=public_download_count,
        currency=currency,
        file_level_tar_zstd_bytes=file_level_tar_zstd_bytes,
        full_tar_zstd_v2_bytes=full_tar_zstd_v2_bytes,
        rsync_bytes=rsync_bytes,
        zsync_bytes=zsync_bytes,
        casync_bytes=casync_bytes,
        external_baseline_note=external_baseline_note,
        download_tolerance_ratio=download_tolerance_ratio,
        download_tolerance_bytes=download_tolerance_bytes,
    )
    hybrid_result_path = hybrid_dir / "hybrid_planner_result.json"
    hybrid_result = json.loads(hybrid_result_path.read_text(encoding="utf-8"))
    base_rows = hybrid_result.get("comparison_rows", [])

    scenario_counts = {
        "internal": int(internal_download_count),
        "public": int(public_download_count),
        "massive": int(massive_download_count),
    }
    scenarios: dict[str, dict] = {}
    scenarios_dir = out / "scenarios"
    scenarios_dir.mkdir(parents=True, exist_ok=True)
    for name, count in scenario_counts.items():
        rows = _alpha28_enrich_rows_for_cost(
            base_rows,
            download_count=count,
            cost_per_gb=cost_per_gb,
            pack_cost_per_hour=pack_cost_per_hour,
        )
        recs = _alpha28_recommend_cost_aware(rows, byte_tolerance_ratio=byte_tolerance_ratio)
        be = _alpha29_compute_break_even(rows, recs)
        sc = {
            "schema": "CoreLangDistribution/CostAwareScenario",
            "version": "2.0.0-alpha50.2",
            "scenario": name,
            "download_count": count,
            "cost_rows": rows,
            "cost_aware_recommendations": recs,
            "break_even": be,
        }
        scenarios[name] = sc
        (scenarios_dir / f"{name}_cost_aware_result.json").write_text(json.dumps(sc, indent=2, sort_keys=True), encoding="utf-8")
        _alpha29_write_cost_csv(rows, scenarios_dir / f"{name}_cost_aware_summary.csv")

    result = {
        "schema": "CoreLangDistribution/CostAwareScenarios",
        "version": "2.0.0-alpha50.2",
        "ok": bool(hybrid_summary.get("ok")),
        "scenario": scenario_name,
        "scenario_kind": scenario_kind,
        "scenario_note": scenario_note,
        "download_counts": scenario_counts,
        "cost_per_gb": cost_per_gb,
        "currency": currency,
        "pack_cost_per_hour": pack_cost_per_hour,
        "byte_tolerance_ratio": byte_tolerance_ratio,
        "hybrid_summary": hybrid_summary,
        "scenarios": scenarios,
        "reports": {
            "json": str(out / "cost_aware_scenarios_result.json"),
            "markdown": str(out / "cost_aware_scenarios_report.md"),
            "hybrid_json": str(hybrid_result_path),
            "hybrid_report": str(hybrid_dir / "hybrid_planner_report.md"),
            "scenarios_dir": str(scenarios_dir),
        },
    }
    (out / "cost_aware_scenarios_result.json").write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    _alpha29_write_scenario_report(result, out / "cost_aware_scenarios_report.md")
    if make_zip:
        zip_path = out.with_name(out.name + "_LIGHT.zip")
        result["reports"]["light_zip"] = make_light_zip(out, zip_path, max_mb=light_zip_max_mb)["zip_out"]
    if make_review:
        zip_path = out.with_name(out.name + "_REVIEW.zip")
        result["reports"]["review_zip"] = make_review_zip(out, zip_path, max_mb=light_zip_max_mb)["zip_out"]
    if make_zip or make_review:
        (out / "cost_aware_scenarios_result.json").write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    return {
        "ok": result["ok"],
        "schema": result["schema"],
        "version": result["version"],
        "scenario": scenario_name,
        "download_counts": scenario_counts,
        "recommendations": {
            name: sc.get("cost_aware_recommendations", {}) for name, sc in scenarios.items()
        },
        "break_even": next(iter(scenarios.values()), {}).get("break_even", {}) if scenarios else {},
        "reports": result["reports"],
    }

# ---- alpha31 corpus benchmark / falsification matrix ----

def _alpha31_parse_corpus_list(value: str | list[str]) -> list[str]:
    if isinstance(value, str):
        return [x.strip() for x in value.split(",") if x.strip()]
    return [str(x).strip() for x in value if str(x).strip()]


def _alpha31_write_repeated(path: Path, token: bytes, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        written = 0
        while written < size:
            take = min(len(token), size - written)
            f.write(token[:take])
            written += take


def _alpha31_make_corpus_pair(pair_root: Path, *, scenario: str, file_size_bytes: int, small_file_count: int = 64, seed: int = 3100) -> tuple[Path, Path, str, str]:
    """Create a deterministic synthetic v1/v2 pair for alpha31 corpus tests."""
    if pair_root.exists():
        shutil.rmtree(pair_root)
    v1 = pair_root / "release_v1"
    v2 = pair_root / "release_v2"
    v1.mkdir(parents=True)
    v2.mkdir(parents=True)
    rnd = random.Random(seed + len(scenario))

    if scenario == "localized-large":
        base = _alpha25_make_structured_bytes(file_size_bytes, seed=seed)
        (v1 / "dataset").mkdir(parents=True, exist_ok=True)
        (v1 / "dataset" / "catalog.bin").write_bytes(base)
        changed = _alpha25_apply_change(base, mode="localized", ratio=0.01, seed=seed + 1)
        (v2 / "dataset" / "catalog.bin").parent.mkdir(parents=True, exist_ok=True)
        (v2 / "dataset" / "catalog.bin").write_bytes(changed)
        return v1, v2, "Favorable: one large structured file with a small localized change.", "favorable-localized"

    if scenario == "distributed-large":
        base = _alpha25_make_structured_bytes(file_size_bytes, seed=seed + 2)
        (v1 / "dataset").mkdir(parents=True, exist_ok=True)
        (v1 / "dataset" / "catalog.bin").write_bytes(base)
        changed = _alpha25_apply_change(base, mode="distributed", ratio=0.10, seed=seed + 3)
        (v2 / "dataset" / "catalog.bin").parent.mkdir(parents=True, exist_ok=True)
        (v2 / "dataset" / "catalog.bin").write_bytes(changed)
        return v1, v2, "Adversarial-ish: one large structured file with distributed 10% changes.", "adversarial-distributed"

    if scenario == "append-only-log":
        line = b"2026-06-02T00:00:00Z INFO sensor=astro shard=000 event=repeated payload=CLD2_ALPHA31\n"
        _alpha31_write_repeated(v1 / "logs" / "events.log", line, file_size_bytes)
        shutil.copytree(v1, v2, dirs_exist_ok=True)
        with (v2 / "logs" / "events.log").open("ab") as f:
            f.write(line.replace(b"000", b"999") * max(1, file_size_bytes // (len(line) * 20)))
        return v1, v2, "Favorable: append-only log update with mostly old content preserved.", "favorable-append"

    if scenario == "many-small-files":
        count = max(4, int(small_file_count))
        per = max(1024, file_size_bytes // count)
        for i in range(count):
            token = f"CLD2_ALPHA31_SMALL_FILE_{i:04d}\n".encode() + rnd.randbytes(96)
            _alpha31_write_repeated(v1 / "small" / f"file_{i:04d}.dat", token, per)
        shutil.copytree(v1, v2, dirs_exist_ok=True)
        # Modify about 20% of files and add a few new ones.
        for i in range(0, count, max(1, count // 8)):
            p = v2 / "small" / f"file_{i:04d}.dat"
            data = bytearray(p.read_bytes())
            start = min(len(data) - 1, max(0, len(data) // 3))
            data[start:start + min(256, len(data) - start)] = rnd.randbytes(min(256, len(data) - start))
            p.write_bytes(bytes(data))
        for j in range(max(1, count // 16)):
            _alpha31_write_repeated(v2 / "small" / f"new_{j:04d}.dat", b"CLD2_ALPHA31_NEW_FILE\n" + rnd.randbytes(64), per)
        return v1, v2, "Mixed: many small files with some modified files and a few additions.", "mixed-many-small"

    if scenario == "high-entropy-rewrite":
        (v1 / "random").mkdir(parents=True, exist_ok=True)
        (v2 / "random").mkdir(parents=True, exist_ok=True)
        (v1 / "random" / "blob.bin").write_bytes(rnd.randbytes(file_size_bytes))
        (v2 / "random" / "blob.bin").write_bytes(random.Random(seed + 999).randbytes(file_size_bytes))
        return v1, v2, "Adversarial: high-entropy file fully rewritten; little/no chunk reuse expected.", "adversarial-entropy"

    if scenario == "rename-move":
        count = max(4, min(int(small_file_count), 32))
        per = max(2048, file_size_bytes // count)
        for i in range(count):
            token = f"ASSET-STABLE-CONTENT-{i:04d}\n".encode() + _alpha25_make_structured_bytes(256, seed=seed + i)
            _alpha31_write_repeated(v1 / "assets_v1" / f"asset_{i:04d}.bin", token, per)
        (v2 / "assets_v2" / "renamed").mkdir(parents=True, exist_ok=True)
        for i in range(count):
            old = v1 / "assets_v1" / f"asset_{i:04d}.bin"
            new = v2 / "assets_v2" / "renamed" / f"asset_{i:04d}_moved.bin"
            new.write_bytes(old.read_bytes())
        return v1, v2, "Mixed/favorable for content-addressed chunking: files moved/renamed but content stable.", "mixed-rename-move"

    raise ValueError(f"unknown alpha31 corpus scenario {scenario}")


def _alpha31_scenario_verdict(result: dict) -> dict:
    public = result.get("scenarios", {}).get("public", {})
    recs = public.get("cost_aware_recommendations", {})
    default_rec = recs.get("cost_aware_default") or {}
    cld2_rec = recs.get("cost_aware_cld2") or {}
    rows = public.get("cost_rows", [])
    external_rows = [r for r in rows if r.get("category") != "cld2" and (r.get("total_cost") is not None)]
    best_external = min(external_rows, key=lambda r: r.get("total_cost") or float("inf")) if external_rows else None
    tar_row = None
    for row in rows:
        if row.get("method") == "file-level-tar.zst-update":
            tar_row = row
            break
    ratio = None
    label = "inconclusive"
    reason = "missing external baseline or CLD2-only recommendation"
    if best_external and cld2_rec:
        ext_cost = best_external.get("total_cost") or 0
        cld_cost = cld2_rec.get("total_cost") or 0
        if ext_cost and cld_cost:
            ratio = cld_cost / ext_cost
            if ratio < 0.80:
                label = "win"
                reason = "best CLD2 plan is at least 20% cheaper than the best available external/file-level baseline in public scenario"
            elif ratio > 1.20:
                label = "loss"
                reason = "best CLD2 plan is at least 20% more expensive than the best available external/file-level baseline in public scenario"
            else:
                label = "near_tie"
                reason = "best CLD2 plan and best available external/file-level baseline are within ±20% in public scenario"
    return {
        "label": label,
        "reason": reason,
        "public_default_plan": default_rec.get("plan"),
        "public_default_category": default_rec.get("category"),
        "public_default_bytes": default_rec.get("bytes"),
        "public_default_total_cost": default_rec.get("total_cost"),
        "best_cld2_plan": cld2_rec.get("plan"),
        "best_cld2_bytes": cld2_rec.get("bytes"),
        "best_cld2_total_cost": cld2_rec.get("total_cost"),
        "best_external_plan": f"external:{best_external.get('method')}" if best_external else None,
        "best_external_bytes": best_external.get("bytes") if best_external else None,
        "best_external_total_cost": best_external.get("total_cost") if best_external else None,
        "tar_zst_update_bytes": tar_row.get("bytes") if tar_row else None,
        "tar_zst_update_total_cost": tar_row.get("total_cost") if tar_row else None,
        "cost_ratio_cld2_vs_best_external": ratio,
    }


def run_corpus_bench(
    out_dir: str | Path,
    *,
    corpus: str | list[str] = "localized-large,distributed-large,append-only-log,many-small-files,high-entropy-rewrite,rename-move",
    file_size: str = "4MiB",
    small_file_count: int = 64,
    profiles: str | list[str] = "large-file-small,large-file-balanced,large-file-large",
    codecs: str | list[str] = "raw,zstd",
    internal_download_count: int = 10,
    public_download_count: int = 10000,
    massive_download_count: int = 100000,
    cost_per_gb: float = 0.05,
    currency: str = "USD",
    pack_cost_per_hour: float = 25.0,
    generate_tar_zst: bool = True,
    keep_heavy: bool = False,
    make_review: bool = False,
    review_zip_max_mb: float = 25.0,
) -> dict:
    """Run alpha31 synthetic corpus tests and summarize wins/losses against tar.zst update."""
    out = Path(out_dir)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    scenarios = _alpha31_parse_corpus_list(corpus)
    size_bytes = parse_size(file_size)
    scenario_results: list[dict] = []
    rows: list[dict] = []

    for idx, scenario in enumerate(scenarios):
        pair_root = out / "pairs" / scenario
        v1, v2, note, kind = _alpha31_make_corpus_pair(pair_root, scenario=scenario, file_size_bytes=size_bytes, small_file_count=small_file_count, seed=3100 + idx * 100)
        tar_update = None
        tar_full = None
        baseline_note = "tar.zst baseline not generated; pass --no-generate-tar-zst only for faster raw-baseline smoke tests"
        if generate_tar_zst:
            tar_update, tar_full, baseline_note = _alpha25_try_make_tar_zst_baselines(v1, v2, out / "tar_zst" / scenario)
        scenario_dir = out / "scenarios" / scenario
        res = run_cost_aware_scenarios_bench(
            v1,
            v2,
            scenario_dir,
            profiles=profiles,
            codecs=codecs,
            scenario_name=f"alpha31-corpus-{scenario}",
            scenario_kind=kind,
            scenario_note=note,
            internal_download_count=internal_download_count,
            public_download_count=public_download_count,
            massive_download_count=massive_download_count,
            cost_per_gb=cost_per_gb,
            currency=currency,
            pack_cost_per_hour=pack_cost_per_hour,
            file_level_tar_zstd_bytes=tar_update,
            full_tar_zstd_v2_bytes=tar_full,
            external_baseline_note=baseline_note,
            make_zip=False,
            make_review=False,
        )
        full_result = json.loads((scenario_dir / "cost_aware_scenarios_result.json").read_text(encoding="utf-8"))
        verdict = _alpha31_scenario_verdict(full_result)
        item = {
            "scenario": scenario,
            "scenario_kind": kind,
            "scenario_note": note,
            "ok": bool(res.get("ok")),
            "file_level_tar_zstd_bytes": tar_update,
            "full_tar_zstd_v2_bytes": tar_full,
            "tar_zst_baseline_note": baseline_note,
            "verdict": verdict,
            "reports": {
                "json": str(scenario_dir / "cost_aware_scenarios_result.json"),
                "markdown": str(scenario_dir / "cost_aware_scenarios_report.md"),
                "review_zip_candidate_dir": str(scenario_dir),
            },
        }
        scenario_results.append(item)
        rows.append({
            "scenario": scenario,
            "scenario_kind": kind,
            "verdict": verdict.get("label"),
            "public_default_plan": verdict.get("public_default_plan"),
            "public_default_bytes": verdict.get("public_default_bytes"),
            "public_default_total_cost": verdict.get("public_default_total_cost"),
            "best_cld2_plan": verdict.get("best_cld2_plan"),
            "best_cld2_bytes": verdict.get("best_cld2_bytes"),
            "best_cld2_total_cost": verdict.get("best_cld2_total_cost"),
            "best_external_plan": verdict.get("best_external_plan"),
            "best_external_bytes": verdict.get("best_external_bytes"),
            "best_external_total_cost": verdict.get("best_external_total_cost"),
            "tar_zst_update_bytes": verdict.get("tar_zst_update_bytes"),
            "tar_zst_update_total_cost": verdict.get("tar_zst_update_total_cost"),
            "tar_zst_baseline_note": baseline_note,
            "cost_ratio_cld2_vs_best_external": verdict.get("cost_ratio_cld2_vs_best_external"),
            "reason": verdict.get("reason"),
        })
        if not keep_heavy:
            shutil.rmtree(pair_root, ignore_errors=True)
            # Remove generated repo internals/packs but keep reports for review.
            cleanup_heavy_artifacts(scenario_dir, min_mb=0.01, apply=True)
            for p in scenario_dir.rglob("*.cldrepo"):
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
            for dname in ("packs", "cache", "install"):
                for p in scenario_dir.rglob(dname):
                    if p.is_dir():
                        shutil.rmtree(p, ignore_errors=True)

    csv_path = out / "corpus_benchmark_summary.csv"
    if rows:
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

    wins = sum(1 for r in rows if r.get("verdict") == "win")
    losses = sum(1 for r in rows if r.get("verdict") == "loss")
    ties = sum(1 for r in rows if r.get("verdict") == "near_tie")
    inconclusive = sum(1 for r in rows if r.get("verdict") == "inconclusive")
    result = {
        "schema": "CoreLangDistribution/CorpusBenchmark",
        "version": "2.0.0-alpha50.2",
        "ok": all(x.get("ok") for x in scenario_results),
        "purpose": "falsification-oriented corpus: identify where CLD2 wins, loses or ties versus tar.zst/file-level baselines under cost-aware scenarios",
        "file_size": file_size,
        "small_file_count": small_file_count,
        "profiles": _parse_profile_list(profiles),
        "codecs": _alpha25_parse_string_list(codecs),
        "download_counts": {
            "internal": internal_download_count,
            "public": public_download_count,
            "massive": massive_download_count,
        },
        "cost_per_gb": cost_per_gb,
        "currency": currency,
        "pack_cost_per_hour": pack_cost_per_hour,
        "generate_tar_zst": generate_tar_zst,
        "keep_heavy": keep_heavy,
        "summary": {
            "scenario_count": len(scenario_results),
            "wins": wins,
            "losses": losses,
            "near_ties": ties,
            "inconclusive": inconclusive,
        },
        "scenarios": scenario_results,
        "reports": {
            "json": str(out / "corpus_benchmark_result.json"),
            "csv": str(csv_path),
            "markdown": str(out / "corpus_benchmark_report.md"),
        },
    }
    (out / "corpus_benchmark_result.json").write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")

    lines = [
        "# CLD2 alpha31.1 corpus benchmark / falsification matrix",
        "",
        "This report is intentionally not only a success benchmark. It is designed to show where CLD2 wins, loses or ties. Alpha31.1 treats 0-byte CLD2 updates as valid and generates tar.zst baselines by default.",
        "",
        "## Summary",
        "",
        f"- scenarios: {len(scenario_results)}",
        f"- wins: {wins}",
        f"- losses: {losses}",
        f"- near ties: {ties}",
        f"- inconclusive: {inconclusive}",
        "",
        "## Scenario results",
        "",
        "| Scenario | Kind | Verdict | Public default | Best CLD2 | Best external | tar.zst baseline | Ratio | Reason |",
        "|---|---|---|---|---:|---:|---|---:|---|",
    ]
    for row in rows:
        ratio = row.get("cost_ratio_cld2_vs_best_external")
        ratio_s = "" if ratio is None else f"{ratio:.4f}"
        tar_s = row.get("tar_zst_update_bytes")
        tar_note = row.get("tar_zst_baseline_note") or ""
        tar_cell = str(tar_s) if tar_s is not None else tar_note
        lines.append(
            f"| {row.get('scenario')} | {row.get('scenario_kind')} | {row.get('verdict')} | `{row.get('public_default_plan')}` | `{row.get('best_cld2_plan')}` / {row.get('best_cld2_total_cost')} | `{row.get('best_external_plan')}` / {row.get('best_external_total_cost')} | {tar_cell} | {ratio_s} | {row.get('reason')} |"
        )
    lines += [
        "",
        "## Interpretation guardrail",
        "",
        "A win on localized or append-only scenarios is expected and useful.",
        "A loss or near-tie on high-entropy/distributed scenarios is not a bug by itself: it defines the boundary where CLD2 should fall back or avoid overclaiming.",
    ]
    (out / "corpus_benchmark_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    if make_review:
        zip_path = out.with_name(out.name + "_REVIEW.zip")
        result["reports"]["review_zip"] = make_review_zip(out, zip_path, max_mb=review_zip_max_mb)["zip_out"]
        (out / "corpus_benchmark_result.json").write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")

    return {
        "ok": result["ok"],
        "schema": result["schema"],
        "version": result["version"],
        "summary": result["summary"],
        "reports": result["reports"],
    }

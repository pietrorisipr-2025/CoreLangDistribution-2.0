#!/usr/bin/env python3
"""Generate public CLD2 alpha55 example artifacts.

Creates v1/v2 directories for three showcase scenarios:
- game_asset_patch
- model_profile_pack
- ci_artifact_bundle
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import shutil
from pathlib import Path


def write_bytes(path: Path, size: int, *, seed: int, pattern: bytes | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    remaining = size
    with path.open("wb") as f:
        if pattern:
            while remaining > 0:
                chunk = pattern[: min(len(pattern), remaining)]
                f.write(chunk)
                remaining -= len(chunk)
        else:
            while remaining > 0:
                n = min(1024 * 1024, remaining)
                f.write(rng.randbytes(n))
                remaining -= n


def patch_file(path: Path, *, offset_ratio: float, patch: bytes) -> None:
    data = bytearray(path.read_bytes())
    if not data:
        path.write_bytes(patch)
        return
    start = max(0, min(len(data) - 1, int(len(data) * offset_ratio)))
    end = min(len(data), start + len(patch))
    data[start:end] = patch[: end - start]
    path.write_bytes(data)


def tree_meta(root: Path) -> dict:
    files = []
    total = 0
    for p in sorted(root.rglob("*")):
        if p.is_file():
            data = p.read_bytes()
            files.append({
                "path": p.relative_to(root).as_posix(),
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            })
            total += len(data)
    return {"files": len(files), "bytes": total, "file_list": files}


def make_game_asset_patch(root: Path, size_mib: int) -> None:
    case = root / "game_asset_patch"
    if case.exists():
        shutil.rmtree(case)
    v1 = case / "v1"
    v2 = case / "v2"
    v1.mkdir(parents=True)

    target = size_mib * 1024 * 1024
    large_assets = 16
    per_large = max(128 * 1024, int(target * 0.75) // large_assets)

    for i in range(large_assets):
        group = f"zone_{i % 4:02d}"
        pattern = (f"CLD2_ALPHA55_GAME_ASSET_{i:03d}\n".encode() * 2048)
        write_bytes(v1 / "assets" / group / f"texture_{i:03d}.pak", per_large, seed=1000 + i, pattern=pattern)

    for i in range(80):
        payload = (f"mesh-{i:03d}-stable\n".encode() * 256)
        write_bytes(v1 / "assets" / "meshes" / f"mesh_{i:03d}.bin", 32 * 1024, seed=2000 + i, pattern=payload)

    manifest = {
        "name": "alpha55 game asset patch",
        "version": 1,
        "assets": large_assets + 80,
    }
    (v1 / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (v1 / "README.txt").write_text("Alpha55 game asset patch v1\n", encoding="utf-8")

    shutil.copytree(v1, v2)

    # Localized changes in some large assets, plus a few added/changed small files.
    for i in [2, 7, 11]:
        p = v2 / "assets" / f"zone_{i % 4:02d}" / f"texture_{i:03d}.pak"
        patch_file(p, offset_ratio=0.45, patch=(b"ALPHA55_LOCALIZED_TEXTURE_PATCH" * 4096))

    for i in range(10):
        p = v2 / "assets" / "meshes" / f"mesh_{i:03d}.bin"
        patch_file(p, offset_ratio=0.2, patch=(f"patched-mesh-{i}\n".encode() * 128))

    for i in range(5):
        write_bytes(v2 / "assets" / "new" / f"dlc_asset_{i:03d}.bin", 96 * 1024, seed=3000 + i, pattern=(b"NEW_DLC_ASSET\n" * 256))

    manifest["version"] = 2
    manifest["note"] = "localized patch + new assets"
    (v2 / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (case / "CASE_DESCRIPTION.md").write_text(
        "# game_asset_patch\n\nGame-like asset tree with localized changes and added assets.\n",
        encoding="utf-8",
    )


def make_model_profile_pack(root: Path, size_mib: int) -> None:
    case = root / "model_profile_pack"
    if case.exists():
        shutil.rmtree(case)
    v1 = case / "v1"
    v2 = case / "v2"
    v1.mkdir(parents=True)

    target = size_mib * 1024 * 1024
    shards = 8
    per = max(512 * 1024, int(target * 0.90) // shards)

    # Mixed entropy: mostly deterministic random shards, but only localized changes in v2.
    for i in range(shards):
        write_bytes(v1 / "weights" / f"shard_{i:02d}.bin", per, seed=4000 + i, pattern=None)

    config = {
        "schema": "alpha55/model_profile_pack",
        "version": 1,
        "shards": shards,
        "notes": "synthetic model/profile pack",
    }
    (v1 / "profile.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    (v1 / "tokenizer.model").write_bytes((b"TOKENIZER_STABLE_BLOCK\n" * 4096))

    shutil.copytree(v1, v2)

    for i, ratio in [(1, 0.10), (4, 0.52), (6, 0.78)]:
        p = v2 / "weights" / f"shard_{i:02d}.bin"
        patch_file(p, offset_ratio=ratio, patch=random.Random(9000 + i).randbytes(192 * 1024))

    config["version"] = 2
    config["notes"] = "localized shard patch"
    (v2 / "profile.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    (case / "CASE_DESCRIPTION.md").write_text(
        "# model_profile_pack\n\nLarge shard-based package with localized binary changes.\n",
        encoding="utf-8",
    )


def make_ci_artifact_bundle(root: Path, size_mib: int) -> None:
    case = root / "ci_artifact_bundle"
    if case.exists():
        shutil.rmtree(case)
    v1 = case / "v1"
    v2 = case / "v2"
    v1.mkdir(parents=True)

    target = size_mib * 1024 * 1024
    bin_count = 10
    per_bin = max(256 * 1024, int(target * 0.65) // bin_count)

    for i in range(bin_count):
        pattern = (f"BINARY_ARTIFACT_{i:03d}_STABLE_SECTION\n".encode() * 2048)
        write_bytes(v1 / "dist" / f"component_{i:03d}.bin", per_bin, seed=6000 + i, pattern=pattern)

    for i in range(120):
        text = "\n".join([f"test_case_{i}_{j}: ok" for j in range(80)]) + "\n"
        (v1 / "reports" / f"test_report_{i:03d}.txt").parent.mkdir(parents=True, exist_ok=True)
        (v1 / "reports" / f"test_report_{i:03d}.txt").write_text(text, encoding="utf-8")

    (v1 / "build.json").write_text(json.dumps({"build": 1, "status": "green"}, indent=2) + "\n", encoding="utf-8")
    shutil.copytree(v1, v2)

    for i in [0, 3, 5, 9]:
        p = v2 / "dist" / f"component_{i:03d}.bin"
        patch_file(p, offset_ratio=0.35, patch=(b"CI_BUILD_PATCHED_REGION\n" * 8192))

    for i in range(20):
        text = "\n".join([f"test_case_{i}_{j}: ok patched" for j in range(80)]) + "\n"
        (v2 / "reports" / f"test_report_{i:03d}.txt").write_text(text, encoding="utf-8")

    for i in range(3):
        write_bytes(v2 / "dist" / "new" / f"new_plugin_{i:03d}.bin", 128 * 1024, seed=7000 + i, pattern=(b"NEW_PLUGIN_BINARY\n" * 1024))

    (v2 / "build.json").write_text(json.dumps({"build": 2, "status": "green", "note": "partial artifact update"}, indent=2) + "\n", encoding="utf-8")
    (case / "CASE_DESCRIPTION.md").write_text(
        "# ci_artifact_bundle\n\nCI/build output bundle with changed components and reports.\n",
        encoding="utf-8",
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--size-mib", type=int, default=64)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    out = Path(args.out_dir).resolve()
    if out.exists() and args.force:
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    make_game_asset_patch(out, args.size_mib)
    make_model_profile_pack(out, args.size_mib)
    make_ci_artifact_bundle(out, args.size_mib)

    summary = {
        "schema": "CLD2/alpha55_public_examples_generated",
        "size_mib": args.size_mib,
        "out_dir": str(out),
        "cases": {},
    }
    for case in ["game_asset_patch", "model_profile_pack", "ci_artifact_bundle"]:
        case_root = out / case
        summary["cases"][case] = {"v1": tree_meta(case_root / "v1"), "v2": tree_meta(case_root / "v2")}

    (out / "alpha55_generated_examples_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "out_dir": str(out), "cases": list(summary["cases"].keys())}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

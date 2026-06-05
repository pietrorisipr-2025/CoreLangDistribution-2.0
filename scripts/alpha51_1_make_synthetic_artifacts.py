#!/usr/bin/env python3
"""Generate less-misleading synthetic artifact pairs for CLD2 alpha51.1.

The original alpha51 synthetic artifacts were useful for pipeline validation but
some were extremely compressible. Alpha51.1 adds mixed/random profiles so that
compressed baselines are less trivially tiny.

Scenarios:

- domain_pack_mixed_high_reuse:
  many files, mostly deterministic pseudo-random payloads; small subset changed
  and a few files added. Tests file/object reuse.

- model_pack_localized_random:
  one large pseudo-random file copied from v1 to v2, then a localized patch is
  applied. Tests intra-file chunk reuse under low compressibility.

- many_small_mixed_update:
  many small files with mixed content; subset changed/added. Tests small-file
  metadata/object behavior.

- badfit_entropy_rewrite:
  large high-entropy blob regenerated from scratch. Should be near parity/bad-fit.
"""
from __future__ import annotations

import argparse
import json
import random
import shutil
from pathlib import Path


def fill_file(path: Path, size: int, seed: int = 1, pattern: bytes | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    remaining = int(size)
    rnd = random.Random(seed)
    with path.open("wb") as f:
        if pattern is not None:
            while remaining > 0:
                chunk = pattern[: min(len(pattern), remaining)]
                f.write(chunk)
                remaining -= len(chunk)
        else:
            while remaining > 0:
                n = min(1024 * 1024, remaining)
                f.write(rnd.randbytes(n))
                remaining -= n


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def patch_file(path: Path, offset: int, data: bytes) -> None:
    with path.open("r+b") as f:
        f.seek(offset)
        f.write(data)


def domain_pack_mixed_high_reuse(root: Path, size_mib: int) -> None:
    base = root / "domain_pack_mixed_high_reuse"
    v1 = base / "v1"
    v2 = base / "v2"
    shutil.rmtree(base, ignore_errors=True)
    v1.mkdir(parents=True)

    file_count = 384
    total_size = size_mib * 1024 * 1024
    per = max(4096, total_size // file_count)

    for i in range(file_count):
        group = f"domain_{i % 32:02d}"
        name = f"entry_{i:05d}.bin"
        # 75% random-ish, 25% structured. This keeps compression realistic-ish
        # while preserving deterministic and reproducible content.
        if i % 4 == 0:
            pattern = (f"DOMAIN_ENTRY_{i:05d}_STRUCTURED_RECORD\n".encode() * 128)
            fill_file(v1 / group / name, per, pattern=pattern)
        else:
            fill_file(v1 / group / name, per, seed=10_000 + i)

    write_text(v1 / "manifest.json", json.dumps({"kind": "domain_pack_mixed", "version": 1, "files": file_count}, indent=2))
    shutil.copytree(v1, v2)

    # Modify about 8% of files with append/patches.
    changed = []
    for i in range(0, file_count, 12):
        p = v2 / f"domain_{i % 32:02d}" / f"entry_{i:05d}.bin"
        if i % 2 == 0:
            with p.open("ab") as f:
                f.write(random.Random(50_000 + i).randbytes(16 * 1024))
        else:
            patch_file(p, min(8192, max(0, p.stat().st_size // 2)), random.Random(60_000 + i).randbytes(16 * 1024))
        changed.append(p.relative_to(v2).as_posix())

    # Add a modest number of new files.
    for j in range(16):
        fill_file(v2 / "new_domain" / f"new_{j:05d}.bin", per, seed=70_000 + j)

    write_text(v2 / "manifest.json", json.dumps({"kind": "domain_pack_mixed", "version": 2, "changed": len(changed), "added": 16}, indent=2))


def model_pack_localized_random(root: Path, size_mib: int) -> None:
    base = root / "model_pack_localized_random"
    v1 = base / "v1"
    v2 = base / "v2"
    shutil.rmtree(base, ignore_errors=True)
    v1.mkdir(parents=True)

    size = size_mib * 1024 * 1024
    fill_file(v1 / "model.weights", size, seed=123456)
    write_text(v1 / "model.json", json.dumps({"kind": "model_pack_random", "version": 1, "bytes": size}, indent=2))
    shutil.copytree(v1, v2)

    # Localized patch of ~1 MiB in the middle; content remains high entropy.
    patch = random.Random(654321).randbytes(min(1024 * 1024, max(1, size // 32)))
    offset = max(0, size // 2 - len(patch) // 2)
    patch_file(v2 / "model.weights", offset, patch)
    write_text(v2 / "model.json", json.dumps({"kind": "model_pack_random", "version": 2, "localized_patch_bytes": len(patch)}, indent=2))


def many_small_mixed_update(root: Path, size_mib: int) -> None:
    base = root / "many_small_mixed_update"
    v1 = base / "v1"
    v2 = base / "v2"
    shutil.rmtree(base, ignore_errors=True)
    v1.mkdir(parents=True)

    file_count = 1024
    total_size = size_mib * 1024 * 1024
    per = max(1024, total_size // file_count)

    for i in range(file_count):
        group = f"shard_{i % 64:02d}"
        name = f"item_{i:05d}.dat"
        if i % 3 == 0:
            fill_file(v1 / group / name, per, pattern=(f"SMALL_STRUCTURED_{i:05d}\n".encode() * 64))
        else:
            fill_file(v1 / group / name, per, seed=200_000 + i)

    write_text(v1 / "index.json", json.dumps({"kind": "many_small_mixed", "version": 1, "files": file_count}, indent=2))
    shutil.copytree(v1, v2)

    # Change 10%, add 5%.
    for i in range(0, file_count, 10):
        p = v2 / f"shard_{i % 64:02d}" / f"item_{i:05d}.dat"
        if i % 20 == 0:
            with p.open("ab") as f:
                f.write(random.Random(300_000 + i).randbytes(2048))
        else:
            fill_file(p, per, seed=310_000 + i)

    for j in range(50):
        fill_file(v2 / "added" / f"added_{j:05d}.dat", per, seed=320_000 + j)

    write_text(v2 / "index.json", json.dumps({"kind": "many_small_mixed", "version": 2, "changed_about": 0.10, "added": 50}, indent=2))


def badfit_entropy_rewrite(root: Path, size_mib: int) -> None:
    base = root / "badfit_entropy_rewrite"
    v1 = base / "v1"
    v2 = base / "v2"
    shutil.rmtree(base, ignore_errors=True)
    v1.mkdir(parents=True)
    v2.mkdir(parents=True)
    size = size_mib * 1024 * 1024
    fill_file(v1 / "blob.bin", size, seed=400001)
    fill_file(v2 / "blob.bin", size, seed=400002)
    write_text(v1 / "meta.json", json.dumps({"kind": "badfit_entropy", "version": 1}, indent=2))
    write_text(v2 / "meta.json", json.dumps({"kind": "badfit_entropy", "version": 2}, indent=2))


def write_manifest(root: Path, size_mib: int) -> None:
    scenarios = []
    for scenario_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        v1 = scenario_dir / "v1"
        v2 = scenario_dir / "v2"
        scenarios.append({
            "name": scenario_dir.name,
            "v1": str(v1),
            "v2": str(v2),
            "v1_exists": v1.exists(),
            "v2_exists": v2.exists(),
        })
    write_text(root / "alpha51_1_synthetic_manifest.json", json.dumps({
        "schema": "CLD2/alpha51_1_synthetic_artifact_manifest",
        "size_mib": size_mib,
        "claim_boundary": "Synthetic artifacts are for harness testing and workload intuition, not production proof.",
        "scenarios": scenarios,
    }, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--size-mib", type=int, default=64)
    args = ap.parse_args()
    root = Path(args.out_dir)
    root.mkdir(parents=True, exist_ok=True)

    domain_pack_mixed_high_reuse(root, args.size_mib)
    model_pack_localized_random(root, args.size_mib)
    many_small_mixed_update(root, args.size_mib)
    badfit_entropy_rewrite(root, args.size_mib)
    write_manifest(root, args.size_mib)
    print(f"Synthetic alpha51.1 artifacts written to: {root}")


if __name__ == "__main__":
    main()

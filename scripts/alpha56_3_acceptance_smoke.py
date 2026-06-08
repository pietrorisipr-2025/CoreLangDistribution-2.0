#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from corelangdistribution2.bench import best_transfer_comparison, make_review_zip, run_real_bench
from corelangdistribution2.profiles import ProfileError, load_profile, profile_metadata, profile_pack_options
from corelangdistribution2.repo import make_repo


def assert_true(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


def expect_profile_error(data: dict, tmp: Path, name: str) -> None:
    path = tmp / f"{name}.json"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        load_profile(path)
    except ProfileError:
        return
    raise AssertionError(f"invalid profile did not fail: {name}")


def test_profiles(tmp: Path) -> None:
    fixed = load_profile(ROOT / "docs/profiles/amd-rdna-extracted-fixed-balanced.json")
    assert_true(fixed["name"] == "amd-rdna-extracted-fixed-balanced", "fixed profile name missing")
    load_profile(ROOT / "docs/profiles/libreoffice-extracted-fixed-balanced.json")
    load_profile(ROOT / "docs/profiles/single-exe-small-chunks-cdc.json")

    base = {
        "schema_version": 1,
        "name": "good",
        "description": "Good fixed profile.",
        "chunker": "fixed",
        "codec": "auto",
        "fixed_size": "1MiB",
    }
    expect_profile_error({k: v for k, v in base.items() if k != "name"}, tmp, "missing_name")
    expect_profile_error({**base, "schema_version": 2}, tmp, "bad_schema")
    expect_profile_error({**base, "chunker": "unknown"}, tmp, "bad_chunker")
    expect_profile_error({k: v for k, v in base.items() if k != "fixed_size"}, tmp, "missing_fixed_size")
    expect_profile_error(
        {
            "schema_version": 1,
            "name": "cdc-missing",
            "description": "Bad CDC profile.",
            "chunker": "fastcdc",
            "codec": "auto",
            "chunk_min": "16KiB",
        },
        tmp,
        "missing_cdc_sizes",
    )


def test_best_method() -> None:
    def row(method: str, bytes_count: int, seconds: float) -> dict:
        return {
            "chunker": method,
            "pack_v1_seconds": seconds / 2,
            "pack_v2_seconds": seconds / 2,
            "diff": {"download_required_pack_bytes": bytes_count},
        }

    assert_true(best_transfer_comparison([row("fixed", 10, 2), row("fastcdc", 12, 1)])["best_transfer_method"] == "fixed", "fixed lower bytes should win")
    assert_true(best_transfer_comparison([row("fixed", 12, 1), row("fastcdc", 10, 2)])["best_transfer_method"] == "fastcdc", "fastcdc lower bytes should win")
    assert_true(best_transfer_comparison([row("fixed", 10, 1), row("fastcdc", 10, 2)])["best_transfer_method"] == "fixed", "lower pack time should break ties")
    assert_true(best_transfer_comparison([row("zeta", 10, 1), row("alpha", 10, 1)])["best_transfer_method"] == "alpha", "method name should break stable ties")


def test_pack_and_bench_with_profile(tmp: Path) -> None:
    profile_path = ROOT / "docs/profiles/amd-rdna-extracted-fixed-balanced.json"
    profile = load_profile(profile_path)
    opts = profile_pack_options(profile)

    release = tmp / "release"
    release.mkdir()
    (release / "hello.txt").write_text("hello profile\n" * 20, encoding="utf-8")
    repo = tmp / "profile_pack.cldrepo"
    make_repo(
        release,
        repo,
        chunker=opts["chunker"],
        fixed_size=opts["fixed_size"],
        chunk_min=opts["chunk_min"],
        chunk_avg=opts["chunk_avg"],
        chunk_max=opts["chunk_max"],
        fastcdc_stride=int(opts["fastcdc_stride"]),
        codec=opts["codec"],
        force=True,
        profile_metadata=profile_metadata(profile, profile_path),
    )
    release_json = json.loads((repo / "release.json").read_text(encoding="utf-8"))
    assert_true(release_json["chunker"]["mode"] == "fixed", "profile pack did not apply fixed chunker")
    assert_true(release_json["codec_policy"] == "auto", "profile pack did not apply codec")
    assert_true(release_json["profile"]["name"] == profile["name"], "profile metadata missing from repo")

    old_dir = tmp / "old"
    new_dir = tmp / "new"
    old_dir.mkdir()
    new_dir.mkdir()
    (old_dir / "shared.txt").write_text("same\n" * 100, encoding="utf-8")
    (new_dir / "shared.txt").write_text("same\n" * 100 + "changed\n", encoding="utf-8")
    (new_dir / "new.txt").write_text("new\n", encoding="utf-8")
    out = tmp / "bench"
    result = run_real_bench(old_dir, new_dir, out, scenario_name="profile_smoke", profile_file=profile_path, profile_data=profile)
    assert_true(result["scenario_metadata"]["profile_name"] == profile["name"], "bench-real profile name not recorded")
    assert_true("Best transfer method" in (out / "bench_real_technical_report.md").read_text(encoding="utf-8"), "best method section missing")


def test_review_zip(tmp: Path) -> None:
    results = tmp / "tmp_results"
    (results / "fixed/release_v2.cldrepo/packs").mkdir(parents=True)
    (results / "cache").mkdir()
    (results / "install").mkdir()
    for name in [
        "bench_real_result.json",
        "bench_real_summary.csv",
        "bench_real_technical_report.md",
        "CLD2_savings_business_report.md",
        "amd_rdna_input_metadata.csv",
    ]:
        (results / name).write_text("review\n", encoding="utf-8")
    (results / "fixed/release_v2.cldrepo/release.json").write_text("{}", encoding="utf-8")
    (results / "fixed/release_v2.cldrepo/chunks.idx.json").write_text("[]", encoding="utf-8")
    (results / "fixed/release_v2.cldrepo/packs/pack.bin").write_bytes(b"internal")
    (results / "cache/object.bin").write_bytes(b"cache")
    (results / "install/file.bin").write_bytes(b"install")
    zip_out = tmp / "tmp_review.zip"
    report = make_review_zip(results, zip_out, max_mb=10)
    assert_true(report["ok"], "review zip failed")
    with zipfile.ZipFile(zip_out) as zf:
        names = set(zf.namelist())
    assert_true("bench_real_result.json" in names, "bench_real_result.json missing")
    assert_true("amd_rdna_input_metadata.csv" in names, "metadata CSV missing")
    assert_true("REVIEW_FILE_MANIFEST.csv" in names, "review manifest missing")
    assert_true(not any(".cldrepo" in n or n.startswith("cache/") or n.startswith("install/") for n in names), "internal artifacts leaked")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="cld2-alpha56-3-") as td:
        tmp = Path(td)
        test_profiles(tmp)
        test_best_method()
        test_pack_and_bench_with_profile(tmp)
        test_review_zip(tmp)
    print(json.dumps({"ok": True, "schema": "CLD2/alpha56.3_acceptance_smoke"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

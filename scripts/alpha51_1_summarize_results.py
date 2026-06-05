#!/usr/bin/env python3
"""Summarize CLD2 alpha51.1 artifact benchmark outputs.

This script scans an output root containing one or more scenario folders with
`bench_real_result.json` files produced by `cld2.py bench-real` and creates:

- alpha51_1_aggregate_summary.csv
- alpha51_1_aggregate_summary.json
- ALPHA51_1_AGGREGATE_REPORT.md

It is intentionally conservative: it compares the best CLD2 run not only with
raw file-level transfer, but also with the best conventional compressed
baseline available in the result.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
from typing import Any

BASELINE_KEYS = [
    "file_level_raw_download_bytes",
    "file_level_tar_zstd_bytes",
    "file_level_tar_gz_bytes",
    "full_tar_zstd_v2_bytes",
    "full_tar_gz_v2_bytes",
    "v2_logical_bytes",
]

CONVENTIONAL_COMPRESSED_KEYS = [
    "file_level_tar_zstd_bytes",
    "file_level_tar_gz_bytes",
    "full_tar_zstd_v2_bytes",
    "full_tar_gz_v2_bytes",
]


def safe_int(x: Any) -> int | None:
    try:
        if x is None:
            return None
        return int(x)
    except Exception:
        return None


def ratio(a: int | None, b: int | None) -> float | None:
    if a is None or b is None or b <= 0:
        return None
    return a / b


def pct_saved(a: int | None, b: int | None) -> float | None:
    # a = candidate bytes, b = baseline bytes
    r = ratio(a, b)
    if r is None:
        return None
    return 1.0 - r


def fmt_bytes(n: int | None) -> str:
    if n is None:
        return "n/a"
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    x = float(n)
    for u in units:
        if abs(x) < 1024.0 or u == units[-1]:
            if u == "B":
                return f"{int(x)} B"
            return f"{x:.2f} {u}"
        x /= 1024.0
    return str(n)


def fmt_ratio(x: float | None) -> str:
    if x is None or math.isnan(x):
        return "n/a"
    return f"{x:.6f}"


def best_cld2_run(data: dict[str, Any]) -> dict[str, Any]:
    candidates = []
    for r in data.get("runs", []) or []:
        d = r.get("diff", {}) or {}
        b = safe_int(d.get("download_required_pack_bytes"))
        raw = safe_int(d.get("download_required_raw_bytes"))
        candidates.append({
            "method": r.get("method_label") or r.get("chunker") or "unknown",
            "chunker": r.get("chunker"),
            "download_required_pack_bytes": b,
            "download_required_raw_bytes": raw,
            "chunk_reuse_ratio": d.get("chunk_reuse_ratio"),
            "pack_v1_seconds": r.get("pack_v1_seconds"),
            "pack_v2_seconds": r.get("pack_v2_seconds"),
        })
    candidates = [c for c in candidates if c["download_required_pack_bytes"] is not None]
    if not candidates:
        return {
            "method": None,
            "download_required_pack_bytes": None,
            "download_required_raw_bytes": None,
            "chunk_reuse_ratio": None,
            "all_runs": [],
        }
    best = min(candidates, key=lambda c: c["download_required_pack_bytes"])
    best = dict(best)
    best["all_runs"] = candidates
    return best


def best_baseline(baseline: dict[str, Any], keys: list[str]) -> tuple[str | None, int | None]:
    vals = []
    for k in keys:
        v = safe_int(baseline.get(k))
        if v is not None and v > 0:
            vals.append((k, v))
    if not vals:
        return None, None
    return min(vals, key=lambda kv: kv[1])


def classify(best_cld2: int | None, raw: int | None, best_conv: int | None, v2_logical: int | None) -> tuple[str, str]:
    if best_cld2 is None:
        return "invalid", "No CLD2 byte count found."

    raw_ratio = ratio(best_cld2, raw)
    conv_ratio = ratio(best_cld2, best_conv)
    full_ratio = ratio(best_cld2, v2_logical)

    if full_ratio is not None and full_ratio >= 0.90:
        return "bad-fit", "CLD2 transfers nearly the full artifact; little useful reuse."

    if raw_ratio is not None and raw_ratio >= 0.90:
        return "near-parity", "CLD2 is roughly equal to file-level/raw transfer."

    if conv_ratio is not None:
        if conv_ratio <= 0.25:
            return "high-reuse-win", "CLD2 is clearly below the best conventional compressed baseline."
        if conv_ratio <= 0.75:
            return "moderate-win", "CLD2 is meaningfully below the best conventional compressed baseline."
        if raw_ratio is not None and raw_ratio <= 0.25:
            return "compressed-baseline-parity", "CLD2 strongly beats raw transfer but is close to the best compressed baseline."
        return "near-parity", "CLD2 is close to the best compressed conventional baseline."

    if raw_ratio is not None:
        if raw_ratio <= 0.01:
            return "high-reuse-win", "CLD2 is far below raw transfer; no compressed baseline available."
        if raw_ratio <= 0.25:
            return "moderate-win", "CLD2 is below raw transfer; no compressed baseline available."

    return "unknown", "Insufficient baseline data for robust classification."


def summarize_one(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    scenario = data.get("scenario") or path.parent.name
    baseline = data.get("baseline", {}) or {}
    best = best_cld2_run(data)
    best_pack = best.get("download_required_pack_bytes")
    best_raw = best.get("download_required_raw_bytes")
    raw = safe_int(baseline.get("file_level_raw_download_bytes"))
    v2_logical = safe_int(baseline.get("v2_logical_bytes"))
    best_conv_key, best_conv_val = best_baseline(baseline, CONVENTIONAL_COMPRESSED_KEYS)
    best_any_key, best_any_val = best_baseline(baseline, BASELINE_KEYS)
    cls, reason = classify(best_pack, raw, best_conv_val, v2_logical)

    return {
        "scenario": scenario,
        "source_result": str(path),
        "profile": data.get("profile"),
        "codec": data.get("codec"),
        "version": data.get("version"),
        "best_cld2_method": best.get("method"),
        "best_cld2_pack_bytes": best_pack,
        "best_cld2_raw_bytes": best_raw,
        "file_level_raw_download_bytes": raw,
        "v2_logical_bytes": v2_logical,
        "best_conventional_baseline_name": best_conv_key,
        "best_conventional_baseline_bytes": best_conv_val,
        "best_any_baseline_name": best_any_key,
        "best_any_baseline_bytes": best_any_val,
        "cld2_vs_raw_ratio": ratio(best_pack, raw),
        "cld2_vs_best_conventional_ratio": ratio(best_pack, best_conv_val),
        "cld2_vs_v2_logical_ratio": ratio(best_pack, v2_logical),
        "cld2_saved_vs_raw_ratio": pct_saved(best_pack, raw),
        "cld2_saved_vs_best_conventional_ratio": pct_saved(best_pack, best_conv_val),
        "classification": cls,
        "classification_reason": reason,
        "all_cld2_runs": best.get("all_runs", []),
        "baseline": baseline,
    }


def write_csv(rows: list[dict[str, Any]], out: Path) -> None:
    fields = [
        "scenario",
        "classification",
        "classification_reason",
        "best_cld2_method",
        "best_cld2_pack_bytes",
        "file_level_raw_download_bytes",
        "best_conventional_baseline_name",
        "best_conventional_baseline_bytes",
        "cld2_vs_raw_ratio",
        "cld2_vs_best_conventional_ratio",
        "cld2_saved_vs_raw_ratio",
        "cld2_saved_vs_best_conventional_ratio",
        "v2_logical_bytes",
        "cld2_vs_v2_logical_ratio",
        "version",
        "profile",
        "codec",
    ]
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in fields})


def write_md(rows: list[dict[str, Any]], out: Path) -> None:
    lines = []
    lines.append("# CLD2 alpha51.1 — aggregate artifact benchmark report")
    lines.append("")
    lines.append("This report is generated from `bench_real_result.json` files.")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append("| Scenario | Class | Best CLD2 | Raw baseline | Best conventional baseline | CLD2/raw | CLD2/conventional |")
    lines.append("|---|---|---:|---:|---:|---:|---:|")
    for r in rows:
        conv_name = r.get("best_conventional_baseline_name") or "n/a"
        conv_bytes = fmt_bytes(r.get("best_conventional_baseline_bytes"))
        lines.append(
            f"| {r['scenario']} | {r['classification']} | "
            f"{fmt_bytes(r.get('best_cld2_pack_bytes'))} via `{r.get('best_cld2_method')}` | "
            f"{fmt_bytes(r.get('file_level_raw_download_bytes'))} | "
            f"{conv_name}: {conv_bytes} | "
            f"{fmt_ratio(r.get('cld2_vs_raw_ratio'))} | "
            f"{fmt_ratio(r.get('cld2_vs_best_conventional_ratio'))} |"
        )
    lines.append("")
    lines.append("## Interpretation")
    lines.append("")
    for r in rows:
        lines.append(f"### {r['scenario']}")
        lines.append("")
        lines.append(f"- Classification: **{r['classification']}**")
        lines.append(f"- Reason: {r['classification_reason']}")
        lines.append(f"- Best CLD2 method: `{r.get('best_cld2_method')}`")
        lines.append(f"- Best CLD2 bytes: {fmt_bytes(r.get('best_cld2_pack_bytes'))}")
        lines.append(f"- Raw baseline: {fmt_bytes(r.get('file_level_raw_download_bytes'))}")
        lines.append(f"- Best conventional baseline: `{r.get('best_conventional_baseline_name')}` = {fmt_bytes(r.get('best_conventional_baseline_bytes'))}")
        lines.append("")
    lines.append("## Claim boundary")
    lines.append("")
    lines.append("Alpha51.1 is an artifact-pair benchmark. It does not prove that CLD2 always wins. Results should be interpreted per artifact and per update pattern.")
    lines.append("")
    out.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-root", required=True, help="Directory containing scenario subfolders")
    ap.add_argument("--output-prefix", default="alpha51_1_aggregate")
    args = ap.parse_args()
    root = Path(args.out_root)
    result_paths = sorted(root.rglob("bench_real_result.json"))
    rows = [summarize_one(p) for p in result_paths]

    out_json = root / f"{args.output_prefix}_summary.json"
    out_csv = root / f"{args.output_prefix}_summary.csv"
    out_md = root / "ALPHA51_1_AGGREGATE_REPORT.md"

    payload = {
        "schema": "CLD2/alpha51_1_aggregate_summary",
        "result_count": len(rows),
        "claim_boundary": "Artifact-pair benchmark; compare per workload; do not generalize to all updates.",
        "scenarios": rows,
    }
    out_json.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_csv(rows, out_csv)
    write_md(rows, out_md)
    print(f"Wrote {out_json}")
    print(f"Wrote {out_csv}")
    print(f"Wrote {out_md}")


if __name__ == "__main__":
    main()

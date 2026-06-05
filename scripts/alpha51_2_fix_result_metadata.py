#!/usr/bin/env python3
"""CLD2 alpha51.2 result metadata cleanup.

Recursively fixes legacy version references in alpha51.1 result folders.
This script is intentionally conservative: it edits JSON structurally when possible,
and text/CSV/Markdown/HTML/log files by safe string replacement.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

LEGACY_STRINGS = [
    "2.0-" + "alpha32",
    "2.0.0-" + "alpha32",
    "alpha32",
]
TEXT_EXTS = {".csv", ".md", ".html", ".txt", ".log", ".ps1", ".py"}
JSON_EXTS = {".json"}
MAX_TEXT_BYTES = 25 * 1024 * 1024


def replace_text(text: str, code_version: str) -> tuple[str, int]:
    count = 0
    out = text
    for old in LEGACY_STRINGS:
        n = out.count(old)
        if n:
            out = out.replace(old, code_version)
            count += n
    return out, count


def fix_json_obj(obj: Any, code_version: str, benchmark_milestone: str) -> tuple[Any, int]:
    changes = 0
    if isinstance(obj, dict):
        new = {}
        for k, v in obj.items():
            nv, c = fix_json_obj(v, code_version, benchmark_milestone)
            new[k] = nv
            changes += c
        # Add metadata only to likely top-level result objects.
        if any(key in new for key in ("schema", "scenario", "classification", "results", "scenarios")):
            meta = new.get("alpha51_2_metadata")
            if not isinstance(meta, dict):
                new["alpha51_2_metadata"] = {
                    "code_baseline_version": code_version,
                    "benchmark_harness_milestone": benchmark_milestone,
                    "note": "metadata cleanup only; no benchmark rerun and no core logic change",
                }
                changes += 1
        return new, changes
    if isinstance(obj, list):
        new_list = []
        for v in obj:
            nv, c = fix_json_obj(v, code_version, benchmark_milestone)
            new_list.append(nv)
            changes += c
        return new_list, changes
    if isinstance(obj, str):
        out, c = replace_text(obj, code_version)
        return out, c
    return obj, 0


def write_report(result_root: Path, changed_files: list[dict[str, Any]], code_version: str, benchmark_milestone: str) -> None:
    report_md = result_root / "ALPHA51_2_METADATA_CLEANUP_REPORT.md"
    rows = []
    for item in changed_files:
        rows.append(f"| `{item['path']}` | {item['changes']} | {item['kind']} |")
    body = "\n".join(rows) if rows else "| _none_ | 0 | no changes needed |"
    report_md.write_text(
        f"""# CLD2 alpha51.2 — metadata cleanup report

## Summary

- Code baseline version: `{code_version}`
- Benchmark harness milestone: `{benchmark_milestone}`
- Heavy benchmark rerun: no
- Core CLD2 logic changed: no
- Files changed: {len(changed_files)}

## Changed files

| File | Replacements/changes | Kind |
|---|---:|---|
{body}

## Note

This cleanup removes confusing legacy `legacy alpha32 marker` metadata from alpha51.1 reports and replaces it with the current public code baseline version.
It does not change the benchmark numbers.
""",
        encoding="utf-8",
    )

    (result_root / "alpha51_2_metadata_cleanup.json").write_text(
        json.dumps(
            {
                "schema": "CLD2/alpha51_2_metadata_cleanup",
                "ok": True,
                "code_baseline_version": code_version,
                "benchmark_harness_milestone": benchmark_milestone,
                "core_logic_changed": False,
                "heavy_benchmark_rerun": False,
                "changed_files": changed_files,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--result-root", required=True, help="alpha51.1 result directory to patch in place")
    ap.add_argument("--code-version", default="2.0.0-alpha50.2")
    ap.add_argument("--benchmark-milestone", default="alpha51.2")
    args = ap.parse_args()

    root = Path(args.result_root)
    if not root.exists():
        raise SystemExit(f"result root not found: {root}")

    changed_files: list[dict[str, Any]] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if p.name.endswith(".zip") or p.suffix.lower() in {".cldrepo", ".pack"}:
            continue
        if p.stat().st_size > MAX_TEXT_BYTES:
            continue
        ext = p.suffix.lower()
        if ext in JSON_EXTS:
            try:
                obj = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                text = p.read_text(encoding="utf-8", errors="replace")
                new_text, c = replace_text(text, args.code_version)
                if c:
                    p.write_text(new_text, encoding="utf-8")
                    changed_files.append({"path": rel, "changes": c, "kind": "json-text-fallback"})
                continue
            fixed, c = fix_json_obj(obj, args.code_version, args.benchmark_milestone)
            if c:
                p.write_text(json.dumps(fixed, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                changed_files.append({"path": rel, "changes": c, "kind": "json"})
        elif ext in TEXT_EXTS:
            text = p.read_text(encoding="utf-8", errors="replace")
            new_text, c = replace_text(text, args.code_version)
            if c:
                p.write_text(new_text, encoding="utf-8")
                changed_files.append({"path": rel, "changes": c, "kind": "text"})

    write_report(root, changed_files, args.code_version, args.benchmark_milestone)
    print(json.dumps({"ok": True, "changed_files": len(changed_files), "result_root": str(root)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import importlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from . import __core_baseline__, __version__
from .selftest import run_selftest

REQUIRED_FILES = [
    "README.md",
    "SPEC_CLD2.md",
    "FORMAT_CLD2.md",
    "CLI.md",
    "THREAT_MODEL.md",
    "BENCHMARK_PLAN.md",
    "pyproject.toml",
    "cld2.py",
    "corelangdistribution2/__init__.py",
    "corelangdistribution2/repo.py",
    "corelangdistribution2/bench.py",
    "corelangdistribution2/profiles.py",
    "docs/REAL_BENCHMARKS_ALPHA56_3.md",
    "docs/BENCHMARK_CLAIM_BOUNDARY_ALPHA56_3.md",
    "docs/BENCHMARK_TOOLING_NOTES_ALPHA56_3.md",
]

FORBIDDEN_SUFFIXES = {".pyc", ".pyo"}
FORBIDDEN_NAMES = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".DS_Store", "Thumbs.db"}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _pep440_alpha(version: str) -> str:
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)-alpha(\d+)(?:\.(\d+))?", version)
    if not m:
        return version
    out = f"{m.group(1)}.{m.group(2)}.{m.group(3)}a{m.group(4)}"
    if m.group(5):
        out += f".post{m.group(5)}"
    return out


def dist_check(root: str | Path = ".", *, run_tests: bool = False) -> dict[str, Any]:
    """Check release ZIP/tree hygiene before publishing an alpha artifact."""
    base = Path(root).resolve()
    errors: list[str] = []
    warnings: list[str] = []
    details: dict[str, Any] = {"root": str(base), "version": __version__}

    if not base.exists():
        return {"ok": False, "errors": [f"path does not exist: {base}"], "warnings": [], "details": details}

    for rel in REQUIRED_FILES:
        if not (base / rel).exists():
            errors.append(f"missing required file: {rel}")

    forbidden: list[str] = []
    large_files: list[dict[str, Any]] = []
    total_bytes = 0
    file_count = 0
    for p in base.rglob("*"):
        rel = p.relative_to(base).as_posix()
        if p.name in FORBIDDEN_NAMES or p.suffix in FORBIDDEN_SUFFIXES:
            forbidden.append(rel)
        if p.is_file():
            file_count += 1
            try:
                size = p.stat().st_size
            except OSError:
                size = 0
            total_bytes += size
            if size > 10 * 1024 * 1024:
                large_files.append({"path": rel, "bytes": size})
    if forbidden:
        errors.extend(f"forbidden generated/cache file present: {x}" for x in forbidden[:50])
    if large_files:
        warnings.extend(f"large file in source package: {x['path']} ({x['bytes']} bytes)" for x in large_files[:20])

    pyproject = base / "pyproject.toml"
    if pyproject.exists():
        text = _read(pyproject)
        m = re.search(r'^version\s*=\s*"([^"]+)"', text, flags=re.MULTILINE)
        if not m:
            errors.append("pyproject.toml has no project version")
        else:
            expected = _pep440_alpha(__version__)
            if m.group(1) != expected:
                errors.append(f"pyproject version {m.group(1)!r} does not match package version {__version__!r} ({expected!r})")

    readme = base / "README.md"
    if readme.exists():
        readme_text = _read(readme).lower()
        if __version__.lower() not in readme_text:
            warnings.append(f"README.md does not mention package version {__version__}")
        if __core_baseline__.lower() not in readme_text:
            warnings.append(f"README.md does not mention core baseline {__core_baseline__}")
        if "new compression algorithm" not in readme_text:
            warnings.append("README.md does not state the compression-algorithm claim boundary")
        if "claim_boundary" not in readme_text:
            warnings.append("README.md does not link to a claim boundary document")

    compile_errors: list[str] = []
    for py in sorted(base.rglob("*.py")):
        rel = py.relative_to(base).as_posix()
        if any(part in FORBIDDEN_NAMES for part in py.parts):
            continue
        try:
            compile(py.read_text(encoding="utf-8"), str(py), "exec")
        except Exception as exc:
            compile_errors.append(f"{rel}: {exc}")
    if compile_errors:
        errors.extend(f"compile failed: {x}" for x in compile_errors[:20])

    import_checks = []
    for mod in [
        "corelangdistribution2.repo",
        "corelangdistribution2.bench",
        "corelangdistribution2.http_range",
        "corelangdistribution2.selftest",
        "corelangdistribution2.release",
        "corelangdistribution2.profiles",
    ]:
        try:
            importlib.import_module(mod)
            import_checks.append({"module": mod, "ok": True})
        except Exception as exc:
            import_checks.append({"module": mod, "ok": False, "error": str(exc)})
            errors.append(f"import failed: {mod}: {exc}")

    selftest_report = None
    if run_tests:
        selftest_report = run_selftest(quick=True)
        if not selftest_report.get("ok"):
            errors.append("embedded selftest failed: " + str(selftest_report.get("error", "unknown")))

    details.update({
        "python": sys.version.split()[0],
        "file_count": file_count,
        "total_bytes": total_bytes,
        "forbidden_count": len(forbidden),
        "large_files": large_files,
        "import_checks": import_checks,
        "selftest": selftest_report,
    })
    return {"ok": not errors, "errors": errors, "warnings": warnings, "details": details}


def write_json_report(path: str | Path, report: dict[str, Any]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def _md_table_row(cols: list[object]) -> str:
    return "| " + " | ".join(str(c) for c in cols) + " |"


def beta_report(
    root: str | Path = ".",
    out_dir: str | Path = "beta_report",
    *,
    run_tests: bool = True,
    run_benchmarks: bool = True,
    benchmark_profile: str = "quick",
    scenarios: list[str] | None = None,
    cost_per_gb: float = 0.05,
    download_count: int = 1000,
    currency: str = "USD",
) -> dict[str, Any]:
    """Create a compact beta-readiness dossier for a CLD2 source tree.

    The report intentionally combines release hygiene, optional selftest, and a
    small benchmark matrix into one artifact set. It is a gate/reporting tool,
    not a substitute for large external corpus and real CDN testing.
    """
    base = Path(root).resolve()
    out = Path(out_dir).resolve()
    if out.exists():
        import shutil
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    dist = dist_check(base, run_tests=run_tests)
    bench = None
    bench_error = None
    if run_benchmarks:
        try:
            from .bench import run_bench_matrix, write_matrix_business_report
            bench_scenarios = scenarios or ["game-patch-insert", "dataset-model", "media-catalog", "random-worstcase"]
            bench_dir = out / "benchmark_matrix"
            bench = run_bench_matrix(bench_dir, scenarios=bench_scenarios, profile=benchmark_profile)
            write_matrix_business_report(
                bench,
                out / "benchmark_matrix_business_report.md",
                cost_per_gb=cost_per_gb,
                download_count=download_count,
                currency=currency,
            )
            # Keep beta dossiers small and reviewable: preserve reports, remove
            # generated corpora/repos/tarballs that can be recreated by rerunning
            # the benchmark command.
            import shutil
            for child in list(bench_dir.rglob("*")):
                if child.is_dir() and (child.name == "corpus" or child.name.endswith(".cldrepo")):
                    shutil.rmtree(child, ignore_errors=True)
            for child in list(bench_dir.rglob("*")):
                if child.is_file() and (child.suffix in {".gz", ".zst", ".tar"} or child.name.startswith("changed_files.tar") or child.name.startswith("v2_full.tar")):
                    child.unlink(missing_ok=True)
        except Exception as exc:
            bench_error = str(exc)

    hard_gates = {
        "dist_check_ok": bool(dist.get("ok")),
        "selftest_ok": bool((dist.get("details", {}).get("selftest") or {"ok": not run_tests}).get("ok")),
        "benchmark_matrix_ok": bench_error is None and (bench is not None or not run_benchmarks),
    }
    ok = all(hard_gates.values())
    report = {
        "schema": "CoreLangDistribution/BetaReadinessReport",
        "version": __version__,
        "ok": ok,
        "hard_gates": hard_gates,
        "root": str(base),
        "out_dir": str(out),
        "dist_check": dist,
        "benchmark_profile": benchmark_profile,
        "benchmark_scenarios": scenarios or ["game-patch-insert", "dataset-model", "media-catalog", "random-worstcase"],
        "benchmark_error": bench_error,
        "benchmark_reports": (bench or {}).get("reports") if isinstance(bench, dict) else None,
        "limitations": [
            "This report is a local readiness dossier, not a production certification.",
            "It does not replace large 10-50 GB corpus tests, real Windows/macOS/Linux validation, or real CDN tests.",
            "Cost projections are byte-transfer estimates only and exclude request fees, cache hit rates, taxes and negotiated CDN tiers.",
        ],
    }

    json_path = out / "CLD2_BETA_READINESS_REPORT.json"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")

    lines = [
        "# CoreLangDistribution 2.0 - Beta readiness dossier",
        "",
        f"Version: `{__version__}`",
        "",
        "## Verdict",
        "",
        f"Overall: **{'PASS' if ok else 'FAIL'}**",
        "",
        "| Gate | Result |",
        "|---|---:|",
    ]
    for k, v in hard_gates.items():
        lines.append(_md_table_row([k, "PASS" if v else "FAIL"]))
    lines += [
        "",
        "## Distribution hygiene",
        "",
        f"- dist-check: {'OK' if dist.get('ok') else 'FAILED'}",
        f"- source files: {dist.get('details', {}).get('file_count')}",
        f"- source bytes: {dist.get('details', {}).get('total_bytes')}",
        f"- warnings: {len(dist.get('warnings', []))}",
        f"- errors: {len(dist.get('errors', []))}",
    ]
    if dist.get("warnings"):
        lines += ["", "### Warnings"] + [f"- {w}" for w in dist.get("warnings", [])]
    if dist.get("errors"):
        lines += ["", "### Errors"] + [f"- {e}" for e in dist.get("errors", [])]
    if run_tests:
        st = dist.get("details", {}).get("selftest") or {}
        lines += [
            "",
            "## Embedded selftest",
            "",
            f"- ok: {st.get('ok')}",
            f"- quick: {st.get('quick')}",
            f"- seconds: {st.get('seconds')}",
            f"- tests: {len(st.get('tests', [])) if isinstance(st.get('tests'), list) else 0}",
        ]
    lines += [
        "",
        "## Benchmark matrix",
        "",
    ]
    if bench_error:
        lines.append(f"Benchmark failed: `{bench_error}`")
    elif bench:
        lines += [
            f"- profile: `{benchmark_profile}`",
            f"- scenarios: {', '.join(report['benchmark_scenarios'])}",
            f"- matrix report: `{Path(bench.get('reports', {}).get('markdown', '')).name}`",
            f"- business report: `benchmark_matrix_business_report.md`",
            "",
            "| Scenario | Method | Update bytes | Chunk reuse | Saved vs file-level raw |",
            "|---|---|---:|---:|---:|",
        ]
        for res in bench.get("results", []):
            for run in res.get("runs", []):
                d = run.get("diff", {})
                lines.append(_md_table_row([
                    res.get("scenario"),
                    run.get("chunker"),
                    d.get("download_required_pack_bytes"),
                    f"{float(d.get('chunk_reuse_ratio') or 0):.2%}",
                    f"{float(d.get('estimated_saved_ratio_vs_file_level_raw') or 0):.2%}",
                ]))
    else:
        lines.append("Benchmark matrix was skipped.")
    lines += [
        "",
        "## Remaining beta blockers",
        "",
        "- Run at least one 10-50 GB real-pair benchmark outside the sandbox.",
        "- Validate on real Windows and macOS, especially path/case/Unicode behavior.",
        "- Validate against a real HTTP/CDN endpoint with Range, ETag and retries.",
        "- Add crash/power-loss testing with fsync/atomicity review.",
        "- Decide the beta key-management policy: root rotation, revocation and timestamp/snapshot metadata.",
        "",
        "## Artifacts",
        "",
        f"- JSON report: `{json_path.name}`",
        "- Benchmark artifacts: `benchmark_matrix/`",
    ]
    md_path = out / "CLD2_BETA_READINESS_REPORT.md"
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    report["reports"] = {"json": str(json_path), "markdown": str(md_path)}
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    return report

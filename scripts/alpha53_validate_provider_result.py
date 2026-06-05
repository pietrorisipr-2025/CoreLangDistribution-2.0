#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
        f.write("\n")


def deep_get(obj: dict[str, Any], keys: list[str], default: Any = None) -> Any:
    cur: Any = obj
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def boolish(v: Any) -> bool:
    return v is True or str(v).strip().lower() == "true"


def extract_audit_ok(result: dict[str, Any]) -> Any:
    candidates = [
        deep_get(result, ["audit_install", "ok"]),
        deep_get(result, ["audit_install_result", "ok"]),
        deep_get(result, ["audit", "ok"]),
        deep_get(result, ["audit_install"]),
    ]
    for c in candidates:
        if c is not None:
            if isinstance(c, dict):
                return c.get("ok")
            return c
    return None


def extract_expected_actual(result: dict[str, Any]) -> tuple[Any, Any]:
    expected = (
        result.get("expected_sha256")
        or result.get("expected_digest")
        or deep_get(result, ["digest", "expected_sha256"])
        or deep_get(result, ["verification", "expected_sha256"])
    )
    actual = (
        result.get("actual_sha256")
        or result.get("actual_digest")
        or deep_get(result, ["digest", "actual_sha256"])
        or deep_get(result, ["verification", "actual_sha256"])
    )
    return expected, actual


def validate(result: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []

    require_ok = bool(policy.get("require_ok", True))
    require_digest_ok = bool(policy.get("require_digest_ok", True))
    require_audit_install_ok = bool(policy.get("require_audit_install_ok", True))
    policy_expected_sha256 = policy.get("expected_sha256")

    result_ok = result.get("ok")
    digest_ok = result.get("digest_ok")
    audit_ok = extract_audit_ok(result)
    expected, actual = extract_expected_actual(result)

    if require_ok and not boolish(result_ok):
        errors.append(f"provider result ok is not true: {result_ok!r}")

    if require_digest_ok and not boolish(digest_ok):
        errors.append(f"digest_ok is not true: {digest_ok!r}")

    if require_audit_install_ok and not boolish(audit_ok):
        errors.append(f"audit_install ok is not true: {audit_ok!r}")

    if not expected:
        errors.append("expected_sha256 missing from provider result")
    if not actual:
        errors.append("actual_sha256 missing from provider result")

    if expected and actual and str(expected).lower() != str(actual).lower():
        errors.append("provider result expected_sha256 != actual_sha256")

    if policy_expected_sha256 and actual and str(policy_expected_sha256).lower() != str(actual).lower():
        errors.append("policy expected_sha256 != provider actual_sha256")

    if not policy_expected_sha256:
        warnings.append("policy expected_sha256 not provided; relying on result self-consistency only")

    return {
        "schema": "CLD2/alpha53_provider_policy_validation",
        "code_baseline": policy.get("code_baseline", "2.0.0-alpha50.2"),
        "benchmark_milestone": policy.get("benchmark_milestone", "alpha53"),
        "ok": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "checks": {
            "require_ok": require_ok,
            "require_digest_ok": require_digest_ok,
            "require_audit_install_ok": require_audit_install_ok,
            "result_ok": result_ok,
            "digest_ok": digest_ok,
            "audit_install_ok": audit_ok,
            "result_expected_sha256": expected,
            "result_actual_sha256": actual,
            "policy_expected_sha256": policy_expected_sha256,
        },
    }


def make_tampered_digest_result(result: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(result)
    expected, actual = extract_expected_actual(out)
    if actual:
        bad = "0" * 64
        if str(actual).lower() == bad:
            bad = "1" * 64
        out["actual_sha256"] = bad
    else:
        out["actual_sha256"] = "0" * 64
    if not expected:
        out["expected_sha256"] = "f" * 64
    out["digest_ok"] = False
    return out


def make_audit_failure_result(result: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(result)
    out["ok"] = False
    if isinstance(out.get("audit_install"), dict):
        out["audit_install"]["ok"] = False
    else:
        out["audit_install"] = {"ok": False, "error": "simulated alpha53 negative audit failure"}
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate CLD2 alpha52 provider result against alpha53 policy.")
    ap.add_argument("--provider-result", required=True, type=Path)
    ap.add_argument("--policy", type=Path)
    ap.add_argument("--expected-sha256")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--mode", choices=["positive", "tampered-digest", "audit-failure"], default="positive")
    args = ap.parse_args()

    result = load_json(args.provider_result)

    if args.mode == "tampered-digest":
        result = make_tampered_digest_result(result)
    elif args.mode == "audit-failure":
        result = make_audit_failure_result(result)

    if args.policy:
        policy = load_json(args.policy)
    else:
        policy = {
            "schema": "CLD2/alpha53_provider_policy",
            "code_baseline": "2.0.0-alpha50.2",
            "benchmark_milestone": "alpha53",
            "require_ok": True,
            "require_digest_ok": True,
            "require_audit_install_ok": True,
        }

    if args.expected_sha256:
        policy["expected_sha256"] = args.expected_sha256

    validation = validate(result, policy)
    validation["mode"] = args.mode
    save_json(args.out, validation)
    return 0 if validation["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

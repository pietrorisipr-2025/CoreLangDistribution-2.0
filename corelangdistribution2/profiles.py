from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .chunker import parse_size

SUPPORTED_SCHEMA_VERSION = 1
SUPPORTED_CHUNKERS = {"fixed", "cdc", "fastcdc", "gear"}
SUPPORTED_CODECS = {"auto", "zstd", "zlib", "raw", "none"}
SUPPORTED_KEYS = {
    "schema_version",
    "name",
    "description",
    "chunker",
    "codec",
    "fixed_size",
    "chunk_min",
    "chunk_avg",
    "chunk_max",
    "fastcdc_stride",
    "claim_boundary",
    "metadata",
}

DEFAULT_PACK_OPTIONS = {
    "fixed_size": "256KiB",
    "chunk_min": "64KiB",
    "chunk_avg": "256KiB",
    "chunk_max": "1MiB",
    "fastcdc_stride": 16,
}


class ProfileError(ValueError):
    """Raised when a CLD2 user profile is missing or invalid."""


def _require_text(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ProfileError(f"profile field {key!r} must be a non-empty string")
    return value.strip()


def _require_size(data: dict[str, Any], key: str) -> str:
    value = _require_text(data, key)
    try:
        parsed = parse_size(value)
    except Exception as exc:
        raise ProfileError(f"profile field {key!r} is not a valid CLD2 size: {value!r}") from exc
    if parsed <= 0:
        raise ProfileError(f"profile field {key!r} must be positive")
    return value


def load_profile(path: str | Path) -> dict[str, Any]:
    """Load and validate a JSON profile file.

    Alpha56.3 intentionally rejects unknown top-level keys so profile files are
    explicit and reproducible across machines.
    """
    p = Path(path)
    if not p.is_file():
        raise ProfileError(f"profile file not found: {p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProfileError(f"profile file is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ProfileError("profile JSON must be an object")

    unknown = sorted(set(data) - SUPPORTED_KEYS)
    if unknown:
        raise ProfileError("unknown profile field(s): " + ", ".join(unknown))

    if data.get("schema_version") != SUPPORTED_SCHEMA_VERSION:
        raise ProfileError("profile field 'schema_version' must be 1")

    name = _require_text(data, "name")
    description = _require_text(data, "description")
    chunker = _require_text(data, "chunker")
    codec = _require_text(data, "codec")
    if chunker not in SUPPORTED_CHUNKERS:
        raise ProfileError(f"unsupported chunker: {chunker}")
    if codec not in SUPPORTED_CODECS:
        raise ProfileError(f"unsupported codec: {codec}")

    if chunker == "fixed":
        _require_size(data, "fixed_size")
    else:
        min_s = _require_size(data, "chunk_min")
        avg_s = _require_size(data, "chunk_avg")
        max_s = _require_size(data, "chunk_max")
        min_b = parse_size(min_s)
        avg_b = parse_size(avg_s)
        max_b = parse_size(max_s)
        if not (0 < min_b <= avg_b <= max_b):
            raise ProfileError("CDC profile sizes must satisfy 0 < chunk_min <= chunk_avg <= chunk_max")

    if "fastcdc_stride" in data:
        try:
            stride = int(data["fastcdc_stride"])
        except Exception as exc:
            raise ProfileError("profile field 'fastcdc_stride' must be an integer") from exc
        if stride <= 0:
            raise ProfileError("profile field 'fastcdc_stride' must be positive")

    if "claim_boundary" in data and not isinstance(data["claim_boundary"], str):
        raise ProfileError("profile field 'claim_boundary' must be a string")
    if "metadata" in data and not isinstance(data["metadata"], dict):
        raise ProfileError("profile field 'metadata' must be an object")

    profile = dict(data)
    profile["name"] = name
    profile["description"] = description
    profile["chunker"] = chunker
    profile["codec"] = codec
    profile["_profile_file"] = str(p)
    return profile


def profile_pack_options(profile: dict[str, Any]) -> dict[str, Any]:
    """Return make_repo-compatible options from a validated profile."""
    options = dict(DEFAULT_PACK_OPTIONS)
    for key in ["fixed_size", "chunk_min", "chunk_avg", "chunk_max", "fastcdc_stride"]:
        if key in profile:
            options[key] = profile[key]
    options["chunker"] = profile["chunker"]
    options["codec"] = profile["codec"]
    return options


def profile_metadata(profile: dict[str, Any], profile_file: str | Path | None = None) -> dict[str, Any]:
    p = Path(profile_file or profile.get("_profile_file", ""))
    return {
        "schema_version": profile.get("schema_version"),
        "name": profile.get("name"),
        "description": profile.get("description"),
        "profile_file": p.name if str(p) else None,
        "chunker": profile.get("chunker"),
        "codec": profile.get("codec"),
        "claim_boundary": profile.get("claim_boundary"),
        "metadata": profile.get("metadata", {}),
    }


def profile_summary(profile: dict[str, Any]) -> dict[str, Any]:
    options = profile_pack_options(profile)
    return {
        "ok": True,
        "schema_version": profile.get("schema_version"),
        "name": profile.get("name"),
        "description": profile.get("description"),
        "chunker": options["chunker"],
        "codec": options["codec"],
        "fixed_size": options["fixed_size"],
        "chunk_min": options["chunk_min"],
        "chunk_avg": options["chunk_avg"],
        "chunk_max": options["chunk_max"],
        "fastcdc_stride": int(options["fastcdc_stride"]),
        "claim_boundary": profile.get("claim_boundary"),
        "profile_file": Path(profile.get("_profile_file", "")).name,
    }

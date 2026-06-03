from __future__ import annotations

import os
import zlib
from dataclasses import dataclass
from pathlib import Path

try:
    import zstandard as zstd  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    zstd = None

ALREADY_COMPRESSED_EXTS = {
    ".7z", ".zip", ".rar", ".gz", ".xz", ".bz2", ".zst",
    ".mp4", ".mkv", ".mov", ".webm", ".mp3", ".aac", ".ogg", ".opus", ".flac",
    ".jpg", ".jpeg", ".png", ".webp", ".avif", ".heic", ".gif",
    ".pdf", ".woff", ".woff2",
}


@dataclass(frozen=True)
class Packed:
    codec: str
    payload: bytes


def looks_precompressed(path: str | Path) -> bool:
    return Path(path).suffix.lower() in ALREADY_COMPRESSED_EXTS


def compress(data: bytes, codec: str = "auto", path_hint: str = "") -> Packed:
    if codec == "none" or codec == "raw":
        return Packed("raw", data)
    if codec == "auto" and (looks_precompressed(path_hint) or len(data) < 64):
        return Packed("raw", data)

    candidates: list[Packed] = []
    if codec in ("auto", "zstd") and zstd is not None:
        c = zstd.ZstdCompressor(level=3).compress(data)
        candidates.append(Packed("zstd", c))
    if codec in ("auto", "zlib"):
        candidates.append(Packed("zlib", zlib.compress(data, level=6)))
    if codec not in ("auto", "zstd", "zlib"):
        raise ValueError(f"unsupported codec: {codec}")

    if not candidates:
        return Packed("raw", data)
    best = min(candidates, key=lambda p: len(p.payload))
    # Keep raw if compression does not save at least 2% plus tiny header overhead.
    if len(best.payload) >= max(1, int(len(data) * 0.98)):
        return Packed("raw", data)
    return best


def decompress(codec: str, payload: bytes, raw_len: int | None = None) -> bytes:
    if codec == "raw":
        return payload
    if codec == "zlib":
        return zlib.decompress(payload)
    if codec == "zstd":
        if zstd is None:
            raise RuntimeError("zstandard is required to decode zstd chunks")
        return zstd.ZstdDecompressor().decompress(payload, max_output_size=raw_len or 0)
    raise ValueError(f"unsupported codec: {codec}")

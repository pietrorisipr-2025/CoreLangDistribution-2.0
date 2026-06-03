from __future__ import annotations

import random
from pathlib import Path
from typing import Iterator, Tuple

KiB = 1024
MiB = 1024 * 1024

# Deterministic Gear table. Stable across platforms/releases.
_rng = random.Random(0x434C4432)
GEAR_TABLE = [_rng.getrandbits(64) for _ in range(256)]


def parse_size(value: str | int) -> int:
    if isinstance(value, int):
        return value
    s = value.strip().lower().replace(" ", "")
    units = [("gib", 1024 * MiB), ("gb", 1000 * 1000 * 1000), ("kib", KiB), ("kb", 1000), ("mib", MiB), ("mb", 1000 * 1000), ("b", 1)]
    for suffix, mult in units:
        if s.endswith(suffix):
            return int(float(s[: -len(suffix)]) * mult)
    return int(s)


def _pow2_floor(value: int) -> int:
    p = 1
    while p * 2 <= max(1, value):
        p *= 2
    return p


def _mask_for_avg(avg_size: int) -> int:
    return _pow2_floor(avg_size) - 1


def fixed_chunks(data: bytes, size: int) -> Iterator[Tuple[int, bytes]]:
    if size <= 0:
        raise ValueError("fixed chunk size must be positive")
    for off in range(0, len(data), size):
        yield off, data[off : off + size]


def gear_cdc_chunks(data: bytes, min_size: int, avg_size: int, max_size: int) -> Iterator[Tuple[int, bytes]]:
    """Small deterministic Gear CDC reference implementation.

    Kept for reproducibility/comparison. The production default for `cdc` below is the
    normalized FastCDC-like variant.
    """
    n = len(data)
    if n == 0:
        return
    if not (0 < min_size <= avg_size <= max_size):
        raise ValueError("require 0 < min_size <= avg_size <= max_size")
    mask = _mask_for_avg(avg_size)
    start = 0
    h = 0
    pos = 0
    while pos < n:
        b = data[pos]
        h = ((h << 1) + GEAR_TABLE[b]) & 0xFFFFFFFFFFFFFFFF
        span = pos + 1 - start
        cut = span >= max_size or (span >= min_size and (h & mask) == 0)
        if cut:
            yield start, data[start : pos + 1]
            start = pos + 1
            h = 0
        pos += 1
    if start < n:
        yield start, data[start:n]


def _normalized_cut_positions(data: bytes | bytearray | memoryview, min_size: int, avg_size: int, max_size: int, *, stride: int = 1) -> Iterator[int]:
    """Yield cut offsets for a FastCDC-like Gear chunker.

    `stride=1` is the exact alpha17-compatible normalized CDC scan. Larger strides
    check fewer candidate positions and are intended for alpha18/alpha19 large-file fastcdc
    mode. This trades a little boundary precision for much lower Python loop cost.
    """
    n = len(data)
    if n == 0:
        return
    if not (0 < min_size <= avg_size <= max_size):
        raise ValueError("require 0 < min_size <= avg_size <= max_size")
    stride = max(1, int(stride))
    base = _pow2_floor(avg_size)
    mask_strict = max(1, (base * 2) - 1)
    mask_easy = max(1, (base // 2) - 1)

    table = GEAR_TABLE
    start = 0
    while start < n:
        remaining = n - start
        if remaining <= max(min_size, 1):
            yield n
            break
        pos = min(start + min_size, n - 1)
        h = 0
        end = min(start + max_size, n)
        normal_end = min(start + avg_size, end)
        cut = None

        # Strict phase: try not to cut too early.
        while pos < normal_end:
            h = ((h << 1) + table[data[pos]]) & 0xFFFFFFFFFFFFFFFF
            if (h & mask_strict) == 0:
                cut = pos + 1
                break
            pos += stride

        # Easy phase: cut soon after avg if not already cut.
        if cut is None:
            while pos < end:
                h = ((h << 1) + table[data[pos]]) & 0xFFFFFFFFFFFFFFFF
                if (h & mask_easy) == 0:
                    cut = pos + 1
                    break
                pos += stride

        if cut is None:
            cut = end
        yield cut
        start = cut


def normalized_cdc_chunks(data: bytes, min_size: int, avg_size: int, max_size: int) -> Iterator[Tuple[int, bytes]]:
    """FastCDC-like normalized content-defined chunking.

    This is the exact alpha17-compatible normalized CDC path. Alpha18 adds a separate
    `fastcdc` mode for large-file speed; `cdc` stays conservative/reproducible.
    """
    start = 0
    for cut in _normalized_cut_positions(data, min_size, avg_size, max_size, stride=1):
        yield start, data[start:cut]
        start = cut


def fastcdc_chunks(data: bytes, min_size: int, avg_size: int, max_size: int, *, stride: int = 16) -> Iterator[Tuple[int, bytes]]:
    """Alpha18 large-file CDC mode.

    It uses the same deterministic masks as normalized CDC, but only evaluates every
    Nth candidate byte. This is much faster in Python and still content-defined. It is
    intended for large-file benchmarking/packing when exact alpha17 CDC boundaries are
    not required.
    """
    start = 0
    for cut in _normalized_cut_positions(data, min_size, avg_size, max_size, stride=stride):
        yield start, data[start:cut]
        start = cut


def iter_chunks(
    data: bytes,
    mode: str,
    fixed_size: int,
    min_size: int,
    avg_size: int,
    max_size: int,
    *,
    fastcdc_stride: int = 16,
) -> Iterator[Tuple[int, bytes]]:
    if mode == "fixed":
        yield from fixed_chunks(data, fixed_size)
    elif mode in ("cdc", "normalized-cdc"):
        yield from normalized_cdc_chunks(data, min_size, avg_size, max_size)
    elif mode in ("fastcdc", "cdc-fast"):
        yield from fastcdc_chunks(data, min_size, avg_size, max_size, stride=fastcdc_stride)
    elif mode in ("gear", "gear-cdc"):
        yield from gear_cdc_chunks(data, min_size, avg_size, max_size)
    else:
        raise ValueError(f"unknown chunker: {mode}")


def iter_file_chunks(
    path: str | Path,
    mode: str,
    fixed_size: int,
    min_size: int,
    avg_size: int,
    max_size: int,
    *,
    read_size: int = 8 * MiB,
    fastcdc_stride: int = 16,
) -> Iterator[Tuple[int, bytes]]:
    """Stream chunks from a file without loading the whole file into memory.

    This is the alpha18 large-file hygiene improvement. For small files it behaves the
    same as `iter_chunks`, but for 10+ GiB files it avoids `Path.read_bytes()` and keeps
    memory bounded to roughly max(read_size, max_size) plus the current chunk.
    """
    p = Path(path)
    if mode == "fixed":
        off = 0
        with p.open("rb") as f:
            while True:
                data = f.read(fixed_size)
                if not data:
                    break
                yield off, data
                off += len(data)
        return

    if mode in ("gear", "gear-cdc"):
        # The old Gear reference is kept simple. It is not intended for large files.
        yield from iter_chunks(p.read_bytes(), mode, fixed_size, min_size, avg_size, max_size)
        return

    if mode in ("cdc", "normalized-cdc"):
        # For normal-sized inputs, keep the exact alpha17 in-memory behavior, which
        # is faster than the streaming reference in CPython. Very large files still
        # stream to avoid requiring huge RAM. Use fastcdc for the alpha18 large-file
        # optimized path.
        if p.stat().st_size <= 512 * MiB:
            yield from iter_chunks(p.read_bytes(), mode, fixed_size, min_size, avg_size, max_size)
            return
        stride = 1
    elif mode in ("fastcdc", "cdc-fast"):
        stride = max(1, int(fastcdc_stride))
    else:
        raise ValueError(f"unknown chunker: {mode}")

    if not (0 < min_size <= avg_size <= max_size):
        raise ValueError("require 0 < min_size <= avg_size <= max_size")

    read_size = max(int(read_size), max_size, 64 * KiB)
    buf = bytearray()
    file_off = 0
    eof = False

    with p.open("rb") as f:
        while True:
            # Ensure enough buffered data to decide a max-size cut unless EOF.
            while not eof and len(buf) < max_size:
                data = f.read(read_size)
                if not data:
                    eof = True
                    break
                buf.extend(data)

            if not buf:
                break

            if not eof and len(buf) < max_size:
                continue

            # At EOF, the tail may be shorter than max_size and should still be chunked.
            scan = buf if eof else bytes(buf[:max_size])
            cut = None
            for c in _normalized_cut_positions(scan, min_size, avg_size, max_size, stride=stride):
                cut = int(c)
                break
            if cut is None or cut <= 0:
                cut = len(buf)
            raw = bytes(buf[:cut])
            yield file_off, raw
            file_off += len(raw)
            del buf[:cut]

            if eof and not buf:
                break


def chunk_stats(lengths: list[int]) -> dict:
    if not lengths:
        return {"count": 0, "min": 0, "avg": 0, "max": 0}
    return {
        "count": len(lengths),
        "min": min(lengths),
        "avg": round(sum(lengths) / len(lengths), 2),
        "max": max(lengths),
    }

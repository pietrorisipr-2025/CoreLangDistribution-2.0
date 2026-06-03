# CLD2 alpha24 benchmark plan

Alpha20 keeps the alpha19 lesson: benchmark the trade-off, not just the best number.

## Required large-file matrix

For each row report:

- pack time v1;
- pack time v2;
- download bytes;
- chunk reuse ratio;
- estimated egress cost;
- savings vs file-level raw;
- savings vs external/measured tar.zst baseline when available.

## Profiles

| Profile | min / avg / max | stride | Intent |
|---|---:|---:|---|
| large-file-small | 64 KiB / 256 KiB / 1 MiB | 4 | maximum reuse, slower |
| large-file-balanced | 128 KiB / 512 KiB / 2 MiB | 8 | recommended default |
| large-file-large | 256 KiB / 1 MiB / 4 MiB | 16 | fastest, less precise |

## Guardrail variants

Run at least:

- middle insertion;
- localized same-size overwrite;
- random rewrite 1%.

Middle insertions are CDC-friendly. Random rewrite tests are important because they prevent overclaiming.

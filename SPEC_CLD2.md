# CoreLangDistribution 2.0 alpha24 specification note

Alpha20 keeps the CLD2 direction: deterministic chunked repositories, delta-friendly update planning, HTTP/range-friendly delivery, verification, and honest benchmark reporting.

The main alpha24 policy change is that `large-file` now maps to the balanced FastCDC preset selected by the alpha19 FITS tuning matrix.

Recommended large-file preset:

```text
chunk_min = 128 KiB
chunk_avg = 512 KiB
chunk_max = 2 MiB
fastcdc_stride = 8
```

Exact profile names remain available for explicit trade-off testing:

```text
large-file-small
large-file-balanced
large-file-large
```

# CLD2 alpha56.3 real benchmark status

This document summarizes the real benchmark review that followed alpha56.2.1. The results are mixed by design: positive cases, moderate cases, and negative controls are all kept visible.

## Summary

CLD2 shows real value on structured, updateable payloads with cross-version reuse. The strongest alpha56.2.1 real benchmark was the extracted AMD RDNA driver payload. CLD2 is not a generic compressor and does not automatically help on monolithic installers, single executables, or datasets without version locality.

## Result classifications

| Domain | Scenario | Best method | CLD2 pack MB | Full v2 tar.zstd MB | Changed-files tar.zstd MB | Chunk reuse | Classification |
|---|---|---:|---:|---:|---:|---:|---|
| AMD RDNA driver | 25.8.1 -> 25.9.1 extracted structured driver payload | fixed | 804.313 | 1127.287 | 971.690 | 49.604% | strong_positive_structured_payload |
| AMD RDNA driver | 25.8.1 -> 25.9.1 monolithic EXE installer | fixed | 955.556 | 955.555 | 955.555 | 0.000% | negative_monolithic_installer |
| LibreOffice | 25.8.6 -> 25.8.7 extracted clean folder | fixed | 243.868 | 423.892 | 233.576 | 91.562% | positive_vs_full_but_not_vs_changed_files |
| LibreOffice | 25.8.7 -> 26.2.4 extracted clean folder | fastcdc | 346.670 | 429.699 | 301.989 | 65.965% | positive_vs_full_but_not_vs_changed_files |
| melonDS | Windows extracted single executable releases | cdc / quick_small_chunks | 12.729-18.556 | 12.655-18.197 | 12.654-18.191 | 2.326%-5.634% | negative_single_binary_small_reuse_not_enough |
| SDSS FITS | DR11v1 -> DR12v5 decompressed .fits | fastcdc | 3037.975 | 2931.848 | 2931.853 | 0.000% | negative_no_reuse_dataset_drift |
| SDSS FITS | DR11v1 -> DR12v5 .fits.gz | fixed | 3089.789 | 3089.860 | 3089.861 | 0.000% | negative_no_reuse_dataset_drift |

The full consolidated CSV rows are included in `docs/real_benchmarks_alpha56_3/consolidated_best_cases.csv`.

## Interpretation

AMD RDNA extracted payload is the strongest positive case. CLD2 fixed-mode warm update was smaller than both full v2 tar.zstd and changed-files tar.zstd.

LibreOffice extracted clean folders are a moderate positive case. CLD2 beat full v2 tar.zstd, but did not beat changed-files tar.zstd.

melonDS is a negative control. The Windows releases are dominated by a single executable. Small chunking found limited reuse, but not enough to beat tar.zstd.

FITS DR11 -> DR12 is a negative control. This pair showed dataset drift and no useful reuse on compressed or decompressed FITS files.

Monolithic EXE/MSI installers are not a positive case. If possible, benchmark extracted structured payloads separately from opaque installer files.

## Methodology boundary

The benchmark outputs used here are summaries, CSV rows, methodology notes, hashes, names, and sizes. Proprietary or copyrighted input payloads from AMD, LibreOffice, melonDS, SDSS, Windows, PDFs, ROMs, or similar sources are not included in this repository.

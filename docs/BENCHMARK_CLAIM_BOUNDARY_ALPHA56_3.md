# CLD2 alpha56.3 benchmark claim boundary

CLD2 shows real value on structured, updateable payloads with cross-version reuse. The strongest alpha56.2.1 real benchmark was the extracted AMD RDNA driver payload. CLD2 is not a generic compressor and does not automatically help on monolithic installers, single executables, or datasets without version locality.

## Safe claims

- CLD2 can reduce warm-update transfer size for structured, versioned artifacts with substantial cross-version reuse.
- AMD RDNA extracted driver payload is the strongest current positive case in this benchmark set.
- LibreOffice extracted folders are useful but mixed: CLD2 beat full v2 tar.zstd, not changed-files tar.zstd.
- melonDS and FITS are negative controls that define limits.
- Monolithic EXE/MSI installer files should be separated from extracted structured payloads in reports.

## Unsafe claims

- Do not claim universal compression superiority.
- Do not claim universal update superiority.
- Do not claim CLD2 broadly beats rsync, zsync, casync, OSTree, DVC, Docker, OCI, or other mature systems.
- Do not claim CDN/cloud production performance from local benchmark data.
- Do not claim enterprise readiness.
- Do not claim an external security audit.

## Practical guidance

Use CLD2 as an experimental cost-aware warm-update distribution planner. Treat real benchmark results as workload-specific evidence, not as universal product claims.

# Benchmark summary — alpha45.9 to alpha49

| Scenario | CLD2 warm | zsync | casync | OSTree | DVC | OCI single | OCI per-file |
|---|---:|---:|---:|---:|---:|---:|---:|
| normal | 419 | 239.827 | 10.986 | 196.227 | 67.109.107 | 196.511 | 196.733 |
| high-entropy | 67.108.937 | 67.342.559 | 67.160.433 | 67.130.107 | 67.109.076 | 67.130.606 | 67.130.785 |
| small-files | 2.147 | 3.515.724 | 55.116 | 34.554 | 6.874.124 | 188.165 | 73.044 |
| heavy-change | 26.845.855 | 27.078.879 | 26.871.717 | 26.971.645 | 67.109.040 | 26.971.886 | 26.972.139 |

## Metric caveats

- **CLD2 warm:** validated warm update bytes from alpha45.8 same-run consolidation.
- **zsync:** HTTP bytes from the validated same-run baseline.
- **casync:** HTTP `.caidx/.castr` extract with local seed directory.
- **OSTree:** HTTP pull of v2 after v1 was already pulled; no static-delta generated in the first baseline.
- **DVC:** new bytes added to a local DVC remote; this is not an HTTP byte benchmark.
- **OCI single/per-file:** deterministic tar.gz layer model; not a real Docker daemon pull.

## Conclusion

The results support a limited but useful claim: CLD2 is promising for cached update distribution with high reuse. The results do not support a universal superiority claim.

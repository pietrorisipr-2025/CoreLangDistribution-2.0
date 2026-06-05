# Alpha51.1 claim boundary

Safe claims:

- CLD2 can reduce warm-update transfer when a receiver already has a prior version and the next version reuses many chunks/objects.
- The benefit is workload-dependent.
- CLD2 is most useful for large, mutable artifacts with high reuse between releases.
- CLD2 is not a generic compression algorithm.
- CLD2 is not a universal replacement for rsync, zsync, casync, OSTree, DVC, Docker/OCI or CDN systems.
- Alpha51.1 measures artifact pairs; it does not replace production/cloud/CDN tests.

Unsafe claims:

- CLD2 always wins.
- CLD2 is a better compressor.
- Synthetic alpha51.1 results prove production performance.
- A win against file-level raw transfer automatically means a win against a well-compressed changed-file baseline.
- A local benchmark is the same as a real CDN deployment.

Key interpretation rule:

```text
Compare CLD2 against both raw file-level transfer and the best conventional compressed baseline.
```

If CLD2 only beats raw but is similar to `tar.zst changed-files`, classify it as compressed-baseline parity, not a strong universal win.

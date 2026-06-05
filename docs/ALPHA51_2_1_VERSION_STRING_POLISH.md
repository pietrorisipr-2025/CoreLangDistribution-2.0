# ALPHA51.2.1 version string polish

Alpha51.2 removed the legacy `alpha32` references, but one derived version string remained cosmetically malformed:

```text
malformed legacy alpha50.2-prefixed version string
```

Alpha51.2.1 replaces it with:

```text
2.0.0-alpha50.2
```

This is a metadata/reporting cleanup only. Benchmark values are not changed.


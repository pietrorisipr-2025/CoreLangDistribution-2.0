# Alpha51.2 metadata cleanup

Alpha51.2 is a reporting/metadata cleanup for alpha51.1.

It should be described as:

```text
CLD2 code baseline: 2.0.0-alpha50.2
Benchmark harness milestone: alpha51.2
```

It must not be described as a performance improvement.

## Why it exists

Alpha51.1 successfully produced a lightweight review ZIP and improved benchmark classification, but some generated result fields still showed the legacy string `legacy alpha32 marker`.

Alpha51.2 fixes that confusion in:

- scripts/docs where the legacy value was hardcoded;
- existing result folders if a result root is passed to the patch script;
- generated review ZIP metadata.

## Recommended next step

After alpha51.2, update public docs with the alpha51.1/51.2 finding:

- CLD2 is strongest for localized internal updates inside large artifacts.
- CLD2 is near-parity when a conventional compressed changed-file baseline is already optimal.
- CLD2 is bad-fit/near-parity for full entropy rewrites.

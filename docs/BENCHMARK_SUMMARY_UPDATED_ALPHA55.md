# CLD2 benchmark summary updated through alpha55

## Validated baseline picture

The alpha45.8 same-run comparison remains the core low-level benchmark anchor:

| Scenario | zsync HTTP bytes | CLD2 warm bytes | Reading |
|---|---:|---:|---|
| normal | 239,827 | 419 | CLD2 clearly lower transfer |
| high-entropy | 67,342,559 | 67,108,937 | roughly equal / adversarial |
| small-files | 3,515,724 | 2,147 | CLD2 clearly lower transfer |
| heavy-change | 27,078,879 | 26,845,855 | roughly equal / heavy-change |

## External/practical baselines through alpha49

- casync, OSTree, DVC and OCI/layering were explored as benchmark/positioning milestones.
- DVC and OCI metrics are not perfectly homogeneous with HTTP byte-transfer baselines.
- OSTree static deltas were not enabled in the first OSTree baseline.

## Real-artifact style tests through alpha51.2.1

Alpha51.1/51.2.1 added more realistic artifact-style cases and showed a more nuanced picture:

- CLD2 wins clearly when the update is localized inside a larger artifact.
- CLD2 can be near-parity against a strong “changed files only + compression” baseline.
- CLD2 is a bad fit when the whole payload is high-entropy or rewritten.

## Provider and showcase tests through alpha55

| Milestone | Result |
|---|---|
| alpha52.1 | Artifact provider demo PASS |
| alpha53 fixed-direct v2 | Provider trust policy PASS |
| alpha54.1 | Object-store-like provider demo PASS |
| alpha55 | Public examples/showcase PASS |

Alpha55 public showcase examples were favorable/high-reuse cases:

| Scenario | Classification | Reading |
|---|---|---|
| game_asset_patch | high-reuse-win | small warm transfer vs large v2 payload |
| model_profile_pack | high-reuse-win | meaningful warm-update savings |
| ci_artifact_bundle | high-reuse-win | strong cache/chunk reuse |

## Claim boundary

These are local synthetic/demonstration workloads. They are useful for documentation, GitHub positioning and devtool evaluation, but they are not cloud/CDN/S3 production benchmarks.

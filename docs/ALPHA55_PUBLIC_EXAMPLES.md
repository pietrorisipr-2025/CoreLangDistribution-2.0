# CLD2 alpha55 — Public examples

Alpha55 is intended to make CLD2 easier to show publicly.

The earlier benchmark work established the technical pattern: CLD2 is strongest when an update has high reuse between versions and weaker/near-parity when the new artifact is mostly rewritten or high entropy. Alpha55 turns that into approachable examples.

## Scenarios

### 1. `game_asset_patch`

A game-like asset tree with many asset files, manifests and metadata. Version 2 changes a subset of assets and adds a few new ones.

Use this to explain:

- patch distribution;
- user already has previous cache;
- many assets reused between releases.

### 2. `model_profile_pack`

A model/profile-like package made of binary shards plus small config files. Version 2 changes localized portions of selected shards.

Use this to explain:

- large artifact warm updates;
- chunk-level reuse inside large files;
- model/profile/domain-pack delivery.

### 3. `ci_artifact_bundle`

A CI/build output with binaries, logs, reports, documentation and metadata. Version 2 changes build outputs and a subset of generated artifacts.

Use this to explain:

- CI/CD artifact delivery;
- build cache reuse;
- auditable artifact installation.

## Output classification

The alpha55 runner writes per-scenario JSON and a summary CSV/Markdown report. It reports:

- full v2 tree bytes;
- warm fetch metrics extracted from CLD2 output when available;
- audit result;
- payload digest validation excluding `.cld2` install metadata;
- a simple reading/classification.

## Important limitation

These are generated local examples. They are not proof of cloud/CDN behavior and should not be presented as production performance claims.

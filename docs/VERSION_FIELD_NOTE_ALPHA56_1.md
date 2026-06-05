# CLD2 alpha56.1 — version field note

The public package version is:

```text
2.0.0-alpha50.2
```

Alpha56.1 is an integration/polish milestone for the GitHub candidate. It does not rename the core package to `2.0.0-alpha56.1`.

Some generated `.cldrepo` metadata may contain internal fields such as:

```json
"version": "2.0-alpha24"
```

Those fields are repository-format/schema lineage markers. They are not the public package version. The generated manifest also includes a `tool` field identifying the public tool version, for example:

```json
"tool": "CoreLangDistribution 2.0.0-alpha50.2"
```

Alpha56.1 cleaned legacy benchmark-result metadata that previously reported `legacy alpha32 marker` in benchmark JSON outputs.

# CLD2 alpha52 — Artifact Provider Specification

## Purpose

CLD2 can be used as a thin artifact provider by other tools that need to fetch and verify large, versioned artifacts.

The provider model is:

```text
Provider input:
- CLD2 repository path or URL
- local cache path
- install/output path
- expected digest, optional but strongly recommended
- digest mode: file-sha256 or tree-sha256

Provider output:
- ok/fail
- fetch command result
- audit-install command result
- actual digest
- expected digest
- bytes reported by CLD2 if available
- machine-readable JSON
```

## Security rule

The caller should not trust the storage backend.
The caller should trust only an expected digest obtained through a trusted/authenticated channel.

Correct sequence:

```text
expected digest is known/pinned/authenticated
        ↓
CLD2 fetch installs artifact
        ↓
CLD2 audit-install verifies CLD2 manifest consistency
        ↓
provider independently computes installed artifact digest
        ↓
artifact is accepted only if expected == actual
```

## Digest modes

### file-sha256

Use when the installed artifact is a single file.

### tree-sha256

Use when the installed artifact is a directory. The tree digest is deterministic:

```text
for each file sorted by POSIX relative path:
  path \0 size \0 sha256(file) \n
sha256(concatenated records)
```

This is not a replacement for CLD2 internal manifest verification. It is an external caller-facing digest.

## Non-goals

Alpha52 is not:

- a new CLD2 core release;
- a new compression algorithm;
- a proof of CDN/cloud performance;
- a replacement for a real package manager;
- an external security audit.

## Recommended public claim

```text
CLD2 can be used as an experimental warm-update artifact provider for large,
versioned, high-reuse artifacts. The recommended integration is a thin wrapper
that fetches through CLD2 and independently verifies the installed artifact
against a pinned expected digest.
```

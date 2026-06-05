# Alpha55 GitHub showcase notes

Suggested README addition:

```markdown
## Public examples

The `alpha55` examples generate three local artifact-shaped workloads:

- game asset patch
- model/profile pack update
- CI artifact bundle update

They are meant to show how CLD2 behaves as a warm-update artifact planner. They are local synthetic examples, not cloud/CDN benchmarks.
```

Suggested command:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\alpha55_run_public_examples.ps1 `
  -RepoRoot . `
  -OutRoot "C:\temp\cld2_alpha55_examples" `
  -SizeMiB 64
```

Suggested positioning:

- Good fit: large mutable artifacts with meaningful reuse.
- Bad fit: tiny static files, cold-only delivery, full rewrites, high entropy with no reuse.

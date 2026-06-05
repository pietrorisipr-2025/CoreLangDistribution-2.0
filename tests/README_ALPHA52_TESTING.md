# Alpha52 testing checklist

From the CLD2 repository root, after copying the alpha52 files into the repo:

```powershell
python .\scripts\smoke_test.py
python .\examples\small_demo\make_demo_data.py
python .\cld2.py pack .\examples\small_demo\release_v1 --out .\examples\small_demo\release_v1.cldrepo --release-id demo --release-seq 1 --force
python .\cld2.py pack .\examples\small_demo\release_v2 --out .\examples\small_demo\release_v2.cldrepo --release-id demo --release-seq 2 --force
python .\scripts\alpha52_artifact_fetch_verify.py --repo .\examples\small_demo\release_v2.cldrepo --install .\examples\small_demo\install_v2 --cache .\examples\small_demo\cache --digest-mode tree-sha256 --json-out .\examples\small_demo\alpha52_provider_result.json
```

The first run can be used without `--expected-sha256` to learn the digest.
A second run can pass `--expected-sha256 <digest>` and must return `ok=true`.

# CLD2 alpha54 — Optional MinIO notes

Alpha54 intentionally does not require MinIO for the first demo.

If a later alpha wants real object-store transport, use a clean MinIO data directory. Historical project notes recorded that the older `minio_alpha44_data` directory was corrupted; the later healthy data directory was `minio_alpha45_fresh_data`.

Recommended order for a future MinIO/S3 run:

1. Start MinIO with a fresh known-good data directory.
2. Upload `.cldrepo` artifacts under deterministic keys.
3. Use signed/pinned metadata carrying expected digest.
4. Fetch through CLD2/provider wrapper.
5. Re-verify digest after install.
6. Run alpha53 trust policy validation.

Do not claim cloud/CDN performance from a local MinIO test.

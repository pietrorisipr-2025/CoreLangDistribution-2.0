# CLD2 alpha50.2 — MIT license finalization

Created: `2026-06-03T18:58:47+00:00`

## What changed

Alpha50.2 finalizes the GitHub repository candidate by replacing the conservative license placeholder with the **MIT License**.

## Changes

- `LICENSE` now contains the standard MIT License text.
- `pyproject.toml` includes `license = { text = "MIT" }` when applicable.
- Public docs were updated to remove the previous “choose a license / review only” blocker.
- Manifest regenerated.
- Repository candidate ZIP regenerated.
- Release assets ZIP regenerated.

## Result

The repository is now much closer to public GitHub publication.

Remaining pre-publication checklist:

- Review author/copyright holder line in `LICENSE`.
- Decide repository name and GitHub description.
- Run CI once after upload to GitHub.
- Optionally add screenshots or benchmark images later.

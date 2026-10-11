---
name: ci-fix
description: Diagnose and fix a failed Trackmind GitHub Actions run (release.yml on the Windows runner — PyInstaller EXE, NSIS installer, Stream Deck plugin build, Release upload). Use when a build/release "failed" or a Release is missing assets.
argument-hint: "[run id]"
---

# Fix a failed Trackmind build

1. **Read the failure with the `ci-log-reader` agent** (`repo=Liberty-Baptist-Church-Yuma/Trackmind`, plus the run ID if given). It returns the root error only, so the Windows build log stays out of the chat.
2. Known traps:
   - The Stream Deck plugin must be **built before it's validated** (`npm ci && npm run build` in `streamdeck/`).
   - `Trackmind.spec` bundles model files by exact name. A renamed `models/*.onnx` must be renamed there too, or the EXE silently falls back to CPU.
   - NSIS reads the version from `version.txt`. A leading `v` or a trailing newline breaks `APP_VERSION_4`.
   - The Release notes step reads the `## v<version> —` section of `CHANGELOG.md`. If it's missing, the Release silently gets a generic "see CHANGELOG" body instead of failing, so check the notes too.
3. Reproduce locally where possible (pytest, `npm run build`). The EXE and installer only build on Windows CI.
4. Fix at the root and commit `Fix CI: <what was wrong>` with no co-author line. Ask before pushing.
5. Re-release: if the tag's build failed before publishing anything, delete and re-push the same tag **only with the owner's OK** (`git push --delete origin v<x>` / `git tag -d v<x>`). Otherwise bump the patch version with `/ship`.
6. Report the quoted root error, the fix, and the green run plus Release link.

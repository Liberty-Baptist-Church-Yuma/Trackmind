# Trackmind

Auto camera tracking for church services: a Python app (pose detection → VISCA pan/tilt/zoom) with a web UI
(`ui/`), a Stream Deck plugin (`streamdeck/`), and a Windows installer. Runs on the vMix PC (RTX 2060).

## Standing rules (owner's "no-brainers")

- **Docs are always updated** with every user-visible change, in the same commit:
  `CHANGELOG.md` (the Release notes are built from it), `README.md`, `docs/API.md` for API changes, and
  `streamdeck/README.md` for plugin changes.
- **New features get Stream Deck support** (a key, or a setting on an existing key) unless it makes no sense.
  Say so when you skip it.
- **New behaviour that changes how tracking acts is toggleable** in Settings.
- **No co-author lines.** Never add `Co-Authored-By` or `Claude-Session` trailers; commits carry only the
  name of the person running the session. Commit as that user, with whatever `git config user.name` /
  `user.email` is already set on the machine. Don't hard-code a username, and don't author a commit as someone
  else. The repo lives in the Liberty-Baptist-Church-Yuma org, and more than one person commits to it.
- Ask before pushing. A pushed `v*` tag builds and publishes a Release.

## Versions and releases

| What | Where |
|---|---|
| App version | `version.txt` (e.g. `1.8.4`). CI, `BUILD_INSTALLER.bat` and the app (`autotrack.py`) read it |
| Stream Deck plugin | `streamdeck/package.json` `version` + `streamdeck/com.coder747.trackmind.sdPlugin/manifest.json` `Version` (4-part), bump only when the plugin changed |
| Release notes | The top `## vX.Y.Z — date` section of `CHANGELOG.md` |

Tag `vX.Y.Z` → `.github/workflows/release.yml` (Windows runner) builds the EXE (PyInstaller), the NSIS installer
and the Stream Deck plugin, then cuts the GitHub Release. Use `/ship`. If CI fails, use `/ci-fix`.

## Tests

`python -m pytest tests -q` (uses the camera simulator in `tests/camsim.py`, no camera needed).

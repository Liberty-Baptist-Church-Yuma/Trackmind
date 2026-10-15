---
name: ship
description: Release Trackmind — bump version.txt (and the Stream Deck plugin version if it changed), write the CHANGELOG entry, update docs, commit as the user with no co-author, tag vX.Y.Z, and watch the Windows build until the Release is published. Use for "commit and push", "version shift", "push/build", "release", "bump".
argument-hint: "[version | patch | minor]"
---

# Ship Trackmind

## 1. Version

- The default bump is a patch (`1.8.4` → `1.8.5`). Use minor for big features, or whatever version the owner names.
- `git tag -l 'v<new>'` must be empty.
- If anything under `streamdeck/` changed since the last tag (`git diff --stat <last tag> -- streamdeck/`), also bump the plugin version in `streamdeck/package.json` (`1.2.0` → `1.3.0`) and in `manifest.json` `Version` (`1.3.0.0`). Then run `npm ci && npm run build` in `streamdeck/` so the built plugin matches.

## 2. Docs and checks. All required.

- **CHANGELOG.md**: add a new top section `## v<new> — YYYY-MM-DD` in the existing style: `###` topic headings, and bullets that start with a **bold plain-English sentence** about what the user notices, followed by why or how. It's built into the Release notes.
- README.md, `docs/API.md` and `streamdeck/README.md` cover any new feature, setting or endpoint.
- New feature without a Stream Deck key → add one, or tell the owner why not.
- `python -m pytest tests -q` passes. Report failures instead of tagging.

## 3. Commit

- Commit **all** outstanding changes unless the owner held something back.
- Use one descriptive subject ending in `; bump to v<new>` (repo style), and a body explaining the why.
- **No `Co-Authored-By` and no `Claude-Session` trailer.** That's the point of the rule. Commit as the user running
  the session, using the `user.name` / `user.email` git already has. Don't hard-code a username. If git has no
  identity set, ask the owner for theirs instead of guessing one.

## 4. Push, tag, watch. Ask first.

Show the commits and ask "Push and release now?". On yes:

```bash
git push origin HEAD && git tag v<new> && git push origin v<new>
gh run watch "$(gh run list --workflow release.yml --limit 1 --json databaseId --jq '.[0].databaseId')" --exit-status
gh release view v<new> --json assets --jq '.assets[].name'   # expect setup .exe and .streamDeckPlugin
```

If it's red, use `/ci-fix`. Don't announce success until the Release has its assets.

## 5. Auto-update check

The in-app updater (`autotrack.py`) reads `api.github.com/repos/<GITHUB_REPO>/releases/latest` and downloads the asset containing `setup` and ending in `.exe`. `/releases/latest` **skips pre-releases and drafts**: v1.8.3 was published as a pre-release, so installed copies stayed on v1.8.2. After releasing, confirm the updater will see it:

```bash
curl -sL https://api.github.com/repos/coder747-8i/Trackmind/releases/latest | python3 -c "import json,sys;d=json.load(sys.stdin);print(d['tag_name'],[a['name'] for a in d['assets']])"
```

The tag must be the new version, with a `Trackmind_Setup_v….exe` asset. If it was meant to be a stable release but shows as pre-release, ask the owner, then `gh release edit v<new> --prerelease=false --latest`. (`GITHUB_REPO` still names the old `coder747-8i/Trackmind`. GitHub's redirect to the org repo works, but update it if the redirect ever stops.)

## 6. Report

Send 3–5 lines: the version, what's in it, the Release link, the plugin version if bumped, and anything not verified (e.g. "not tested on the vMix PC").

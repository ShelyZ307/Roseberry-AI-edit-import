# GitHub Update Workflow

Phase 1 uses:

- GitHub Releases ZIPs
- `update_manifest.json`
- manual update scripts
- stable Resolve launchers

No in-app updater and no auto-update on launch are included yet.

## Create The GitHub Repo

1. Create a repo, for example:

```text
roseberry-ai-edit-import
```

2. Copy this folder into the repo.
3. Check `.gitignore`.
4. Confirm no local paths, logs, `.venv`, or caches are committed.
5. Commit and push.

## Build Release ZIPs

Run:

```bash
python3 packaging/build_release_zip.py
```

This creates platform release ZIPs in `dist/`.

## Publish Release

1. Create GitHub release `v0.2.0`.
2. Upload the Mac ZIP.
3. Upload the Windows ZIP.
4. Compute SHA256 hashes.
5. Create a real `update_manifest.json` from `update_manifest.example.json`.
6. Confirm release URLs and SHA values in `update_manifest.json`.
7. Upload `update_manifest.json` as a release asset.

## Migration From Current ZIP Installs

Recommended migration:

1. Run the new setup script once.
2. Run the new launcher installer once.
3. Keep the old ZIP package as backup.
4. Use manual updater scripts for future updates.

This is safer than patching the old ZIP install in place.

## What Remains Manual In Phase 1

- User manually runs update script.
- Release author manually creates GitHub release.
- Release author manually updates manifest SHA values.
- No in-app update button.
- No auto-check on startup.

## Phase 2 Ideas

- In-app Check for Updates button.
- Version display in the main UI.
- Update notification only, without auto-install.
- Signed releases.
- Cleaner installer.

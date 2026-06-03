# Roseberry AI Edit Import

Roseberry AI Edit Import is a local DaVinci Resolve tool for validating and importing AI-generated edit segments.

Version: `0.2.0`

This repository is prepared for a Phase 1 GitHub update workflow:

- GitHub Releases ZIPs
- `update_manifest.json`
- manual update scripts
- stable DaVinci Resolve launchers

No in-app update button and no auto-update on launch are included in Phase 1.

## Current Features

- JSON segment input.
- Temporary Gemini `.xlsx` input.
- Excel `Reason for Cut` header alias.
- Excel footer/source row handling.
- Grouped validation warnings.
- Cleaned segment review table.
- `Create Edited Timeline`-only `01:00:00` Resolve timeline-label normalization.
- `Add Markers Only` unchanged.

## Install Locations

Mac app install:

```text
~/Roseberry/AI Edit Import
```

Windows app install:

```text
%LOCALAPPDATA%\Roseberry\AI Edit Import
```

Mac Resolve launcher:

```text
~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility/Roseberry AI Tools/Roseberry AI Edit Import.py
```

Windows Resolve launcher:

```text
%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility\Roseberry AI Tools\Roseberry AI Edit Import.py
```

## First-Time Setup

Mac:

```bash
./scripts/setup_mac.sh
./scripts/install_launcher_mac.sh
```

Windows:

```bat
scripts\setup_windows.bat
scripts\install_launcher_windows.bat
```

## Manual Update

Mac:

```bash
./scripts/update_mac.sh
```

Windows:

```bat
scripts\update_windows.bat
```

The updater scripts point at this private repository's release manifest. Because
the repository is private, users need GitHub access and must provide
`GITHUB_TOKEN` or `GH_TOKEN` in their environment before running the updater.

## GitHub Release Flow

1. Update `VERSION`.
2. Update `CHANGELOG.md`.
3. Build release ZIPs with `packaging/build_release_zip.py`.
4. Compute SHA256 hashes.
5. Publish GitHub release assets.
6. Publish/update `update_manifest.json`.

See `docs/Troubleshooting.md` and the installation docs for more detail.

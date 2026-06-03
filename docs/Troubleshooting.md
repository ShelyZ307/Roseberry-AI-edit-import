# Troubleshooting

## Update Manifest SHA Check

Before publishing a new release, make sure the release-specific manifest contains the final release ZIP SHA256 values. For this `0.2.0` release, the real manifest is:

```text
update_manifest.json
```

## Python Not Found

Install Python 3.10 or newer, then rerun setup.

## PySide6 Install Fails

Rerun setup with a working internet connection. The app requires:

```text
PySide6>=6.7
```

## App Does Not Launch From Resolve

Check launcher logs:

Mac:

```text
~/Desktop/roseberry_ai_tools_desktop_launcher_debug.txt
```

Windows:

```text
%USERPROFILE%\Desktop\roseberry_ai_tools_desktop_launcher_debug.txt
```

Check app logs:

Mac:

```text
~/Library/Logs/Roseberry AI Tools/roseberry_ai_tools_desktop_debug.txt
```

Windows:

```text
%LOCALAPPDATA%\Roseberry\AI Edit Import\logs\roseberry_ai_tools_desktop_debug.txt
```

## Update Fails

The updater creates a backup before replacing files. Use the backup folder in the install directory to roll back manually.

The update manifest and release ZIPs are public. GitHub authentication is not
required for normal updates.

## Timing Looks Wrong

Compare Gemini/JSON time to `Source Time`, not `Timeline Time`.

Timeline Time includes review gaps in the generated edited timeline.

#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import shutil
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
DIST = ROOT / "dist"

EXCLUDE_PARTS = {
    ".venv",
    "__pycache__",
    ".git",
    "dist",
}
EXCLUDE_SUFFIXES = {
    ".pyc",
    ".log",
}
EXCLUDE_NAMES = {
    ".DS_Store",
}


def should_include(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    if any(part in EXCLUDE_PARTS for part in rel.parts):
        return False
    if path.name in EXCLUDE_NAMES:
        return False
    if path.name.startswith("._"):
        return False
    if path.suffix in EXCLUDE_SUFFIXES:
        return False
    return True


def add_tree(zip_file: zipfile.ZipFile, source: Path, target_prefix: str) -> None:
    for path in sorted(source.rglob("*")):
        if path.is_file() and should_include(path):
            arcname = Path(target_prefix) / path.relative_to(source)
            zip_file.write(path, arcname.as_posix())


def build(platform: str) -> Path:
    name = f"Roseberry_AI_Edit_Import_{platform}_v{VERSION}.zip"
    output = DIST / name
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        add_tree(zf, ROOT / "roseberry_ai_edit_import", "roseberry_ai_edit_import")
        add_tree(zf, ROOT / "docs", "docs")
        add_tree(zf, ROOT / "samples", "samples")
        zf.write(ROOT / "VERSION", "VERSION")
        zf.write(ROOT / "CHANGELOG.md", "CHANGELOG.md")
        zf.write(ROOT / "README.md", "README.md")
        launcher = ROOT / "launchers" / platform / "Roseberry AI Edit Import.py"
        zf.write(launcher, "Resolve Launcher/Roseberry AI Edit Import.py")
        setup_script = ROOT / "scripts" / f"setup_{platform}.sh" if platform == "mac" else ROOT / "scripts" / "setup_windows.bat"
        install_script = ROOT / "scripts" / f"install_launcher_{platform}.sh" if platform == "mac" else ROOT / "scripts" / "install_launcher_windows.bat"
        update_script = ROOT / "scripts" / f"update_{platform}.sh" if platform == "mac" else ROOT / "scripts" / "update_windows.bat"
        zf.write(setup_script, setup_script.name)
        zf.write(install_script, install_script.name)
        zf.write(update_script, update_script.name)
        if platform == "windows":
            zf.write(ROOT / "scripts" / "windows_copy_excludes.txt", "windows_copy_excludes.txt")
    return output


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    if DIST.exists():
        shutil.rmtree(DIST)
    DIST.mkdir(parents=True)
    for platform in ("mac", "windows"):
        output = build(platform)
        print(f"{output}")
        print(f"sha256={sha256(output)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


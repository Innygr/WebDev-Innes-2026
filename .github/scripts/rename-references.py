#!/usr/bin/env python3
"""Rename one repository file and update textual references to it."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[2]
SKIP_DIRS = {".git", ".github", "node_modules", "__pycache__", ".venv"}


def fail(message: str) -> None:
    raise SystemExit(f"ERROR: {message}")


def main() -> None:
    if len(sys.argv) != 3:
        fail("Usage: rename-references.py <repository-relative-file> <new-filename>")

    raw_path, raw_name = sys.argv[1:3]
    source = (ROOT / raw_path).resolve()
    if not source.is_relative_to(ROOT.resolve()) or not source.is_file():
        fail("The selected file does not exist inside this repository.")

    new_name = Path(raw_name).name.strip()
    if not new_name or new_name in {".", ".."} or "/" in raw_name or "\\" in raw_name:
        fail("Enter a filename only, not a folder path.")
    if new_name in {".git", ".github"}:
        fail("That filename is not allowed.")
    if not Path(new_name).suffix:
        new_name += source.suffix

    target = source.with_name(new_name)
    if target == source:
        print("The file already has that name; nothing to do.")
        return
    if target.exists():
        fail(f"The destination already exists: {target.relative_to(ROOT)}")

    old_rel = source.relative_to(ROOT).as_posix()
    new_rel = target.relative_to(ROOT).as_posix()
    old_name = source.name

    # Rename first; if a later text file cannot be decoded, it is simply skipped.
    target.parent.mkdir(parents=True, exist_ok=True)
    os.rename(source, target)

    basename_is_unique = sum(
        1 for p in ROOT.rglob(old_name)
        if p.is_file() and not any(part in SKIP_DIRS for part in p.relative_to(ROOT).parts)
    ) == 0
    replacements = [
        (quote(old_rel, safe="/"), quote(new_rel, safe="/")),
        (old_rel, new_rel),
    ]
    if basename_is_unique:
        replacements.append((old_name, new_name))

    updated = []
    for file in ROOT.rglob("*"):
        if not file.is_file() or file == target:
            continue
        rel_parts = file.relative_to(ROOT).parts
        if any(part in SKIP_DIRS for part in rel_parts):
            continue
        try:
            original = file.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue

        revised = original
        for old, new in replacements:
            revised = revised.replace(old, new)
        if revised != original:
            file.write_text(revised, encoding="utf-8", newline="")
            updated.append(file.relative_to(ROOT).as_posix())

    print(f"Renamed: {old_rel} -> {new_rel}")
    print(f"Updated references in {len(updated)} file(s).")
    for item in updated:
        print(f"Updated: {item}")
    if not updated:
        print("No textual references needed updating.")


if __name__ == "__main__":
    main()

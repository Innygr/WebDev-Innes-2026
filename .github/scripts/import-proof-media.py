#!/usr/bin/env python3
"""Unpack repository ZIPs and register extracted images/videos in images.html."""
from __future__ import annotations

import html
import os
import re
import subprocess
import zipfile
from PIL import Image, ImageOps, UnidentifiedImageError
import pillow_avif  # Registers AVIF decoding with Pillow.
import pillow_heif  # Registers HEIC/HEIF decoding with Pillow.
import rawpy  # Decodes supported camera RAW formats through LibRaw.
pillow_heif.register_heif_opener()
from pathlib import Path, PurePosixPath
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[2]
PROOF_HUB = ROOT / "images.html"
START = "<!-- AUTO-PROOF-GALLERY:START -->"
END = "<!-- AUTO-PROOF-GALLERY:END -->"
RAW_EXTS = {".3fr", ".ari", ".arw", ".bay", ".cap", ".cr2", ".cr3", ".dcs", ".dcr", ".dng", ".drf", ".eip", ".erf", ".fff", ".gpr", ".iiq", ".k25", ".kdc", ".mdc", ".mef", ".mos", ".mrw", ".nef", ".nrw", ".obm", ".orf", ".pef", ".ptx", ".pxn", ".r3d", ".raf", ".raw", ".rwl", ".rw2", ".rwz", ".sr2", ".srf", ".srw", ".x3f"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".svg", ".bmp", ".tif", ".tiff", ".heic", ".heif"} | RAW_EXTS
RASTER_IMAGE_EXTS = IMAGE_EXTS - {".svg"}
VIDEO_EXTS = {".mp4", ".webm", ".mov", ".m4v", ".ogv", ".mkv", ".avi", ".flv", ".wmv", ".mpeg", ".mpg", ".mts", ".3gp"}
SKIP_DIRS = {".git", ".github", "__pycache__"}
MAX_MEMBER_BYTES = 250 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024


def safe_extract(archive: Path) -> list[Path]:
    """Extract without allowing traversal, symlinks, or oversized ZIP bombs."""
    extracted: list[Path] = []
    total = 0
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            if info.file_size > MAX_MEMBER_BYTES:
                raise ValueError(f"ZIP member too large: {info.filename}")
            total += info.file_size
            if total > MAX_TOTAL_BYTES:
                raise ValueError("ZIP expands beyond the 1 GiB safety limit")
            name = PurePosixPath(info.filename)
            if name.is_absolute() or ".." in name.parts:
                raise ValueError(f"Unsafe ZIP path: {info.filename}")
            # Refuse Unix symlinks; they can redirect later writes.
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise ValueError(f"ZIP symlink not allowed: {info.filename}")
            target = (archive.parent / Path(*name.parts)).resolve()
            if not target.is_relative_to(ROOT.resolve()):
                raise ValueError(f"ZIP path escapes repository: {info.filename}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as source, target.open("wb") as destination:
                while chunk := source.read(1024 * 1024):
                    destination.write(chunk)
            extracted.append(target)
    return extracted


def unpack_all_zips() -> list[Path]:
    extracted_media: list[Path] = []
    # Repeat to also process ZIP files that were themselves inside another ZIP.
    while True:
        archives = sorted(
            p for p in ROOT.rglob("*")
            if p.is_file() and p.suffix.lower() == ".zip"
            and not any(part in SKIP_DIRS for part in p.relative_to(ROOT).parts)
        )
        if not archives:
            break
        progress = False
        for archive in archives:
            try:
                files = safe_extract(archive)
            except (zipfile.BadZipFile, OSError, ValueError) as exc:
                print(f"ERROR: keeping {archive.relative_to(ROOT)}: {exc}")
                continue
            extracted_media.extend(
                p for p in files if p.suffix.lower() in IMAGE_EXTS | VIDEO_EXTS
            )
            archive.unlink()
            print(f"Extracted and removed: {archive.relative_to(ROOT)}")
            progress = True
        if not progress:
            break
    return extracted_media


def move_uploaded_media(extracted: list[Path]) -> list[Path]:
    """Move newly uploaded and ZIP-extracted media into videos/ and fix references."""
    videos_dir = ROOT / "videos"
    videos_dir.mkdir(exist_ok=True)
    candidates = set(extracted)

    # Find media files added or changed by the push, without relocating the
    # repository's existing image/video assets on every workflow run.
    base = os.environ.get("UPLOAD_BASE_SHA", "")
    if base and set(base) != {"0"}:
        try:
            changed = subprocess.check_output(
                ["git", "diff", "--name-only", "--diff-filter=ACMRT", f"{base}..HEAD"],
                cwd=ROOT, text=True
            ).splitlines()
            candidates.update(
                ROOT / name for name in changed
                if (ROOT / name).is_file()
                and (ROOT / name).suffix.lower() in IMAGE_EXTS | VIDEO_EXTS
            )
        except subprocess.CalledProcessError as exc:
            print(f"WARNING: could not identify changed uploads: {exc}")

    moved: list[Path] = []
    replacements: list[tuple[str, str]] = []
    for source in sorted(candidates):
        if not source.exists() or not source.is_file():
            continue
        if source.parent == videos_dir or source.parent == ROOT / ".github":
            moved.append(source)
            continue

        original_rel = source.relative_to(ROOT).as_posix()
        destination = videos_dir / source.name
        counter = 2
        while destination.exists() and destination.resolve() != source.resolve():
            destination = videos_dir / f"{source.stem}-{counter}{source.suffix}"
            counter += 1
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.replace(destination)
        new_rel = destination.relative_to(ROOT).as_posix()
        replacements.extend([
            (original_rel, new_rel),
            (quote(original_rel, safe="/"), quote(new_rel, safe="/")),
            (quote(original_rel, safe=""), quote(new_rel, safe="")),
        ])
        moved.append(destination)
        print(f"Moved upload: {original_rel} -> {new_rel}")

    # Keep existing page/workflow references working after the move.
    if replacements:
        text_suffixes = {
            ".html", ".htm", ".css", ".js", ".mjs", ".cjs", ".json",
            ".xml", ".txt", ".md", ".yml", ".yaml", ".py", ".svg"
        }
        for file in ROOT.rglob("*"):
            if (not file.is_file()
                or file.suffix.lower() not in text_suffixes
                or any(part in SKIP_DIRS for part in file.relative_to(ROOT).parts)):
                continue
            try:
                content = file.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            updated = content
            for old, new in replacements:
                updated = updated.replace(old, new)
            if updated != content:
                file.write_text(updated, encoding="utf-8")
                print(f"Updated references in {file.relative_to(ROOT)}")

    return moved


def convert_videos(uploaded: list[Path]) -> list[Path]:
    """Convert only media identified as newly uploaded in this workflow run."""
    converted: list[Path] = []
    sources = sorted(
        p for p in set(uploaded)
        if p.is_file()
        and p.suffix.lower() in VIDEO_EXTS
        and not p.stem.lower().endswith("-web")
    )
    for source in sources:
        target = source.with_name(source.stem + "-web.mp4")
        temporary = target.with_name(target.stem + ".tmp.mp4")
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(source), "-map", "0:v:0", "-an",
            "-vf", "scale=w='min(1920,iw)':h='min(1080,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",
            "-c:v", "libx264", "-preset", "medium", "-crf", "21",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(temporary),
        ]
        try:
            subprocess.run(command, check=True)
            temporary.replace(target)
            converted.append(target)
            print(f"Converted (audio removed): {target.relative_to(ROOT)}")
        except (subprocess.CalledProcessError, OSError) as exc:
            temporary.unlink(missing_ok=True)
            print(f"ERROR: could not convert {source.relative_to(ROOT)}: {exc}")
    return converted



def convert_images(uploaded: list[Path]) -> list[Path]:
    """Convert only newly uploaded raster images; preserve originals and SVGs."""
    converted: list[Path] = []
    sources = sorted(
        p for p in set(uploaded)
        if p.is_file()
        and p.suffix.lower() in RASTER_IMAGE_EXTS
        and not p.stem.lower().endswith("-web")
    )
    for source in sources:
        target = source.with_name(source.stem + "-web.webp")
        temporary = target.with_name(target.stem + ".tmp.webp")
        try:
            if source.suffix.lower() in RAW_EXTS:
                with rawpy.imread(str(source)) as raw:
                    rgb = raw.postprocess(output_bps=8, use_camera_wb=True, no_auto_bright=False)
                with Image.fromarray(rgb) as raw_image:
                    raw_image.thumbnail((2560, 2560), Image.Resampling.LANCZOS)
                    raw_image.save(temporary, format="WEBP", quality=88, method=6)
                temporary.replace(target)
                converted.append(target)
                print(f"Developed RAW image to WebP: {target.relative_to(ROOT)}")
                continue

            with Image.open(source) as original:
                frames = []
                durations = []
                loop = original.info.get("loop", 0)
                animated = getattr(original, "is_animated", False)
                frame_count = getattr(original, "n_frames", 1)
                for index in range(frame_count):
                    original.seek(index)
                    frame = ImageOps.exif_transpose(original.copy())
                    frame.thumbnail((2560, 2560), Image.Resampling.LANCZOS)
                    has_alpha = "A" in frame.getbands() or "transparency" in frame.info
                    frames.append(frame.convert("RGBA" if has_alpha else "RGB"))
                    durations.append(original.info.get("duration", 100))
                options = {"format": "WEBP", "quality": 86, "method": 6}
                if animated and len(frames) > 1:
                    options.update({
                        "save_all": True,
                        "append_images": frames[1:],
                        "duration": durations,
                        "loop": loop,
                    })
                frames[0].save(temporary, **options)
                for frame in frames:
                    frame.close()
            temporary.replace(target)
            converted.append(target)
            print(f"Converted image to WebP: {target.relative_to(ROOT)}")
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            print(f"ERROR: could not convert image {source.relative_to(ROOT)}: {exc}")
    return converted


def display_name(path: Path) -> str:
    # Strip the extension and turn common filename separators into readable spaces.
    stem = re.sub(r"-web$", "", path.stem, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", re.sub(r"[_-]+", " ", stem)).strip().title() or path.name


def make_card(path: Path) -> str:
    rel = path.relative_to(ROOT).as_posix()
    url = quote(rel, safe="/")
    title = html.escape(display_name(path))
    safe_url = html.escape(url, quote=True)
    ext = path.suffix.lower()
    if ext in VIDEO_EXTS:
        kind, icon, label = "video", "▶", "Muted MP4"
        detail = "H.264 · Audio removed"
        media = (
            f'<video controls playsinline preload="none"><source src="./{safe_url}" '
            f'type="{ "video/webm" if ext == ".webm" else "video/ogg" if ext == ".ogv" else "video/mp4" }">'
            "Your browser does not support this video format.</video>"
        )
        action = f'<a href="./{safe_url}" target="_blank" rel="noopener">Open video file directly ↗</a>'
    else:
        kind, icon, label = "image", "▧", "Image"
        detail = "WebP · Optimized for web" if ext == ".webp" and path.stem.lower().endswith("-web") else f"{ext[1:].upper()} · Vector / source image"
        media = f'<img src="./{safe_url}" alt="{title}" loading="lazy">'
        action = f'<a href="./{safe_url}" target="_blank" rel="noopener">Open full-size image ↗</a>'
    search = html.escape((title + " " + path.name).lower(), quote=True)
    return f"""<details class="evidence auto-evidence" data-category="{kind}" data-search="{search}">
<summary><span class="file-icon" aria-hidden="true">{icon}</span><span class="file-name"><strong>{title}</strong><small>{label} · {detail}</small></span><span class="badge">{label}</span><span class="chevron" aria-hidden="true">⌄</span></summary>
<div class="evidence-content"><p>Optimized for browser playback; original preserved. Source file <code>{html.escape(path.name)}</code>.</p>{media}<div class="media-status" aria-live="polite"></div><div class="media-actions">{action}</div></div>
</details>"""


def update_proof_hub(files: list[Path]) -> None:
    if not files:
        print("No extracted image/video files to add.")
        return
    content = PROOF_HUB.read_text(encoding="utf-8")
    if START not in content or END not in content:
        raise RuntimeError("Proof Hub auto-gallery markers are missing.")
    start = content.index(START) + len(START)
    end = content.index(END, start)
    existing = content[start:end]
    known = set(re.findall(r'(?:src|href)="\./([^"]+)"', content))
    cards = []
    for path in sorted(set(files), key=lambda p: p.as_posix().lower()):
        rel = path.relative_to(ROOT).as_posix()
        if quote(rel, safe="/") in known or rel in known:
            continue
        cards.append(make_card(path))
    if not cards:
        print("Extracted media already appears in the Proof Hub.")
        return
    content = content[:start] + "\n" + "\n".join(cards) + "\n" + content[end:]
    PROOF_HUB.write_text(content, encoding="utf-8")
    print(f"Added {len(cards)} media item(s) to images.html.")


if __name__ == "__main__":
    media = unpack_all_zips()
    media = move_uploaded_media(media)
    converted_videos = convert_videos(media)
    converted_images = convert_images(media)
    # SVG stays vector; raster formats use optimized WebP copies.
    vector_images = [p for p in media if p.suffix.lower() == ".svg"]
    update_proof_hub(vector_images + converted_images + converted_videos)

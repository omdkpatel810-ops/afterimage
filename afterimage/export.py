"""Save what a backup still holds into a folder: leftover thumbnails, and full-size photos in Recently Deleted.

Each photo or video goes in a subfolder named after what became of it. A thumbnail gets its date taken and
place written in (see exif.py) and its file time set to when the photo was taken, so photo apps sort it
where it belongs. A spreadsheet (afterimage.csv) and a page to open in a browser (report.html) list
everything. The backup itself is only ever read.
"""

from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone, tzinfo
from pathlib import Path

from .backup import Backup, BackupError, BackupInfo
from .camera_roll import CameraRoll, Thumbnail
from .exif import add_exif, is_jpeg
from .library import (
    GONE,
    IN_LIBRARY,
    LABELS,
    RECENTLY_DELETED,
    UNKNOWN,
    Asset,
    Library,
    find_leftovers,
    recently_deleted_in_backup,
)

FULL_SIZE = "full size"
FOLDERS = {
    GONE: "only-thumbnails-left",
    RECENTLY_DELETED: "recently-deleted",
    IN_LIBRARY: "still-in-library",
    UNKNOWN: "deleted-or-in-icloud",
    FULL_SIZE: "recently-deleted-full-size",
}
SAVED_LABELS = {**LABELS, FULL_SIZE: "In Recently Deleted, full size"}
DESCRIPTION = "Thumbnail recovered from an iPhone backup by afterimage"
REPORT, TABLE = "report.html", "afterimage.csv"
_RESERVED = re.compile(r"(con|prn|aux|nul|com\d|lpt\d)(\..*)?", re.IGNORECASE)  # names Windows won't allow


class ExportError(Exception):
    """The export can't go where it was asked to; the message says why, in plain words."""


@dataclass(frozen=True)
class Saved:
    """One file written to the export folder."""

    file: str  # relative to the export folder, with "/" between parts
    status: str  # GONE, RECENTLY_DELETED, IN_LIBRARY, UNKNOWN or FULL_SIZE
    original: str  # where the photo belongs in the backup, e.g. Media/DCIM/100APPLE/IMG_0004.HEIC
    asset: Asset | None
    thumbnail_saved: datetime | None = None
    tagged: bool = False  # date taken or place written into the file


@dataclass
class Export:
    folder: Path
    info: BackupInfo
    saved: list[Saved] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)  # listed in the backup, but its file isn't there


def check_folder(folder: Path, backup: Path) -> None:
    """Refuse a folder inside the backup, or one that already has files in it."""
    target, inside = folder.resolve(), backup.resolve()
    if target == inside or inside in target.parents:
        raise ExportError(f"{folder} is inside the backup. Pick a folder outside it, so the backup stays as it was.")
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ExportError(f"{folder} already has files in it. Pick a new or empty folder, so nothing is overwritten.")


def export(backup: Backup, roll: CameraRoll, library: Library | None, folder: Path, tz: tzinfo | None = None) -> Export:
    """Save every leftover thumbnail and every full-size photo still in Recently Deleted to `folder`."""
    from .report import render  # the report imports this module's labels

    check_folder(folder, backup.path)
    result = Export(folder, backup.info)
    names = _Names()
    folder.mkdir(parents=True, exist_ok=True)

    for leftover in find_leftovers(roll, library):
        best = _largest(backup, leftover.thumbnails)
        if best is None:
            result.missing.append(leftover.original)
            continue
        thumbnail = best
        asset = leftover.asset
        try:
            data = backup.read(thumbnail.file)
        except BackupError:
            result.missing.append(leftover.original)
            continue
        tagged = add_exif(
            data,
            taken=asset.taken if asset else None,
            place=asset.place if asset else None,
            description=DESCRIPTION,
        )
        extension = ".jpg" if is_jpeg(data) else _extension(thumbnail.file.name)
        name = names.claim(FOLDERS[leftover.status], f"{_stem(leftover.original, asset)} (thumbnail){extension}")
        _write(folder / name, tagged or data, (asset.taken if asset else None) or leftover.thumbnail_saved)
        result.saved.append(
            Saved(name, leftover.status, leftover.original, asset, leftover.thumbnail_saved, tagged is not None)
        )

    for asset in recently_deleted_in_backup(roll, library):
        entry = roll.by_path[asset.path]
        if backup.locate(entry) is None:
            result.missing.append(asset.path)
            continue
        name = names.claim(FOLDERS[FULL_SIZE], asset.path.rpartition("/")[2])
        target = folder / name
        target.parent.mkdir(exist_ok=True)
        try:
            backup.copy(entry, target)
        except BackupError:
            result.missing.append(asset.path)
            continue
        _set_time(target, asset.taken)
        result.saved.append(Saved(name, FULL_SIZE, asset.path, asset))

    _write_table(folder / TABLE, result.saved)
    (folder / REPORT).write_text(render(result, tz), encoding="utf-8")
    return result


def _largest(backup: Backup, thumbnails: tuple[Thumbnail, ...]) -> Thumbnail | None:
    """The biggest of a photo's thumbnails that's really in the backup folder: usually the sharpest."""
    found = [(t, path) for t in thumbnails if (path := backup.locate(t.file)) is not None]
    best = max(found, key=lambda pair: pair[1].stat().st_size, default=None)
    return best[0] if best else None


def _stem(original: str, asset: Asset | None) -> str:
    """A photo's name without its extension, preferring the name it had when it was taken."""
    name = (asset.original_name if asset else None) or original.rpartition("/")[2]
    stem, dot, _ = name.rpartition(".")
    return stem if dot and stem else name


def _extension(name: str) -> str:
    stem, dot, extension = name.rpartition(".")
    return f".{extension.lower()}" if dot and stem else ""


def safe_name(name: str, limit: int = 120) -> str:
    """A file name that works on Windows, macOS and Linux, made from a name the backup supplied."""
    cleaned = re.sub(r"[^\w .()+-]+", "_", name).strip(" .") or "photo"
    stem, dot, extension = cleaned.rpartition(".")
    if not (dot and stem):
        stem, extension = cleaned, ""
    if _RESERVED.fullmatch(cleaned):
        stem = "_" + stem
    extension = extension[:16]
    stem = stem[: max(1, limit - len(extension) - 1)].rstrip(" .") or "photo"
    return f"{stem}.{extension}" if extension else stem


class _Names:
    """Hands out file names, adding " (2)", " (3)"... so no two files share one (ignoring case)."""

    def __init__(self) -> None:
        self.taken: set[tuple[str, str]] = set()

    def claim(self, folder: str, name: str) -> str:
        base = safe_name(name)
        stem, dot, extension = base.rpartition(".")
        if not (dot and stem):
            stem, extension = base, ""
        candidate, n = base, 1
        while (folder, candidate.casefold()) in self.taken:
            n += 1
            candidate = f"{stem} ({n}).{extension}" if extension else f"{stem} ({n})"
        self.taken.add((folder, candidate.casefold()))
        return f"{folder}/{candidate}"


def _write(path: Path, data: bytes, when: datetime | None) -> None:
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(data)
    _set_time(path, when)


def _set_time(path: Path, when: datetime | None) -> None:
    """Set a file's modified time, which photo apps use when a picture has no date of its own."""
    if when is None:
        return
    try:
        stamp = when.timestamp()
        os.utime(path, (stamp, stamp))
    except (OverflowError, OSError, ValueError):  # dates some systems can't store; the file is still fine
        pass


def _iso(moment: datetime | None) -> str:
    return "" if moment is None else moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _write_table(path: Path, saved: list[Saved]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "file",
                "what became of it",
                "path in backup",
                "kind",
                "taken (UTC)",
                "latitude",
                "longitude",
                "deleted (UTC)",
                "hidden",
                "first named",
                "thumbnail saved (UTC)",
                "date or place written in",
            ]
        )
        for item in saved:
            asset = item.asset
            place = asset.place if asset and asset.place else None
            writer.writerow(
                [
                    item.file,
                    SAVED_LABELS[item.status],
                    item.original,
                    (asset.kind if asset else None) or "",
                    _iso(asset.taken if asset else None),
                    f"{place[0]:.6f}" if place else "",
                    f"{place[1]:.6f}" if place else "",
                    _iso(asset.trashed_on if asset else None),
                    "yes" if asset and asset.hidden else "",
                    (asset.original_name if asset else None) or "",
                    _iso(item.thumbnail_saved),
                    "yes" if item.tagged else "",
                ]
            )

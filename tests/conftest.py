"""Fake iPhone backups, built in code, so the tests never need a real phone or real photos."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from afterimage.library import CORE_DATA_EPOCH  # noqa: F401  (tests import it from here)
from afterimage.sample import Encryption, Row, SampleBackup, encrypt, mbfile, photos_database  # noqa: F401

WHEN = datetime(2026, 10, 20, 21, 14, tzinfo=timezone.utc)
UDID = "00008110-001A2B3C4D5E801E"
CAMERA_ROLL = "CameraRollDomain"
# The smallest bytes that start and end like a JPEG. Nothing in these tests decodes images.
JPEG = bytes.fromhex("ffd8ffe000104a464946000101") + bytes(20) + bytes.fromhex("ffd9")
HEIC = bytes.fromhex("0000001866747970686569630000000068656963") + bytes(1000)
MOV = bytes.fromhex("00000014667479707174202000000000") + bytes(3000)


ICLOUD = "PhotoData/CPLAssets/group349/6F1E2D3C-4B5A-4978-8695-A4B3C2D1E0F9.JPG"
PASSWORD = "correct horse battery staple"


def utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=timezone.utc)


class FakeBackup(SampleBackup):
    """A sample backup of "Om's iPhone", as the tests expect it."""

    def __init__(self, parent: Path, udid: str = UDID, device: str | None = "Om's iPhone", **kwargs) -> None:
        kwargs.setdefault("when", WHEN)
        super().__init__(parent, udid, device, **kwargs)


LIBRARY = [
    Row("DCIM/100APPLE", "IMG_0001.HEIC", taken=utc(2026, 10, 18, 18, 2), place=(49.8951, -97.1384)),
    Row("DCIM/100APPLE", "IMG_0002.JPG", taken=utc(2026, 10, 18, 19, 30), favorite=True),
    Row("DCIM/100APPLE", "IMG_0003.MOV", kind=1, taken=utc(2026, 10, 19, 1, 10), trashed_on=utc(2026, 10, 19, 22, 0)),
    # IMG_0004.HEIC has no row: it was deleted for good, and only its thumbnails are left.
    Row("DCIM/100APPLE", "IMG_0005.HEIC", taken=utc(2026, 10, 17, 15, 45), trashed_on=utc(2026, 10, 20, 12, 0)),
    Row(
        ICLOUD.rpartition("/")[0],
        ICLOUD.rpartition("/")[2],
        taken=utc(2025, 7, 1, 16, 20),
        place=(51.0447, -114.0719),
        hidden=True,
        original_name="IMG_4821.JPG",  # iCloud files get new names; the database keeps the camera's
    ),
]


def add_camera_roll(
    fake: FakeBackup, library: list[Row] | None = LIBRARY, keep_in_log: bool = True, picture: bytes = JPEG
) -> FakeBackup:
    """A camera roll with its Photos library. Three photos and videos are in the backup with their thumbnails.

    Three more have thumbnails but no original: IMG_0004 was deleted for good, IMG_0005 is in Recently
    Deleted, and the iCloud one is still in the library. IMG_0003 is in Recently Deleted but still here.
    """
    fake.folder(CAMERA_ROLL, "Media").folder(CAMERA_ROLL, "Media/DCIM").folder(CAMERA_ROLL, "Media/DCIM/100APPLE")
    fake.file(CAMERA_ROLL, "Media/DCIM/100APPLE/IMG_0001.HEIC", HEIC)
    fake.file(CAMERA_ROLL, "Media/DCIM/100APPLE/IMG_0002.JPG", JPEG * 40)
    fake.file(CAMERA_ROLL, "Media/DCIM/100APPLE/IMG_0002.AAE", b"<plist>edit settings</plist>")
    fake.file(CAMERA_ROLL, "Media/DCIM/100APPLE/IMG_0003.MOV", MOV)
    previews = {
        "DCIM/100APPLE/IMG_0001.HEIC": ["5005", "5003"],
        "DCIM/100APPLE/IMG_0002.JPG": ["5005"],
        "DCIM/100APPLE/IMG_0003.MOV": ["5005"],
        "DCIM/100APPLE/IMG_0004.HEIC": ["5005", "5003"],
        "DCIM/100APPLE/IMG_0005.HEIC": ["5005"],
        ICLOUD: ["5003"],
    }
    for original, variants in previews.items():
        for variant in variants:
            fake.file(CAMERA_ROLL, f"Media/PhotoData/Thumbnails/V2/{original}/{variant}.JPG", picture)
    fake.file(CAMERA_ROLL, "Media/PhotoData/Thumbnails/V2/thumbnailConfiguration", b"bplist00")
    fake.file(CAMERA_ROLL, "Media/PhotoData/Thumbnails/3303.ithmb", bytes(512))
    if library is not None:
        for name, data in photos_database(library, keep_in_log=keep_in_log).items():
            fake.file(CAMERA_ROLL, f"Media/PhotoData/{name}", data)
    fake.file(CAMERA_ROLL, "Media/PhotoData/Mutations/DCIM/100APPLE/IMG_0002/Adjustments/FullSizeRender.jpg", JPEG)
    fake.file("HomeDomain", "Library/SMS/sms.db", b"SQLite format 3\x00")
    fake.file("AppDomain-com.example.notes", "Documents/notes.json", b"{}")
    return fake


def snapshot(folder: Path) -> dict[str, tuple[str | None, int]]:
    """Every file's contents and every file's and folder's modified time, to prove nothing was written.

    Folder times matter too: a file created and then deleted (say, an SQLite journal) still changes them.
    """
    state: dict[str, tuple[str | None, int]] = {".": (None, folder.stat().st_mtime_ns)}
    for path in sorted(folder.rglob("*")):
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        state[path.relative_to(folder).as_posix()] = (digest, path.stat().st_mtime_ns)
    return state


@pytest.fixture
def backups_dir(tmp_path: Path) -> Path:
    folder = tmp_path / "Backup"
    folder.mkdir()
    return folder


@pytest.fixture
def phone(backups_dir: Path) -> Path:
    """A finished, unencrypted backup of "Om's iPhone" with a camera roll."""
    return add_camera_roll(FakeBackup(backups_dir)).build()

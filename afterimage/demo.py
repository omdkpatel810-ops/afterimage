"""A made-up backup to try afterimage on, with no iPhone and no real photos: `afterimage demo`.

It has the same layout as a real one and a small camera roll: three photos and videos, plus thumbnails of
three more whose originals aren't there (one deleted for good, one in Recently Deleted, one kept in iCloud),
and a photo in Recently Deleted that's still there at full size. The pictures are simple drawings.
"""

from __future__ import annotations

from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path

from .camera_roll import DOMAIN
from .sample import Row, SampleBackup, photos_database

WHEN = datetime(2026, 10, 20, 21, 14, tzinfo=timezone.utc)
ICLOUD = "PhotoData/CPLAssets/group349/6F1E2D3C-4B5A-4978-8695-A4B3C2D1E0F9.JPG"
# Bytes that start like a HEIC photo and a QuickTime video. Nothing ever plays or decodes them.
HEIC = bytes.fromhex("0000001866747970686569630000000068656963") + bytes(2000)
MOV = bytes.fromhex("00000014667479707174202000000000") + bytes(6000)


def utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=timezone.utc)


LIBRARY = [
    Row("DCIM/100APPLE", "IMG_0001.HEIC", taken=utc(2026, 10, 18, 18, 2), place=(49.8951, -97.1384)),
    Row(
        "DCIM/100APPLE",
        "IMG_0002.JPG",
        taken=utc(2026, 10, 19, 15, 30),
        place=(51.4164, -116.2177),
        trashed_on=utc(2026, 10, 19, 22, 0),
    ),
    Row("DCIM/100APPLE", "IMG_0003.MOV", kind=1, taken=utc(2026, 10, 19, 1, 10)),
    # IMG_0004.HEIC has no row: it was deleted for good, and only its thumbnail is left.
    Row(
        "DCIM/100APPLE",
        "IMG_0005.HEIC",
        taken=utc(2026, 10, 17, 15, 45),
        place=(49.8951, -97.1384),
        trashed_on=utc(2026, 10, 20, 12, 0),
    ),
    Row(
        ICLOUD.rpartition("/")[0],
        ICLOUD.rpartition("/")[2],
        taken=utc(2025, 7, 1, 3, 20),
        place=(51.0447, -114.0719),
        hidden=True,
        original_name="IMG_4821.JPG",
    ),
]


def picture(name: str) -> bytes:
    return (files("afterimage") / "pictures" / f"{name}.jpg").read_bytes()


def build_demo_backup(folder: Path) -> Path:
    """Write the sample backup into `folder` and return its path."""
    sample = SampleBackup(folder, device="Sample iPhone", when=WHEN)
    sample.folder(DOMAIN, "Media").folder(DOMAIN, "Media/DCIM").folder(DOMAIN, "Media/DCIM/100APPLE")
    sample.file(DOMAIN, "Media/DCIM/100APPLE/IMG_0001.HEIC", HEIC)
    sample.file(DOMAIN, "Media/DCIM/100APPLE/IMG_0002.JPG", picture("lake"))
    sample.file(DOMAIN, "Media/DCIM/100APPLE/IMG_0003.MOV", MOV)
    thumbnails = {
        "DCIM/100APPLE/IMG_0001.HEIC": "meadow",
        "DCIM/100APPLE/IMG_0002.JPG": "lake",
        "DCIM/100APPLE/IMG_0003.MOV": "night",
        "DCIM/100APPLE/IMG_0004.HEIC": "sunset",
        "DCIM/100APPLE/IMG_0005.HEIC": "meadow",
        ICLOUD: "night",
    }
    for original, name in thumbnails.items():
        sample.file(DOMAIN, f"Media/PhotoData/Thumbnails/V2/{original}/5005.JPG", picture(name))
    for name, data in photos_database(LIBRARY).items():
        sample.file(DOMAIN, f"Media/PhotoData/{name}", data)
    sample.file("HomeDomain", "Library/Preferences/com.apple.sample.plist", b"bplist00")
    return sample.build()

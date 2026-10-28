from __future__ import annotations

import pytest
from conftest import CAMERA_ROLL, HEIC, JPEG, MOV, FakeBackup
from hypothesis import given
from hypothesis import strategies as st

from afterimage.backup import Backup
from afterimage.camera_roll import media_kind, parse_thumbnail, scan


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("Media/DCIM/100APPLE/IMG_0001.HEIC", "photo"),
        ("Media/DCIM/100APPLE/IMG_0002.jpg", "photo"),
        ("Media/DCIM/100APPLE/IMG_0003.MOV", "video"),
        ("Media/DCIM/100APPLE/IMG_0002.AAE", None),
        ("Media/DCIM/100APPLE/.heic", None),
        ("Media/DCIM/100APPLE/README", None),
        ("Media/DCIM.jpg/README", None),
    ],
)
def test_media_kind(path, kind):
    assert media_kind(path) == kind


@pytest.mark.parametrize(
    ("path", "parsed"),
    [
        (
            "Media/PhotoData/Thumbnails/V2/DCIM/100APPLE/IMG_0001.HEIC/5005.JPG",
            ("Media/DCIM/100APPLE/IMG_0001.HEIC", "5005"),
        ),
        (
            "Media/PhotoData/Thumbnails/V2/PhotoData/CPLAssets/group349/AB12.JPG/5003.JPG",
            ("Media/PhotoData/CPLAssets/group349/AB12.JPG", "5003"),
        ),
        (
            "Media/PhotoData/Thumbnails/V2/DCIM/100APPLE/IMG_0001.HEIC/preview",
            ("Media/DCIM/100APPLE/IMG_0001.HEIC", "preview"),
        ),
        ("Media/PhotoData/Thumbnails/V2/thumbnailConfiguration", None),
        ("Media/PhotoData/Thumbnails/V2/DCIM/", None),
        ("Media/PhotoData/Thumbnails/3303.ithmb", None),
        ("Media/DCIM/100APPLE/IMG_0001.HEIC", None),
    ],
)
def test_parse_thumbnail(path, parsed):
    assert parse_thumbnail(path) == parsed


@given(
    folders=st.lists(st.text(st.characters(blacklist_characters="/"), min_size=1, max_size=12), min_size=1, max_size=4),
    variant=st.text(st.characters(blacklist_characters="/."), min_size=1, max_size=8),
)
def test_every_thumbnail_leads_back_to_its_original(folders, variant):
    original = "/".join(folders)
    assert parse_thumbnail(f"Media/PhotoData/Thumbnails/V2/{original}/{variant}.JPG") == ("Media/" + original, variant)


def test_scan_sorts_the_camera_roll(phone):
    with Backup(phone) as backup:
        roll = scan(backup)

    assert [f.name for f in roll.originals] == ["IMG_0001.HEIC", "IMG_0002.JPG", "IMG_0003.MOV"]
    assert (roll.count("photo"), roll.count("video")) == (2, 1)
    assert roll.originals_size == len(HEIC) + 40 * len(JPEG) + len(MOV)
    assert {original: [t.variant for t in found] for original, found in roll.thumbnails.items()} == {
        "Media/DCIM/100APPLE/IMG_0001.HEIC": ["5003", "5005"],
        "Media/DCIM/100APPLE/IMG_0002.JPG": ["5005"],
        "Media/DCIM/100APPLE/IMG_0003.MOV": ["5005"],
        "Media/DCIM/100APPLE/IMG_0004.HEIC": ["5003", "5005"],
        "Media/PhotoData/CPLAssets/group349/6F1E2D3C-4B5A-4978-8695-A4B3C2D1E0F9.JPG": ["5003"],
    }
    assert roll.without_original() == [
        "Media/DCIM/100APPLE/IMG_0004.HEIC",
        "Media/PhotoData/CPLAssets/group349/6F1E2D3C-4B5A-4978-8695-A4B3C2D1E0F9.JPG",
    ]
    assert [f.name for f in roll.thumbnail_tables] == ["3303.ithmb"]
    assert [f.name for f in roll.database] == ["Photos.sqlite", "Photos.sqlite-wal"] and roll.has_database
    assert [f.name for f in roll.edits] == ["FullSizeRender.jpg"]
    assert [f.name for f in roll.other] == ["IMG_0002.AAE", "thumbnailConfiguration"]
    assert roll.not_on_disk == []
    assert roll.total == 16  # every camera roll file, and no folders or other apps' files


def test_files_listed_but_missing_from_the_folder(backups_dir):
    fake = FakeBackup(backups_dir)
    fake.file(CAMERA_ROLL, "Media/DCIM/100APPLE/IMG_0001.JPG", JPEG, on_disk=False)
    fake.file(CAMERA_ROLL, "Media/PhotoData/Photos.sqlite-wal", b"")
    with Backup(fake.build()) as backup:
        roll = scan(backup)
    assert [f.name for f in roll.not_on_disk] == ["IMG_0001.JPG"]
    assert [f.name for f in roll.originals] == ["IMG_0001.JPG"]  # still counted: the manifest lists it
    assert not roll.has_database  # a write-ahead log without its database isn't the database

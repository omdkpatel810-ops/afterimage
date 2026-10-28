from __future__ import annotations

import sqlite3
import tempfile
from datetime import timedelta
from pathlib import Path

import pytest
from conftest import CAMERA_ROLL, ICLOUD, JPEG, FakeBackup, Row, add_camera_roll, photos_database, snapshot, utc

from afterimage.backup import Backup
from afterimage.camera_roll import scan
from afterimage.library import (
    CORE_DATA_EPOCH,
    GONE,
    IN_LIBRARY,
    RECENTLY_DELETED,
    UNKNOWN,
    Asset,
    LibraryError,
    _core_data_date,
    _place,
    find_leftovers,
    read_library,
    recently_deleted_in_backup,
)


def open_library(root: Path):
    with Backup(root) as backup:
        roll = scan(backup)
        return roll, read_library(backup, roll)


def backup_with(backups_dir: Path, files: dict[str, bytes]) -> Path:
    fake = FakeBackup(backups_dir)
    for name, data in files.items():
        fake.file(CAMERA_ROLL, f"Media/PhotoData/{name}", data)
    return fake.build()


def test_reads_each_photo_from_the_database(phone):
    _, library = open_library(phone)

    assert library.table == "ZASSET"
    assert library.assets["Media/DCIM/100APPLE/IMG_0001.HEIC"] == Asset(
        path="Media/DCIM/100APPLE/IMG_0001.HEIC",
        kind="photo",
        taken=utc(2026, 10, 18, 18, 2),
        place=(49.8951, -97.1384),
    )
    assert library.assets["Media/DCIM/100APPLE/IMG_0003.MOV"] == Asset(
        path="Media/DCIM/100APPLE/IMG_0003.MOV",
        kind="video",
        taken=utc(2026, 10, 19, 1, 10),
        trashed=True,
        trashed_on=utc(2026, 10, 19, 22, 0),
    )
    icloud = library.assets["Media/" + ICLOUD]
    assert (icloud.hidden, icloud.original_name, icloud.place) == (True, "IMG_4821.JPG", (51.0447, -114.0719))
    assert "Media/DCIM/100APPLE/IMG_0004.HEIC" not in library.assets
    assert (library.count(trashed=True), library.count(hidden=True), len(library.assets)) == (2, 1, 5)


def test_rows_still_in_the_write_ahead_log_are_read(backups_dir, tmp_path):
    files = photos_database([Row("DCIM/100APPLE", "IMG_0001.JPG")], keep_in_log=True)
    assert set(files) == {"Photos.sqlite", "Photos.sqlite-wal"}

    _, library = open_library(backup_with(backups_dir, files))
    assert list(library.assets) == ["Media/DCIM/100APPLE/IMG_0001.JPG"]

    # Without its log, the same database file has no rows yet: reading the log is what finds them.
    _, alone = open_library(backup_with(tmp_path / "alone", {"Photos.sqlite": files["Photos.sqlite"]}))
    assert alone.assets == {}


def test_reads_the_older_table_name(backups_dir):
    files = photos_database([Row("DCIM/100APPLE", "IMG_0001.JPG", kind=1)], table="ZGENERICASSET")
    _, library = open_library(backup_with(backups_dir, files))
    assert library.table == "ZGENERICASSET"
    assert library.assets["Media/DCIM/100APPLE/IMG_0001.JPG"].kind == "video"


def test_a_minimal_table_still_gives_paths(backups_dir):
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "Photos.sqlite"
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE ZASSET (Z_PK INTEGER PRIMARY KEY, ZDIRECTORY VARCHAR, ZFILENAME VARCHAR)")
        db.executemany(
            "INSERT INTO ZASSET (ZDIRECTORY, ZFILENAME) VALUES (?, ?)",
            [("/DCIM/100APPLE/", "IMG_0001.JPG"), (None, "loose.jpg"), ("DCIM/100APPLE", None), ("DCIM", "")],
        )
        db.commit()
        db.close()
        files = {"Photos.sqlite": path.read_bytes()}
    _, library = open_library(backup_with(backups_dir, files))
    assert library.assets == {
        "Media/DCIM/100APPLE/IMG_0001.JPG": Asset("Media/DCIM/100APPLE/IMG_0001.JPG"),
        "Media/loose.jpg": Asset("Media/loose.jpg"),
    }


@pytest.mark.parametrize(
    ("schema", "message"),
    [
        ("CREATE TABLE ZALBUM (Z_PK INTEGER)", "no table of photos"),
        ("CREATE TABLE ZASSET (Z_PK INTEGER, ZUUID VARCHAR)", "ZASSET table has no file names"),
    ],
)
def test_databases_that_arent_a_photos_library(backups_dir, schema, message):
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "Photos.sqlite"
        db = sqlite3.connect(path)
        db.execute(schema)
        db.commit()
        db.close()
        files = {"Photos.sqlite": path.read_bytes()}
    with pytest.raises(LibraryError, match=message):
        open_library(backup_with(backups_dir, files))


def test_a_file_that_isnt_a_database(backups_dir):
    with pytest.raises(LibraryError, match="couldn't be read: file is not a database"):
        open_library(backup_with(backups_dir, {"Photos.sqlite": b"nope" * 1000}))


def test_no_database_in_the_backup(backups_dir):
    with pytest.raises(LibraryError, match="isn't in this backup"):
        open_library(FakeBackup(backups_dir).build())


def test_a_database_listed_but_missing_from_the_folder(backups_dir):
    fake = FakeBackup(backups_dir)
    fake.file(CAMERA_ROLL, "Media/PhotoData/Photos.sqlite", b"", on_disk=False)
    with pytest.raises(LibraryError, match="listed in this backup, but its file is missing"):
        open_library(fake.build())


def test_reading_works_on_a_copy_and_leaves_nothing_behind(phone, tmp_path, monkeypatch):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    before = snapshot(phone)
    open_library(phone)
    assert snapshot(phone) == before
    assert list(scratch.iterdir()) == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, CORE_DATA_EPOCH),
        (814_046_400.5, CORE_DATA_EPOCH + timedelta(seconds=814_046_400.5)),
        (None, None),
        ("yesterday", None),
        (True, None),
        (1e300, None),
    ],
)
def test_core_data_dates(value, expected):
    assert _core_data_date(value) == expected


@pytest.mark.parametrize(
    ("latitude", "longitude", "place"),
    [
        (49.8951, -97.1384, (49.8951, -97.1384)),
        (-180.0, -180.0, None),
        (0, 0, None),
        (91, 10, None),
        (10, 181, None),
        (None, 10, None),
        ("north", 10, None),
    ],
)
def test_places(latitude, longitude, place):
    assert _place(latitude, longitude) == place


def test_what_became_of_each_leftover(phone):
    roll, library = open_library(phone)
    leftovers = find_leftovers(roll, library)

    assert [(left.original, left.status) for left in leftovers] == [
        ("Media/DCIM/100APPLE/IMG_0004.HEIC", GONE),
        ("Media/DCIM/100APPLE/IMG_0005.HEIC", RECENTLY_DELETED),
        ("Media/" + ICLOUD, IN_LIBRARY),
    ]
    gone = leftovers[0]
    assert gone.asset is None and len(gone.thumbnails) == 2
    assert gone.thumbnail_saved == utc(2026, 10, 19, 21, 14)
    assert leftovers[1].asset.trashed_on == utc(2026, 10, 20, 12, 0)
    assert [a.path for a in recently_deleted_in_backup(roll, library)] == ["Media/DCIM/100APPLE/IMG_0003.MOV"]


def test_without_a_library_nothing_can_be_told(phone):
    roll, _ = open_library(phone)
    assert {left.status for left in find_leftovers(roll, None)} == {UNKNOWN}
    assert recently_deleted_in_backup(roll, None) == []


def test_a_thumbnail_without_a_date(backups_dir):
    fake = add_camera_roll(FakeBackup(backups_dir), library=[])
    fake.file(CAMERA_ROLL, "Media/PhotoData/Thumbnails/V2/DCIM/100APPLE/IMG_0009.JPG/5005.JPG", JPEG, blob=b"?")
    roll, library = open_library(fake.build())
    undated = next(left for left in find_leftovers(roll, library) if left.original.endswith("IMG_0009.JPG"))
    assert (undated.status, undated.thumbnail_saved) == (GONE, None)

from __future__ import annotations

import plistlib
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from conftest import CAMERA_ROLL, JPEG, UDID, WHEN, FakeBackup, add_camera_roll, mbfile, snapshot
from hypothesis import given, settings
from hypothesis import strategies as st

from afterimage.backup import Backup, BackupError, BackupFile, default_backup_dirs, file_id, find_backups, read_info


def test_file_names_match_the_ones_forensic_tools_look_up():
    # Widely published: these are the names sms.db, the contacts database and Photos.sqlite have in every backup.
    assert file_id("HomeDomain", "Library/SMS/sms.db") == "3d0d7e5fb2ce288813306e4d4636395e047a3d28"
    assert (
        file_id("HomeDomain", "Library/AddressBook/AddressBook.sqlitedb") == "31bb7ba8914766d4ba40d6dfb6113c8b614be442"
    )
    assert file_id(CAMERA_ROLL, "Media/PhotoData/Photos.sqlite") == "12b144c0bd44f2b3dffd9186d3f9c05b917cee25"


def test_default_backup_folders():
    home = Path("/home/om")
    assert default_backup_dirs("Darwin", home) == [home / "Library/Application Support/MobileSync/Backup"]
    assert default_backup_dirs("Windows", home, {"APPDATA": "C:/Users/om/AppData/Roaming"}) == [
        Path("C:/Users/om/AppData/Roaming/Apple Computer/MobileSync/Backup"),
        home / "Apple/MobileSync/Backup",
    ]
    assert default_backup_dirs("Windows", home, {}) == [home / "Apple/MobileSync/Backup"]
    assert default_backup_dirs("Linux", home, {}) == []
    assert isinstance(default_backup_dirs(), list)  # this computer's own answer


# What the plists say ------------------------------------------------------------------------------------


def test_read_info(phone):
    info = read_info(phone)
    assert (info.name, info.model, info.ios_version, info.last_backup, info.encrypted, info.finished) == (
        "Om's iPhone",
        "iPhone15,2",
        "17.5.1",
        WHEN,
        False,
        True,
    )


def test_info_falls_back_to_the_manifest_and_status(backups_dir):
    fake = FakeBackup(backups_dir)
    fake.info_plist = False
    info = read_info(fake.build())
    assert (info.name, info.model, info.ios_version, info.last_backup) == ("Om's iPhone", "iPhone15,2", "17.5.1", WHEN)


def test_without_any_device_name_the_folder_names_the_backup(backups_dir):
    info = read_info(FakeBackup(backups_dir, device=None).build())
    assert info.name == UDID


@pytest.mark.parametrize(("state", "finished"), [("finished", True), ("uploading", False), (None, None)])
def test_whether_the_backup_finished(backups_dir, state, finished):
    fake = FakeBackup(backups_dir)
    fake.status = state
    assert read_info(fake.build()).finished is finished


def test_a_backup_without_a_usable_date(backups_dir):
    root = FakeBackup(backups_dir).build()
    manifest = plistlib.loads((root / "Manifest.plist").read_bytes())
    (root / "Info.plist").unlink()
    (root / "Status.plist").unlink()
    manifest["Date"] = "not a date"
    (root / "Manifest.plist").write_bytes(plistlib.dumps(manifest))
    assert read_info(root).last_backup is None


def test_dates_that_carry_a_time_zone_are_converted_to_utc():
    from afterimage.backup import _utc

    winnipeg = timezone(timedelta(hours=-5))
    assert _utc(datetime(2026, 10, 20, 16, 14, tzinfo=winnipeg)) == datetime(2026, 10, 20, 21, 14, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        (lambda root: (root / "Manifest.plist").unlink(), "isn't an iPhone backup: it has no Manifest.plist"),
        (lambda root: (root / "Manifest.plist").write_bytes(b"garbage"), "Couldn't read Manifest.plist"),
        (lambda root: (root / "Info.plist").write_bytes(plistlib.dumps([1])), "Info.plist in .* isn't shaped like"),
    ],
)
def test_folders_that_arent_readable_backups(backups_dir, setup, message):
    root = FakeBackup(backups_dir).build()
    setup(root)
    with pytest.raises(BackupError, match=message):
        read_info(root)


def test_no_folder(tmp_path):
    with pytest.raises(BackupError, match="No folder at"):
        read_info(tmp_path / "nowhere")


@pytest.mark.parametrize(("system", "tip"), [("Darwin", True), ("Windows", False)])
def test_a_blocked_plist_explains_what_to_do(phone, monkeypatch, system, tip):
    real_open = Path.open

    def guarded(self, *args, **kwargs):
        if self.name == "Manifest.plist":
            raise PermissionError(1, "Operation not permitted")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded)
    monkeypatch.setattr("platform.system", lambda: system)
    with pytest.raises(BackupError, match="didn't allow reading") as caught:
        read_info(phone)
    assert ("Full Disk Access" in str(caught.value)) is tip


# Finding backups --------------------------------------------------------------------------------------


def test_find_backups_newest_first_and_skips_broken_ones(backups_dir, tmp_path):
    FakeBackup(backups_dir, udid="older", when=datetime(2025, 1, 2, tzinfo=timezone.utc)).build()
    FakeBackup(backups_dir).build()
    broken = FakeBackup(backups_dir, udid="broken").build()
    (broken / "Info.plist").write_bytes(b"garbage")
    (backups_dir / "not-a-backup").mkdir()
    (backups_dir / ".DS_Store").write_bytes(b"")

    search = find_backups([backups_dir, tmp_path / "missing"])

    assert [b.path.name for b in search.backups] == [UDID, "older"]
    assert [(p.name, "Couldn't read Info.plist" in reason) for p, reason in search.skipped] == [("broken", True)]
    assert search.searched == [backups_dir, tmp_path / "missing"]


def test_a_backup_folder_can_be_searched_directly(phone):
    assert [b.path for b in find_backups([phone]).backups] == [phone]


def test_backups_without_a_date_go_last(backups_dir):
    undated = FakeBackup(backups_dir, udid="undated")
    undated.info_plist = False
    undated.status = None
    root = undated.build()
    manifest = plistlib.loads((root / "Manifest.plist").read_bytes())
    del manifest["Date"]
    (root / "Manifest.plist").write_bytes(plistlib.dumps(manifest))
    FakeBackup(backups_dir).build()
    assert [b.path.name for b in find_backups([backups_dir]).backups] == [UDID, "undated"]


def test_a_blocked_backups_folder_explains_what_to_do(backups_dir, monkeypatch):
    def refuse(self):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(Path, "iterdir", refuse)
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    with pytest.raises(BackupError, match="Full Disk Access"):
        find_backups([backups_dir])


# Reading Manifest.db ----------------------------------------------------------------------------------


def test_files_by_domain_and_prefix(phone):
    with Backup(phone) as backup:
        everything = list(backup.files())
        dcim = [f.relative_path for f in backup.files(CAMERA_ROLL, "Media/DCIM/")]
        sms = list(backup.files("HomeDomain"))

    assert len(everything) == 22
    assert dcim == [
        "Media/DCIM/100APPLE",
        "Media/DCIM/100APPLE/IMG_0001.HEIC",
        "Media/DCIM/100APPLE/IMG_0002.AAE",
        "Media/DCIM/100APPLE/IMG_0002.JPG",
        "Media/DCIM/100APPLE/IMG_0003.MOV",
    ]
    assert sms == [
        BackupFile(
            file_id="3d0d7e5fb2ce288813306e4d4636395e047a3d28",
            domain="HomeDomain",
            relative_path="Library/SMS/sms.db",
            kind="file",
            size=16,
            modified=datetime(2026, 10, 19, 21, 14, tzinfo=timezone.utc),
        )
    ]
    assert sms[0].name == "sms.db"


def test_counts(phone):
    with Backup(phone) as backup:
        assert backup.kinds() == {"file": 19, "folder": 3}
        assert backup.domains() == [(CAMERA_ROLL, 20), ("AppDomain-com.example.notes", 1), ("HomeDomain", 1)]


def test_unknown_flags_and_unreadable_records(backups_dir):
    fake = FakeBackup(backups_dir)
    fake.file("D", "garbled", JPEG, blob=b"not a plist")
    fake.file("D", "odd-size", JPEG, blob=_record(Size=True, LastModified=10**18))  # a date past year 9999
    fake.file("D", "negative", JPEG, blob=_record(Size=-5, LastModified="yesterday"))
    fake.file(
        "D",
        "not-a-dict",
        JPEG,
        blob=plistlib.dumps(
            {"$archiver": "NSKeyedArchiver", "$top": {"root": plistlib.UID(1)}, "$objects": ["$null", "just text"]},
            fmt=plistlib.FMT_BINARY,
        ),
    )
    root = fake.build()
    db = sqlite3.connect(root / "Manifest.db")
    db.execute("INSERT INTO Files VALUES ('a' || hex(randomblob(19)), 'D', 'no-record', 8, NULL)")
    db.commit()
    db.close()

    with Backup(root) as backup:
        files = {f.relative_path: f for f in backup.files("D")}
        assert backup.kinds() == {"file": 4, "other": 1}

    assert {(f.size, f.modified) for f in files.values()} == {(None, None)}
    assert files["no-record"].kind == "other"


def _record(**fields) -> bytes:
    record = plistlib.loads(mbfile("x", 1, 1))
    record["$objects"][1].update(fields)
    return plistlib.dumps(record, fmt=plistlib.FMT_BINARY)


def test_locate_only_ever_points_inside_the_backup(backups_dir):
    outside = backups_dir / "secret.txt"
    outside.write_text("not part of the backup")
    fake = FakeBackup(backups_dir)
    fake.file("D", "kept", b"here")
    fake.file("D", "listed-only", b"gone", on_disk=False)
    for name in ["../../secret.txt", "../secret.txt", "3D0D7E5FB2CE288813306E4D4636395E047A3D28", "abc"]:
        fake.file("D", f"bad {name}", b"x", file_id=name, on_disk=False)
    root = fake.build()

    with Backup(root) as backup:
        located = {f.relative_path: backup.locate(f) for f in backup.files("D")}

    kept = file_id("D", "kept")
    assert located.pop("kept") == root / kept[:2] / kept
    assert set(located.values()) == {None}


@settings(max_examples=40, deadline=None)
@given(
    paths=st.lists(st.text(alphabet="aAbB%_/.", min_size=1, max_size=6), min_size=1, max_size=8, unique=True),
    prefix=st.text(alphabet="aAbB%_/.", min_size=1, max_size=4),
)
def test_prefixes_match_exactly(paths, prefix):
    """Case counts, and % and _ are ordinary characters, as they are in file names (SQL's LIKE gets both wrong)."""
    with tempfile.TemporaryDirectory() as folder:
        fake = FakeBackup(Path(folder))
        for path in paths:
            fake.file("D", path, on_disk=False)
        with Backup(fake.build()) as backup:
            found = [f.relative_path for f in backup.files("D", prefix)]
    assert found == sorted(p for p in paths if p.startswith(prefix))


# Backups that can't be opened --------------------------------------------------------------------------


def test_an_encrypted_backup_is_refused_politely(backups_dir):
    fake = FakeBackup(backups_dir)
    fake.encrypted = True
    root = fake.build()
    assert read_info(root).encrypted is True
    with pytest.raises(BackupError, match="is encrypted.*never tries to get around it"):
        Backup(root)


def test_a_backup_from_ios_9_or_earlier(backups_dir):
    fake = FakeBackup(backups_dir)
    fake.manifest_db = False
    root = fake.build()
    (root / "Manifest.mbdb").write_bytes(b"mbdb\x05\x00")
    with pytest.raises(BackupError, match="iOS 9 or earlier"):
        Backup(root)


def test_a_backup_without_its_manifest(backups_dir):
    fake = FakeBackup(backups_dir)
    fake.manifest_db = False
    with pytest.raises(BackupError, match="no Manifest.db, so the backup may be unfinished"):
        Backup(fake.build())


@pytest.mark.parametrize("content", [b"definitely not sqlite" * 100, None])
def test_a_manifest_that_cant_be_read(backups_dir, content):
    root = FakeBackup(backups_dir).build()
    (root / "Manifest.db").unlink()
    if content is None:  # a real database, but not a backup manifest
        sqlite3.connect(root / "Manifest.db").execute("CREATE TABLE Other (x)").connection.close()
    else:
        (root / "Manifest.db").write_bytes(content)
    with pytest.raises(BackupError, match="Couldn't read Manifest.db"):
        Backup(root)


def test_reading_never_changes_the_backup(phone):
    before = snapshot(phone)
    with Backup(phone) as backup:
        for entry in backup.files():
            if (path := backup.locate(entry)) is not None:
                path.read_bytes()
        backup.domains()
        backup.kinds()
    assert snapshot(phone) == before


def test_add_camera_roll_builds_a_complete_backup(backups_dir):
    root = add_camera_roll(FakeBackup(backups_dir)).build()
    assert (root / "Info.plist").read_bytes().startswith(b"<?xml")
    assert (root / "Manifest.plist").read_bytes().startswith(b"bplist00")

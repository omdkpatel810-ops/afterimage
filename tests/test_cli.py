from __future__ import annotations

import io
import plistlib
import subprocess
import sys
from datetime import datetime, timezone

import pytest
from conftest import CAMERA_ROLL, ICLOUD, JPEG, UDID, FakeBackup, Row, add_camera_roll, photos_database, snapshot

from afterimage import __version__
from afterimage.cli import HOW_TO_BACK_UP, NO_STANDARD_PLACE, main

DEFAULT = object()
OLDER = datetime(2025, 3, 1, 9, 5, tzinfo=timezone.utc)


def run(*args, dirs=DEFAULT, backups_dir=None):
    out, err = io.StringIO(), io.StringIO()
    folders = [backups_dir] if dirs is DEFAULT else dirs
    code = main(list(args), out=out, err=err, backup_dirs=folders, tz=timezone.utc)
    return code, out.getvalue(), err.getvalue()


def test_version(capsys):
    with pytest.raises(SystemExit):
        main(["--version"])
    assert capsys.readouterr().out.strip() == f"afterimage {__version__}"


def test_runs_as_a_module():
    result = subprocess.run(
        [sys.executable, "-m", "afterimage", "--version"], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == f"afterimage {__version__}"


# list -------------------------------------------------------------------------------------------------


def test_list(backups_dir, phone):
    old = FakeBackup(backups_dir, udid="00008030-0011223344556677", device="Old iPhone", ios="16.7.10")
    old.when, old.encrypted, old.status = OLDER, True, "uploading"
    old.build()
    broken = FakeBackup(backups_dir, udid="broken").build()
    (broken / "Manifest.plist").write_bytes(b"garbage")

    code, out, err = run("list", backups_dir=backups_dir)

    assert (code, err) == (0, "")
    assert out.splitlines() == [
        f"2 backups in {backups_dir}:",
        f"  Om's iPhone  iOS 17.5.1   Tue 20 Oct 2026 21:14 UTC  -                      {UDID}",
        "  Old iPhone   iOS 16.7.10  Sat 01 Mar 2025 09:05 UTC  encrypted, unfinished  00008030-0011223344556677",
        f"Skipped {broken}: Couldn't read Manifest.plist in {broken}: it isn't a valid plist",
    ]


def test_list_with_no_backups(backups_dir):
    _, out, _ = run("list", backups_dir=backups_dir)
    assert out.splitlines() == [f"No backups found in {backups_dir}.", HOW_TO_BACK_UP]


def test_list_where_theres_no_standard_place():
    code, _, err = run("list", dirs=[])
    assert (code, err.strip()) == (1, f"error: {NO_STANDARD_PLACE}")


def test_list_in_folders_named_on_the_command_line(tmp_path, phone):
    _, out, _ = run("list", "--dir", str(phone.parent), "--dir", str(tmp_path / "none"), dirs=[])
    assert out.splitlines()[0] == f"1 backup in {phone.parent} and {tmp_path / 'none'}:"


# info -------------------------------------------------------------------------------------------------


def test_info(phone):
    code, out, _ = run("info", str(phone), dirs=[])
    assert code == 0
    assert out.splitlines() == [
        "Om's iPhone",
        "  Model      iPhone15,2",
        "  iOS        17.5.1",
        "  Backed up  Tue 20 Oct 2026 21:14 UTC",
        "  Encrypted  no",
        "  Finished   yes",
        f"  Folder     {phone}",
        "  Contents   19 files and 3 folders from 3 apps and system areas",
        "",
        "Areas with the most entries:",
        "  CameraRollDomain             20",
        "  AppDomain-com.example.notes   1",
        "  HomeDomain                    1",
    ]


def test_no_command_prints_the_phones_identifiers(phone, backups_dir):
    for args in (["list"], ["info"], ["photos", "--list"]):
        _, out, _ = run(*args, backups_dir=backups_dir)
        assert out and "DO-NOT-PRINT" not in out


def test_info_on_an_encrypted_unfinished_backup(backups_dir):
    fake = FakeBackup(backups_dir, device=None)
    fake.encrypted, fake.status, fake.info_plist = True, None, False
    fake.ios = ""
    root = fake.build()
    _, out, _ = run("info", backups_dir=backups_dir)
    assert out.splitlines() == [
        UDID,
        "  Model      iPhone15,2",
        "  iOS        unknown",
        "  Backed up  Tue 20 Oct 2026 21:14 UTC",
        "  Encrypted  yes",
        "  Finished   unknown",
        f"  Folder     {root}",
        "",
        "It's encrypted, so afterimage can't look inside yet. That's planned, using the backup's own password.",
    ]


def test_info_warns_about_an_unfinished_backup(backups_dir):
    fake = FakeBackup(backups_dir)
    fake.status = "uploading"
    fake.build()
    _, out, _ = run("info", backups_dir=backups_dir)
    assert "  Finished   no" in out.splitlines()
    assert out.splitlines()[-1] == "Warning: this backup didn't finish, so some files may be missing from it."


def test_info_with_a_date_unknown(backups_dir):
    fake = FakeBackup(backups_dir)
    fake.info_plist, fake.status = False, None
    root = fake.build()
    manifest = plistlib.loads((root / "Manifest.plist").read_bytes())
    del manifest["Date"]
    (root / "Manifest.plist").write_bytes(plistlib.dumps(manifest))
    _, out, _ = run("info", str(root), dirs=[])
    assert "  Backed up  date unknown" in out.splitlines()


# photos -----------------------------------------------------------------------------------------------

SUMMARY = [
    "Om's iPhone · iOS 17.5.1 · backed up Tue 20 Oct 2026 21:14 UTC",
    "",
    "Camera roll in this backup:",
    "  Photos and videos      3  2 photos, 1 video, 5.4 KB",
    "  Thumbnails             8  for 6 photos and videos",
    "  Thumbnail tables       1  .ithmb files, many thumbnails in each",
    "  Edited versions        1",
    "  Photos database    found  5 photos and videos, 2 in Recently Deleted, 1 hidden",
    "  Other files            2",
    "",
    "3 photos and videos with thumbnails but no original in this backup:",
    "  No longer in the library: only thumbnails are left  1",
    "  In Recently Deleted                                 1",
    "  Still in the library, original not in this backup   1",
    "",
    "1 photo or video in Recently Deleted is still in this backup at full size.",
]
RESTORE_TIP = [
    "",
    "Recently Deleted keeps photos for about 30 days. Until then, the phone can restore them at full size"
    " from that album in the Photos app.",
]


def test_photos(phone, backups_dir):
    code, out, err = run("photos", backups_dir=backups_dir)  # the only backup, so no need to name it
    assert (code, err) == (0, "")
    assert out.splitlines() == [*SUMMARY, "Add --list to see them.", *RESTORE_TIP]


def test_photos_list(phone):
    _, out, _ = run("photos", str(phone), "--list", dirs=[])
    assert out.splitlines() == [
        *SUMMARY,
        "",
        "No longer in the library: only thumbnails are left (1):",
        "  DCIM/100APPLE/IMG_0004.HEIC  2 thumbnails, saved Mon 19 Oct 2026 21:14 UTC",
        "",
        "In Recently Deleted (1):",
        "  DCIM/100APPLE/IMG_0005.HEIC  photo, taken Sat 17 Oct 2026 15:45 UTC, deleted Tue 20 Oct 2026 12:00 UTC,"
        " 1 thumbnail",
        "",
        "Still in the library; the original isn't in this backup, usually because it's kept in iCloud (1):",
        f"  {ICLOUD}  photo, taken Tue 01 Jul 2025 16:20 UTC, at 51.04470, -114.07190, hidden,"
        " first named IMG_4821.JPG, 1 thumbnail",
        "",
        "In Recently Deleted, still in this backup at full size (1):",
        "  DCIM/100APPLE/IMG_0003.MOV  video, taken Mon 19 Oct 2026 01:10 UTC, deleted Mon 19 Oct 2026 22:00 UTC",
        *RESTORE_TIP,
    ]


def test_photos_without_a_photos_database(backups_dir):
    add_camera_roll(FakeBackup(backups_dir), library=None).build()
    _, out, _ = run("photos", "--list", backups_dir=backups_dir)
    lines = out.splitlines()
    assert "  Photos database    missing" in lines
    start = lines.index("3 photos and videos have thumbnails but no original in this backup.")
    assert lines[start + 1 : start + 3] == [
        "Each was deleted, or is kept in iCloud and the phone held only a small copy.",
        "The Photos database (Photos.sqlite) isn't in this backup. So afterimage can't tell which.",
    ]
    assert lines[-4:] == [
        "Deleted, or kept in iCloud (3):",
        f"  {'DCIM/100APPLE/IMG_0004.HEIC':<{len(ICLOUD)}}  2 thumbnails, saved Mon 19 Oct 2026 21:14 UTC",
        f"  {'DCIM/100APPLE/IMG_0005.HEIC':<{len(ICLOUD)}}  1 thumbnail, saved Mon 19 Oct 2026 21:14 UTC",
        f"  {ICLOUD}  1 thumbnail, saved Mon 19 Oct 2026 21:14 UTC",
    ]


def test_photos_with_a_photos_database_that_cant_be_read(backups_dir):
    fake = add_camera_roll(FakeBackup(backups_dir), library=None)
    fake.file(CAMERA_ROLL, "Media/PhotoData/Photos.sqlite", b"not a database" * 100)
    fake.build()
    _, out, _ = run("photos", backups_dir=backups_dir)
    assert "  Photos database    unreadable" in out.splitlines()
    assert (
        "The Photos database couldn't be read: file is not a database. So afterimage can't tell which."
        in out.splitlines()
    )


def test_photos_when_every_thumbnail_has_its_original(backups_dir):
    fake = FakeBackup(backups_dir)
    fake.file(CAMERA_ROLL, "Media/DCIM/100APPLE/IMG_0001.JPG", JPEG * 30)
    fake.file(CAMERA_ROLL, "Media/PhotoData/Thumbnails/V2/DCIM/100APPLE/IMG_0001.JPG/5005.JPG", JPEG)
    for name, data in photos_database([Row("DCIM/100APPLE", "IMG_0001.JPG")]).items():
        fake.file(CAMERA_ROLL, f"Media/PhotoData/{name}", data)
    fake.build()
    _, out, _ = run("photos", "--list", backups_dir=backups_dir)
    assert out.splitlines()[3:] == [
        "  Photos and videos      1  1 photo, 0 videos, 1.1 KB",
        "  Thumbnails             1  for 1 photo or video",
        "  Thumbnail tables       0",
        "  Edited versions        0",
        "  Photos database    found  1 photo or video",
        "  Other files            0",
        "",
        "Every thumbnail's photo or video is in this backup too.",
    ]


def test_photos_with_one_thumbnail_left_and_files_missing(backups_dir):
    fake = FakeBackup(backups_dir)
    fake.status = "uploading"
    fake.file(CAMERA_ROLL, "Media/PhotoData/Thumbnails/V2/DCIM/100APPLE/IMG_0009.HEIC/5005.JPG", JPEG)
    fake.file(CAMERA_ROLL, "Media/DCIM/100APPLE/IMG_0010.MOV", bytes(10), on_disk=False)
    fake.build()
    _, out, _ = run("photos", backups_dir=backups_dir)
    lines = out.splitlines()
    assert lines[1] == "Warning: this backup didn't finish, so some files may be missing from it."
    assert "  Photos and videos        1  0 photos, 1 video, 10 bytes" in lines
    assert "1 photo or video has thumbnails but no original in this backup." in lines
    assert lines[-1] == (
        "Note: 1 camera roll file listed in Manifest.db is missing from the backup folder,"
        " so the backup may be incomplete."
    )


def test_photos_without_thumbnails(backups_dir):
    fake = FakeBackup(backups_dir)
    for n in range(2):
        fake.file(CAMERA_ROLL, f"Media/DCIM/100APPLE/IMG_000{n}.MOV", bytes(2500), on_disk=False)
    fake.build()
    _, out, _ = run("photos", backups_dir=backups_dir)
    assert "There are no thumbnails in Media/PhotoData/Thumbnails/V2 in this backup." in out.splitlines()
    assert "Note: 2 camera roll files listed in Manifest.db are missing from the backup folder" in out


def test_photos_with_no_camera_roll(backups_dir):
    FakeBackup(backups_dir).file("HomeDomain", "Library/SMS/sms.db", b"x").build()
    _, out, _ = run("photos", backups_dir=backups_dir)
    assert out.splitlines()[-1] == "This backup holds no camera roll files."


def test_photos_refuses_an_encrypted_backup(backups_dir):
    fake = FakeBackup(backups_dir)
    fake.encrypted = True
    fake.build()
    code, out, err = run("photos", backups_dir=backups_dir)
    assert (code, out) == (1, "")
    assert err.startswith("error: The backup of Om's iPhone is encrypted.")


# Picking a backup -------------------------------------------------------------------------------------


@pytest.fixture
def two_phones(backups_dir):
    add_camera_roll(FakeBackup(backups_dir)).build()
    FakeBackup(backups_dir, udid="00008030-0011223344556677", device="Old iPhone", when=OLDER).build()
    return backups_dir


@pytest.mark.parametrize("name", ["old iphone", "00008030", "00008030-0011223344556677"])
def test_a_backup_can_be_named_by_device_or_folder(two_phones, name):
    _, out, _ = run("photos", name, backups_dir=two_phones)
    assert out.startswith("Old iPhone · iOS 17.5.1")


def test_several_backups_need_a_name(two_phones):
    code, _, err = run("photos", backups_dir=two_phones)
    assert code == 1
    assert err.strip() == (
        f"error: 2 backups fit. Name one by its folder: {UDID} (Om's iPhone, Tue 20 Oct 2026 21:14 UTC);"
        " 00008030-0011223344556677 (Old iPhone, Sat 01 Mar 2025 09:05 UTC)"
    )


def test_no_backup_by_that_name(two_phones):
    code, _, err = run("info", "Mom's iPad", backups_dir=two_phones)
    assert (code, err.strip()) == (1, f'error: No folder or backup called "Mom\'s iPad". Looked in {two_phones}.')


def test_no_backups_at_all(backups_dir):
    code, _, err = run("photos", backups_dir=backups_dir)
    assert (code, err.strip()) == (1, f"error: No backups found in {backups_dir}. {HOW_TO_BACK_UP}")


@pytest.mark.parametrize(
    ("args", "message"),
    [(["photos"], NO_STANDARD_PLACE), (["photos", "Om's iPhone"], f"No folder at Om's iPhone. {NO_STANDARD_PLACE}")],
)
def test_without_a_standard_place_a_path_is_needed(args, message):
    code, _, err = run(*args, dirs=[])
    assert (code, err.strip()) == (1, f"error: {message}")


# Read-only --------------------------------------------------------------------------------------------


def test_no_command_changes_the_backup(phone, backups_dir):
    before = snapshot(backups_dir)
    for args in (["list"], ["info"], ["photos", "--list"], ["photos", str(phone)]):
        assert run(*args, backups_dir=backups_dir)[0] == 0
    assert snapshot(backups_dir) == before

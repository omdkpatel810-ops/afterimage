from __future__ import annotations

import csv
import io
import os
import re
from datetime import timezone
from pathlib import Path
from urllib.parse import unquote

import pytest
from conftest import (
    CAMERA_ROLL,
    ICLOUD,
    LIBRARY,
    MOV,
    FakeBackup,
    Row,
    add_camera_roll,
    photos_database,
    snapshot,
    utc,
)
from PIL import Image

from afterimage.backup import Backup
from afterimage.camera_roll import scan
from afterimage.cli import main
from afterimage.export import Export, ExportError, check_folder, export, safe_name
from afterimage.library import read_library
from afterimage.report import render


def picture() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (36, 36), (40, 120, 200)).save(buffer, "JPEG")
    return buffer.getvalue()


PICTURE = picture()


def run(*args, dirs):
    out, err = io.StringIO(), io.StringIO()
    code = main(list(args), out=out, err=err, backup_dirs=dirs, tz=timezone.utc)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def phone(backups_dir):
    """The usual camera roll, with thumbnails that are real pictures."""
    return add_camera_roll(FakeBackup(backups_dir), picture=PICTURE).build()


def files_in(folder: Path) -> list[str]:
    return sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file())


def taken_and_place(path: Path):
    exif = Image.open(path).getexif()
    return exif.get_ifd(0x8769).get(0x9003), exif.get_ifd(0x8825).get(1)


def test_export_saves_each_photo_where_it_belongs(phone, tmp_path):
    out = tmp_path / "saved"
    before = snapshot(phone)

    code, text, err = run("export", str(phone), "--out", str(out), dirs=[])

    assert (code, err) == (0, "")
    assert text.splitlines() == [
        f"Saved 4 files to {out}:",
        "  No longer in the library: only thumbnails are left  1  only-thumbnails-left/",
        "  In Recently Deleted                                 1  recently-deleted/",
        "  In Recently Deleted, full size                      1  recently-deleted-full-size/",
        "  Still in the library, original not in this backup   1  still-in-library/",
        "Wrote the date taken or place into 2 thumbnails.",
        "",
        "Open report.html in your browser to see them all; afterimage.csv lists them for a spreadsheet.",
        "This folder holds copies of your photos. Keep it private, and delete it when you're done.",
    ]
    assert files_in(out) == [
        "afterimage.csv",
        "only-thumbnails-left/IMG_0004 (thumbnail).jpg",
        "recently-deleted-full-size/IMG_0003.MOV",
        "recently-deleted/IMG_0005 (thumbnail).jpg",
        "report.html",
        "still-in-library/IMG_4821 (thumbnail).jpg",  # named as the camera named it, not by its iCloud ID
    ]
    assert snapshot(phone) == before  # the backup is only read

    # Dates and places are written in where the library knows them, and file times follow the date taken.
    assert taken_and_place(out / "recently-deleted/IMG_0005 (thumbnail).jpg") == ("2026:10:17 15:45:00", None)
    assert taken_and_place(out / "still-in-library/IMG_4821 (thumbnail).jpg") == ("2025:07:01 16:20:00", "N")
    assert taken_and_place(out / "only-thumbnails-left/IMG_0004 (thumbnail).jpg") == (None, None)
    times = {name: os.stat(out / name).st_mtime for name in files_in(out) if "/" in name}
    assert times == {
        "only-thumbnails-left/IMG_0004 (thumbnail).jpg": utc(2026, 10, 19, 21, 14).timestamp(),  # thumbnail saved
        "recently-deleted-full-size/IMG_0003.MOV": utc(2026, 10, 19, 1, 10).timestamp(),
        "recently-deleted/IMG_0005 (thumbnail).jpg": utc(2026, 10, 17, 15, 45).timestamp(),
        "still-in-library/IMG_4821 (thumbnail).jpg": utc(2025, 7, 1, 16, 20).timestamp(),
    }
    assert (out / "recently-deleted-full-size/IMG_0003.MOV").read_bytes() == MOV  # a full, exact copy


def test_the_spreadsheet(phone, tmp_path):
    run("export", str(phone), "--out", str(tmp_path / "saved"), dirs=[])
    with (tmp_path / "saved" / "afterimage.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    by_file = {row["file"]: row for row in rows}
    assert len(rows) == 4
    assert by_file["still-in-library/IMG_4821 (thumbnail).jpg"] == {
        "file": "still-in-library/IMG_4821 (thumbnail).jpg",
        "what became of it": "Still in the library, original not in this backup",
        "path in backup": "Media/" + ICLOUD,
        "kind": "photo",
        "taken (UTC)": "2025-07-01 16:20:00",
        "latitude": "51.044700",
        "longitude": "-114.071900",
        "deleted (UTC)": "",
        "hidden": "yes",
        "first named": "IMG_4821.JPG",
        "thumbnail saved (UTC)": "2026-10-19 21:14:00",
        "date or place written in": "yes",
    }
    assert by_file["recently-deleted-full-size/IMG_0003.MOV"]["deleted (UTC)"] == "2026-10-19 22:00:00"


def test_the_report_page(phone, tmp_path):
    out = tmp_path / "saved"
    run("export", str(phone), "--out", str(out), dirs=[])
    page = (out / "report.html").read_text(encoding="utf-8")

    sources = [unquote(src) for src in re.findall(r'<img src="([^"]+)"', page)]
    assert sorted(sources) == [
        "only-thumbnails-left/IMG_0004 (thumbnail).jpg",
        "recently-deleted/IMG_0005 (thumbnail).jpg",
        "still-in-library/IMG_4821 (thumbnail).jpg",
    ]
    assert all((out / source).is_file() for source in sources)
    assert '<span class="tile">MOV</span>' in page  # a video gets a tile, not a broken picture
    assert "<strong>IMG_4821.JPG</strong>" in page and "Stored in the backup as 6F1E2D3C" in page
    assert "Thumbnail saved Mon 19 Oct 2026 21:14 UTC, about when the photo reached the phone" in page
    assert "https://www.openstreetmap.org/?mlat=51.044700&amp;mlon=-114.071900" in page
    assert "Get back what's in Recently Deleted." in page
    assert not re.search(r"""(src|href)=["']?(https?:)?//""", page.replace('href="https://www.openstreetmap', ""))


def test_the_report_escapes_what_the_backup_says(backups_dir, tmp_path):
    sneaky = '<img src=x onerror="alert(1)">.jpg'
    library = [
        *LIBRARY[:4],
        Row(ICLOUD.rpartition("/")[0], ICLOUD.rpartition("/")[2], original_name=sneaky),
    ]
    root = add_camera_roll(FakeBackup(backups_dir, device="<b>Om</b>"), library=library, picture=PICTURE).build()
    out = tmp_path / "saved"
    run("export", str(root), "--out", str(out), dirs=[])

    page = (out / "report.html").read_text(encoding="utf-8")
    assert "<img src=x" not in page and "<b>Om</b>" not in page
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;.jpg" in page and "&lt;b&gt;Om&lt;/b&gt;" in page
    assert not any(set('<>:"') & set(name) for name in files_in(out))


def test_a_full_size_photo_shows_on_the_page(backups_dir, tmp_path):
    library = [Row("DCIM/100APPLE", "IMG_0002.JPG", trashed_on=utc(2026, 10, 20, 9, 0))]
    root = add_camera_roll(FakeBackup(backups_dir), library=library, picture=PICTURE).build()
    run("export", str(root), "--out", str(tmp_path / "saved"), dirs=[])
    page = (tmp_path / "saved" / "report.html").read_text(encoding="utf-8")
    assert '<img src="recently-deleted-full-size/IMG_0002.JPG"' in page


def test_without_a_photos_database(backups_dir, tmp_path):
    add_camera_roll(FakeBackup(backups_dir), library=None, picture=PICTURE).build()
    out = tmp_path / "saved"
    _, text, _ = run("export", "--out", str(out), dirs=[backups_dir])
    assert text.splitlines()[:4] == [
        f"Saved 3 files to {out}:",
        "  Deleted, or kept in iCloud  3  deleted-or-in-icloud/",
        "Wrote the date taken or place into 0 thumbnails.",
        "The Photos database (Photos.sqlite) isn't in this backup. So afterimage couldn't tell what became of"
        " each photo.",
    ]
    assert "so each of these was either deleted or is kept in iCloud" in (out / "report.html").read_text(
        encoding="utf-8"
    )


def test_same_names_get_numbers(backups_dir, tmp_path):
    fake = FakeBackup(backups_dir)
    for folder, name in [("100APPLE", "IMG_0001.HEIC"), ("101APPLE", "IMG_0001.HEIC"), ("102APPLE", "img_0001.heic")]:
        fake.file(CAMERA_ROLL, f"Media/PhotoData/Thumbnails/V2/DCIM/{folder}/{name}/5005.JPG", PICTURE)
    fake.file(CAMERA_ROLL, "Media/PhotoData/Thumbnails/V2/DCIM/103APPLE/IMG_0009.HEIC/preview", b"GIF89a")
    for name, data in photos_database([]).items():
        fake.file(CAMERA_ROLL, f"Media/PhotoData/{name}", data)
    out = tmp_path / "saved"
    run("export", str(fake.build()), "--out", str(out), dirs=[])
    assert files_in(out / "only-thumbnails-left") == [
        "IMG_0001 (thumbnail) (2).jpg",
        "IMG_0001 (thumbnail).jpg",
        "IMG_0009 (thumbnail)",  # not a JPEG, and its name gives no type
        "img_0001 (thumbnail) (3).jpg",
    ]
    assert '<span class="tile">file</span>' in (out / "report.html").read_text(encoding="utf-8")


def test_files_missing_from_the_backup_folder(backups_dir, tmp_path):
    fake = FakeBackup(backups_dir)
    fake.file(CAMERA_ROLL, "Media/PhotoData/Thumbnails/V2/DCIM/100APPLE/IMG_0008.HEIC/5005.JPG", PICTURE, on_disk=False)
    fake.file(CAMERA_ROLL, "Media/DCIM/100APPLE/IMG_0007.MOV", MOV, on_disk=False)
    for name, data in photos_database([Row("DCIM/100APPLE", "IMG_0007.MOV", trashed_on=utc(2026, 10, 1))]).items():
        fake.file(CAMERA_ROLL, f"Media/PhotoData/{name}", data)
    _, text, _ = run("export", str(fake.build()), "--out", str(tmp_path / "saved"), dirs=[])
    assert text.splitlines()[0] == f"Saved 0 files to {tmp_path / 'saved'}:"
    assert "2 photos and videos couldn't be saved: their files are missing from the backup folder, or damaged." in text


def test_nothing_to_save(backups_dir, tmp_path):
    fake = FakeBackup(backups_dir)
    fake.file(CAMERA_ROLL, "Media/DCIM/100APPLE/IMG_0001.JPG", PICTURE)
    fake.file(CAMERA_ROLL, "Media/PhotoData/Thumbnails/V2/DCIM/100APPLE/IMG_0001.JPG/5005.JPG", PICTURE)
    code, text, _ = run("export", str(fake.build()), "--out", str(tmp_path / "saved"), dirs=[])
    assert code == 0 and text.startswith("Nothing to save")
    assert not (tmp_path / "saved").exists()


def test_refuses_to_write_into_the_backup_or_over_files(phone, tmp_path):
    before = snapshot(phone)
    code, _, err = run("export", str(phone), "--out", str(phone / "saved"), dirs=[])
    assert code == 1 and "is inside the backup" in err
    assert snapshot(phone) == before

    full = tmp_path / "full"
    full.mkdir()
    (full / "keep.txt").write_text("mine")
    code, _, err = run("export", str(phone), "--out", str(full), dirs=[])
    assert code == 1 and "already has files in it" in err
    assert files_in(full) == ["keep.txt"]

    with pytest.raises(ExportError, match="already has files"):
        check_folder(full / "keep.txt", phone)
    with pytest.raises(ExportError, match="inside the backup"):
        check_folder(phone, phone)

    empty = tmp_path / "empty"
    empty.mkdir()
    assert run("export", str(phone), "--out", str(empty), dirs=[])[0] == 0


def test_dates_a_system_cant_store_dont_stop_the_export(phone, tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise OSError("this file system can't store that date")

    monkeypatch.setattr(os, "utime", refuse)
    with Backup(phone) as backup:
        roll = scan(backup)
        result = export(backup, roll, read_library(backup, roll), tmp_path / "saved")
    assert len(result.saved) == 4


@pytest.mark.parametrize(
    ("name", "safe"),
    [
        ("IMG_0001 (thumbnail).jpg", "IMG_0001 (thumbnail).jpg"),
        ("Café à Montréal.HEIC", "Café à Montréal.HEIC"),
        ("../../etc/passwd", "_._etc_passwd"),  # no way back up the folders
        ('a:b*c?"d<e>f|g.jpg', "a_b_c_d_e_f_g.jpg"),
        ("CON.jpg", "_CON.jpg"),
        ("lpt1", "_lpt1"),
        (" .hidden. ", "hidden"),
        ("", "photo"),
        ("///", "_"),
        ("x" * 300 + ".jpeg", "x" * 115 + ".jpeg"),
        ("." + "y" * 10, "y" * 10),
    ],
)
def test_safe_names(name, safe):
    assert safe_name(name) == safe


def test_a_report_with_nothing_in_it(phone):
    with Backup(phone) as backup:
        page = render(Export(Path("unused"), backup.info))
    assert "Nothing to show" in page and "Get back what's in Recently Deleted" not in page

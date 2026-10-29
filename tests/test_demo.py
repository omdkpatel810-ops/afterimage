from __future__ import annotations

import io
import re
from datetime import timezone

from PIL import Image

from afterimage.cli import main
from afterimage.demo import picture


def run(*args):
    out, err = io.StringIO(), io.StringIO()
    code = main(list(args), out=out, err=err, backup_dirs=[], tz=timezone.utc)
    return code, out.getvalue(), err.getvalue()


def test_the_demo_runs_end_to_end(tmp_path):
    folder = tmp_path / "try"
    code, out, err = run("demo", str(folder))

    assert (code, err) == (0, "")
    lines = out.splitlines()
    assert lines[0].startswith(f"Made a sample backup in {folder / 'backup'}")
    assert f"$ afterimage export {folder / 'backup'}" in out
    assert "  Photos database    found  5 photos and videos, 2 in Recently Deleted, 1 hidden" in lines
    assert lines[-1] == f"The report: {folder / 'export' / 'report.html'}"
    page = (folder / "export" / "report.html").read_text(encoding="utf-8")
    assert len(re.findall(r"<img ", page)) == 4  # three thumbnails, and the full-size photo from Recently Deleted
    assert Image.open(folder / "export" / "recently-deleted-full-size" / "IMG_0002.JPG").size == (360, 270)


def test_the_demo_has_a_default_folder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert run("demo")[0] == 0
    assert (tmp_path / "afterimage-demo" / "export" / "report.html").is_file()


def test_the_demo_wont_mix_with_other_files(tmp_path):
    (tmp_path / "keep.txt").write_text("mine")
    code, _, err = run("demo", str(tmp_path))
    assert code == 1 and "already has files in it" in err


def test_the_pictures_ship_with_the_package():
    for name in ("sunset", "meadow", "night", "lake"):
        assert picture(name).startswith(b"\xff\xd8\xff")

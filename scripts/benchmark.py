"""How fast afterimage is on a backup the size of a well-used iPhone's.

It builds a made-up backup (nothing real) with a camera roll of PHOTOS photos, each with its original and
two thumbnails, plus leftover thumbnails of photos no longer in the library, a Photos database with a row
per photo, and files from other apps. Then it times each step, best of three. Run it from the project
folder after `pip install -e ".[dev]"`:

    python scripts/benchmark.py --photos 20000
"""

from __future__ import annotations

import argparse
import platform
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from afterimage.backup import Backup
from afterimage.camera_roll import DOMAIN, scan
from afterimage.demo import picture
from afterimage.export import export
from afterimage.library import find_leftovers, read_library
from afterimage.sample import Encryption, Row, SampleBackup, photos_database

START = datetime(2019, 1, 1, tzinfo=timezone.utc)


def build(folder: Path, photos: int, leftovers: int, others: int) -> tuple[Path, int]:
    sample = SampleBackup(folder)
    thumbnail = picture("meadow")
    rows = []
    for n in range(photos):
        name = f"{100 + n // 1000}APPLE/IMG_{n % 10000:04d}.HEIC"
        sample.file(DOMAIN, f"Media/DCIM/{name}")  # empty: only how many files there are matters here
        for variant in ("5003", "5005"):
            sample.file(DOMAIN, f"Media/PhotoData/Thumbnails/V2/DCIM/{name}/{variant}.JPG")
        taken = START + timedelta(hours=n)
        rows.append(Row(f"DCIM/{name.rpartition('/')[0]}", name.rpartition("/")[2], taken=taken, place=(49.9, -97.1)))
    for n in range(leftovers):  # deleted for good: a real picture left behind, and no row
        sample.file(DOMAIN, f"Media/PhotoData/Thumbnails/V2/DCIM/900APPLE/IMG_{n:04d}.HEIC/5005.JPG", thumbnail)
    for name, data in photos_database(rows).items():
        sample.file(DOMAIN, f"Media/PhotoData/{name}", data)
    for n in range(others):
        sample.file(f"AppDomain-com.example.app{n % 40}", f"Documents/file{n}.dat")
    root = sample.build()
    return root, len(sample.entries)


def best_of_three(step) -> float:
    times = []
    for _ in range(3):
        started = time.perf_counter()
        step()
        times.append(time.perf_counter() - started)
    return min(times)


def unlock_time() -> float:
    """Unlocking with Apple's own settings: 10,000,000 SHA-256 rounds, then 10,000 SHA-1 rounds."""
    with tempfile.TemporaryDirectory() as folder:
        sample = SampleBackup(Path(folder))
        sample.encryption = Encryption("a long backup password", dpic=10_000_000, iterations=10_000)
        sample.file(DOMAIN, "Media/DCIM/100APPLE/IMG_0001.HEIC", b"x")
        root = sample.build()
        started = time.perf_counter()
        Backup(root, "a long backup password").close()
        return time.perf_counter() - started


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--photos", type=int, default=20_000)
    parser.add_argument("--leftovers", type=int, default=2_000)
    parser.add_argument("--others", type=int, default=30_000)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as scratch:
        folder = Path(scratch)
        started = time.perf_counter()
        root, entries = build(folder / "backup", args.photos, args.leftovers, args.others)
        print(f"Built a {entries:,}-file backup in {time.perf_counter() - started:.0f} s.")

        def open_and_scan():
            with Backup(root) as backup:
                scan(backup)

        def read_the_library():
            with Backup(root) as backup:
                read_library(backup, scan(backup))

        runs = iter(range(3))

        def save_leftovers():
            with Backup(root) as backup:
                roll = scan(backup)
                export(backup, roll, read_library(backup, roll), folder / f"export{next(runs)}")

        with Backup(root) as backup:
            roll = scan(backup)
            found = len(find_leftovers(roll, read_library(backup, roll)))

        results = [
            (f"Open the backup and sort its camera roll ({entries:,} files listed)", best_of_three(open_and_scan)),
            (f"…and read the Photos database ({args.photos:,} photos)", best_of_three(read_the_library)),
            (f"…and save {found:,} leftover thumbnails with a report", best_of_three(save_leftovers)),
            ("Unlock an encrypted backup with Apple's settings (10M + 10k rounds)", unlock_time()),
        ]

    cpu = next(
        (line.split(":", 1)[1].strip() for line in _cpuinfo() if line.startswith("model name")), platform.machine()
    )
    print(f"\n{cpu}, Python {platform.python_version()}. Best of three runs; the unlock is one run.\n")
    print("| Step | Time |\n| --- | ---: |")
    for step, seconds in results:
        print(f"| {step} | {seconds:.2f} s |")


def _cpuinfo() -> list[str]:
    try:
        return Path("/proc/cpuinfo").read_text().splitlines()
    except OSError:
        return []


if __name__ == "__main__":
    main()

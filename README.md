# Afterimage

[![tests](https://github.com/omdkpatel810-ops/afterimage/actions/workflows/tests.yml/badge.svg)](https://github.com/omdkpatel810-ops/afterimage/actions/workflows/tests.yml)
![coverage 100%](https://img.shields.io/badge/coverage-100%25-brightgreen)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![no dependencies](https://img.shields.io/badge/dependencies-none-blue)
[![MIT license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**Find the photos your iPhone backup still remembers.**

When you delete a photo on an iPhone, the small preview the Photos app made of it doesn't always go with it. Those previews, called thumbnails, end up in the backups your computer makes. Afterimage reads a backup, without changing anything in it, and shows what its camera roll holds: the photos and videos, their thumbnails, and the thumbnails whose photo is no longer there.

That's useful two ways:

- **Get back a lost photo**, as a small copy, when no other copy is left.
- **Check your own privacy**: see what a backup still holds that you thought was deleted.

> **Status: milestone 1 of 5 is done.** Afterimage finds backups, reads them and counts what their camera roll holds. Telling deleted photos apart from ones kept in iCloud, and saving the thumbnails, come next ([roadmap](#roadmap)).

```text
$ afterimage photos --list
Om's iPhone · iOS 17.5.1 · backed up Tue 20 Oct 2026 16:14 CDT

Camera roll in this backup:
  Photos and videos      3  2 photos, 1 video, 5.4 KB
  Thumbnails             7  for 5 photos and videos
  Thumbnail tables       1  .ithmb files, many thumbnails in each
  Edited versions        1
  Photos database    found
  Other files            2

2 photos and videos have thumbnails but no original in this backup.
Each was deleted, or is kept in iCloud and the phone held only a small copy.
This version can't tell those apart yet; the next one will, using the Photos database.

Thumbnails with no original in this backup:
  DCIM/100APPLE/IMG_0004.HEIC                                            2 thumbnails
  PhotoData/CPLAssets/group349/6F1E2D3C-4B5A-4978-8695-A4B3C2D1E0F9.JPG  1 thumbnail
```

<sub>From the fake backup the tests build, so the numbers are small.</sub>

## How a backup is laid out

```mermaid
flowchart LR
    folder["Backup folder"] --> plists["Info, Manifest and Status plists<br/>device, encrypted?, finished?"]
    folder --> manifest[("Manifest.db<br/>one row per file")]
    manifest -->|"SHA-1 of domain and path"| stored["3d/3d0d7e5f…<br/>the file's contents"]
    manifest --> roll["CameraRollDomain"]
    roll --> originals["Media/DCIM/…<br/>photos and videos"]
    roll --> thumbs["Media/PhotoData/Thumbnails/V2/…<br/>thumbnails"]
    roll --> db["Media/PhotoData/Photos.sqlite<br/>the library's database"]
```

1. **Every file is renamed.** A backup stores each file under the SHA-1 of its domain (the app or part of iOS it belongs to) and its path, in a subfolder named after the first two characters. `HomeDomain-Library/SMS/sms.db` becomes `3d/3d0d7e5fb2ce288813306e4d4636395e047a3d28`. `Manifest.db` maps the names back.
2. **Each file's details are an archived object.** `Manifest.db` keeps a file's size and dates as an `MBFile` object in Apple's NSKeyedArchiver format. Afterimage reads it with its own small decoder ([`keyedarchive.py`](afterimage/keyedarchive.py)), using only Python's standard library.
3. **Thumbnails are filed under their photo's path.** `Media/PhotoData/Thumbnails/V2/DCIM/100APPLE/IMG_0001.HEIC/5005.JPG` is a thumbnail of `Media/DCIM/100APPLE/IMG_0001.HEIC`. So a thumbnail whose original isn't in the backup stands out. Either the photo was deleted, or it's kept in iCloud and the phone held only a small copy until it was opened. The Photos database can tell which; reading it is milestone 2.

## Built to only read

- **Manifest.db is opened read-only and "immutable"**, so SQLite writes nothing, not even a lock or journal file.
- **Names from the manifest are checked before they're used as paths.** Only 40 hex characters are accepted, so a tampered backup can't point outside its folder with something like `../../`.
- **A test proves nothing changes.** It fingerprints every file and folder in a backup (contents and modified times) before and after every command.
- **It never prints the phone's serial number, IMEI or phone number,** though the backup has them. A test checks that too.

## Quick start

Requires Python 3.10 or newer, and nothing else: only the standard library is used.

**1. Make a backup.** Connect your iPhone, open Finder (Mac) or the Apple Devices app or iTunes (Windows), and back it up to this computer. Afterimage reads unencrypted backups for now. If yours is encrypted, keep it that way: opening encrypted backups with their own password is milestone 4.

**2. Run it.**

```bash
git clone https://github.com/omdkpatel810-ops/afterimage.git
cd afterimage
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

afterimage list               # the backups on this computer
afterimage info               # one backup's device, date and contents
afterimage photos --list      # what its camera roll holds
```

With more than one backup, name one by its device name, or the start of its folder name: `afterimage photos "Om's iPhone"`. You can also give a backup's folder directly.

Afterimage looks for backups where Apple's tools keep them:

| Computer | Folder |
| --- | --- |
| Mac | `~/Library/Application Support/MobileSync/Backup` |
| Windows: Apple Devices app, or iTunes from the Microsoft Store | `%USERPROFILE%\Apple\MobileSync\Backup` |
| Windows: iTunes from apple.com | `%APPDATA%\Apple Computer\MobileSync\Backup` |
| Linux | wherever `idevicebackup2` saved it; give the path or `--dir` |

On a Mac, macOS keeps that folder private: give your terminal app **Full Disk Access** (System Settings → Privacy & Security → Full Disk Access) first. Afterimage says so if it's blocked.

## What it can't do

- **Bring back the full-size original.** Once a photo is permanently deleted, the original is gone. What can remain is a thumbnail: a small preview, not the photo.
- **Promise there's anything left.** Whether leftover thumbnails exist depends on the iOS version, iCloud Photos settings, and how long ago the photo was deleted.
- **Read encrypted backups yet** (milestone 4), or backups made by iOS 9 or earlier.
- **Read a phone directly.** It reads backups that are already on your computer.

## Use it on your own backups

Afterimage is for backups of your own devices, or ones whose owner has asked you to look. It doesn't get around passwords or encryption, and it never will: an encrypted backup will need its own backup password, the one Finder or iTunes asked for when encryption was turned on.

## Tests

```bash
pip install -e ".[dev]"
python -m pytest --cov     # 88 tests, 100% coverage; fails under 95%
ruff check . && ruff format --check .
```

The tests never use a real phone or a real photo. They build backups in code, shaped like the ones Apple's tools make ([`tests/conftest.py`](tests/conftest.py)), and run on Linux, macOS and Windows. Among them:

- **File names match the published ones.** The SHA-1 names for `sms.db`, the contacts database and `Photos.sqlite` are checked against the ones forensic tools look up.
- **Nothing is written,** by any command (see above).
- **Tampered names can't escape the folder.** Manifest entries like `../../secret.txt` are refused.
- **Path prefixes match exactly.** A property-based test (Hypothesis) checks that case counts and that `%` and `_` are ordinary characters, both of which SQL's `LIKE` gets wrong.
- **Any file record reads back.** Another property-based test archives random paths, sizes and dates, in any language, and decodes them.

## Roadmap

- [x] **1. Read a backup:** find it, read Manifest.db, list its photos and thumbnails without changing anything
- [ ] **2. Match thumbnails to Photos.sqlite:** dates, places, and which photos were deleted
- [ ] **3. Export:** save the leftover thumbnails with their dates and places, plus a privacy report page
- [ ] **4. Encrypted backups,** unlocked with the owner's own backup password
- [ ] **5. Ship it**

## Background

Thumbnails outliving their photos isn't new. Forensic examiners have used them for years, and in 2013 a bug made deleted photos' thumbnails reappear in the iOS 7 Photos app ([AppleInsider](https://appleinsider.com/articles/13/11/14/bug-resurrects-deleted-image-thumbnails-in-ios-7-photos-app-dates-back-to-ios-5)). The `Thumbnails/V2` layout is discussed on [Forensic Focus](https://www.forensicfocus.com/forums/general/file-path-question-please-advise-iphone-var-mobile-media-photodata-thumbnails-v2-photodata-cplassets), and commercial tools such as [Decipher Backup Browser](https://deciphertools.com/blog/photo-metadata-backup-thumbnail-photos/) export these thumbnails from backups. Afterimage is an open-source take: it shows its reasoning, and says what it can't tell.

## License

[MIT](LICENSE) © 2026 Om Patel

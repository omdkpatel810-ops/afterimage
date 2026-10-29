# Changelog

## 1.0.0 (2026-10-29)

- `afterimage demo`: try it in one command on a made-up backup, with no iPhone or real photos.
- Sorting a camera roll is 6.6 times faster (5.98 s to 0.91 s on a 92,001-file backup): each file's archived record is now decoded only when it's needed. `scripts/benchmark.py` measures every step.
- A demo GIF recorded from the program's real output.

## 0.4.0 (2026-10-29)

- Encrypted backups open with their own backup password: keybag, PBKDF2 and AES key unwrap, then each file decrypted as it's read. The password is never kept, and tampered key settings are refused.
- `--password-stdin` for scripts.
- Checked against iphone_backup_decrypt, an independent open-source decrypter.

## 0.3.0 (2026-10-28)

- `afterimage export`: saves leftover thumbnails with their date and place written in as Exif, full-size photos still in Recently Deleted, a CSV and a report page.

## 0.2.0 (2026-10-28)

- Reads the Photos database (including its write-ahead log) to tell deleted photos from ones in Recently Deleted or kept in iCloud, with when and where each was taken.

## 0.1.0 (2026-10-28)

- Finds local backups, reads Manifest.db read-only, and counts what the camera roll holds, including thumbnails whose photo isn't in the backup.

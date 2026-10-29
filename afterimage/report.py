"""The page an export ends with: report.html, a self-contained summary to open in any browser.

It loads nothing from the internet (no fonts, scripts or trackers), so it works offline and nobody learns
it was opened. Place links go to OpenStreetMap only when clicked.
"""

from __future__ import annotations

from datetime import datetime, timezone, tzinfo
from html import escape
from pathlib import PurePosixPath
from urllib.parse import quote

from . import __version__
from .export import FULL_SIZE, SAVED_LABELS, Export, Saved
from .library import GONE, IN_LIBRARY, RECENTLY_DELETED, UNKNOWN

ORDER = (GONE, RECENTLY_DELETED, FULL_SIZE, IN_LIBRARY, UNKNOWN)
EXPLANATIONS = {
    GONE: "Deleted for good. The Photos app's small previews of them stayed behind, and they're all that's left.",
    RECENTLY_DELETED: "In Recently Deleted. The phone keeps them for about 30 days and can restore them at full"
    " size from that album.",
    FULL_SIZE: "Also in Recently Deleted, and their full-size originals are still in this backup: these are full"
    " copies, not thumbnails.",
    IN_LIBRARY: "Still in the Photos library. Their originals aren't in this backup, usually because iCloud Photos"
    " keeps them in iCloud.",
    UNKNOWN: "The Photos database couldn't be read, so each of these was either deleted or is kept in iCloud.",
}
SHOWN_INLINE = (".jpg", ".jpeg", ".png", ".gif", ".webp")

STYLE = """
:root {
  color-scheme: light dark;
  --bg: #f6f5f2; --card: #ffffff; --text: #1d1d1f; --muted: #66656b; --line: #e3e1dc;
  --accent: #b4441b; --tile: #ecebe7;
}
@media (prefers-color-scheme: dark) {
  :root { --bg: #151517; --card: #1f1f22; --text: #f2f1ee; --muted: #a3a2a8; --line: #2f2f33;
          --accent: #f08a5d; --tile: #29292d; }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
       font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
main { max-width: 1100px; margin: 0 auto; padding: 40px 16px 64px; }
.eyebrow { margin: 0; color: var(--accent); font-weight: 600; letter-spacing: .04em; text-transform: uppercase;
           font-size: .8rem; }
h1 { margin: 4px 0 8px; font-size: clamp(1.8rem, 4vw, 2.6rem); line-height: 1.15; }
h2 { margin: 0 0 4px; font-size: 1.3rem; }
.meta, .explain, footer { color: var(--muted); }
.summary { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; margin: 28px 0 8px; }
.stat { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 14px 16px; }
.stat b { display: block; font-size: 1.9rem; line-height: 1.1; font-variant-numeric: tabular-nums; }
.stat span { color: var(--muted); font-size: .9rem; }
section.group { margin-top: 40px; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: 14px; margin-top: 14px; }
figure { margin: 0; background: var(--card); border: 1px solid var(--line); border-radius: 12px; overflow: hidden; }
figure a.picture { display: block; aspect-ratio: 1; background: var(--tile); text-decoration: none; }
figure img { width: 100%; height: 100%; object-fit: contain; display: block; }
.tile { height: 100%; display: grid; place-items: center; color: var(--muted); font-weight: 600; }
figcaption { padding: 10px 12px 12px; font-size: .88rem; }
figcaption strong { display: block; font-size: .95rem; overflow-wrap: anywhere; }
figcaption ul { list-style: none; margin: 4px 0 0; padding: 0; color: var(--muted); }
a { color: var(--accent); }
.advice { margin-top: 48px; background: var(--card); border: 1px solid var(--line); border-radius: 12px;
          padding: 20px 24px; }
.advice li { margin: 6px 0; }
footer { margin-top: 32px; font-size: .85rem; }
"""


def render(export: Export, tz: tzinfo | None = None, now: datetime | None = None) -> str:
    info = export.info
    now = now or datetime.now(timezone.utc)
    groups: dict[str, list[Saved]] = {}
    for item in export.saved:
        groups.setdefault(item.status, []).append(item)
    order = [status for status in ORDER if status in groups]

    details = [info.name, f"iOS {info.ios_version or '?'}", f"backed up {_when(info.last_backup, tz)}"]
    parts = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta name="referrer" content="no-referrer">',
        f"<title>What {escape(info.name)}'s backup still holds</title>",
        f"<style>{STYLE}</style>",
        "</head>",
        "<body>",
        "<main>",
        "<header>",
        '<p class="eyebrow">Afterimage report</p>',
        "<h1>What this backup still holds</h1>",
        f'<p class="meta">{escape(" · ".join(details))}. Report made {escape(_when(now, tz))}.</p>',
        "</header>",
    ]
    if order:
        parts.append('<section class="summary" aria-label="Summary">')
        for status in order:
            parts.append(
                f'<div class="stat"><b>{len(groups[status]):,}</b><span>{escape(SAVED_LABELS[status])}</span></div>'
            )
        parts.append("</section>")
    else:
        parts.append('<p class="explain">Nothing to show: every thumbnail\'s photo or video is in this backup.</p>')

    for status in order:
        parts += [
            f'<section class="group" id="{escape(status.replace(" ", "-"))}">',
            f"<h2>{escape(SAVED_LABELS[status])} ({len(groups[status]):,})</h2>",
            f'<p class="explain">{escape(EXPLANATIONS[status])}</p>',
            '<div class="grid">',
            *(_card(item, tz) for item in groups[status]),
            "</div>",
            "</section>",
        ]

    advice = []
    if RECENTLY_DELETED in groups or FULL_SIZE in groups:
        advice.append(
            "<li><b>Get back what's in Recently Deleted.</b> On the phone, open the Recently Deleted album in"
            " Photos and restore it. That works for about 30 days after it was deleted.</li>"
        )
    advice += [
        "<li><b>Keep this folder private.</b> It holds copies of your photos. Delete it when you're done.</li>",
        "<li><b>Encrypt your local backups.</b> In Finder, the Apple Devices app or iTunes, turn on"
        " “Encrypt local backup”. An encrypted backup can't be read without its password.</li>",
        "<li><b>Delete old backups you don't need.</b> Each one keeps whatever the phone held on the day it was"
        " made.</li>",
    ]
    parts += [
        '<section class="advice">',
        "<h2>What you can do</h2>",
        "<ul>",
        *advice,
        "</ul>",
        "</section>",
        f"<footer>Made by afterimage {escape(__version__)}, which only reads backups and never changes them."
        " Thumbnails are the Photos app's small previews, not the original photos.</footer>",
        "</main>",
        "</body>",
        "</html>",
    ]
    return "\n".join(parts) + "\n"


def _card(item: Saved, tz: tzinfo | None) -> str:
    stored_as = item.original.rpartition("/")[2]
    name = (item.asset.original_name if item.asset else None) or stored_as  # the name it was taken with
    href = escape(quote(item.file))
    extension = PurePosixPath(item.file).suffix.lower()
    if extension in SHOWN_INLINE:
        picture = f'<img src="{href}" alt="{escape(name)}" loading="lazy">'
    else:
        picture = f'<span class="tile">{escape(extension.lstrip(".").upper() or "file")}</span>'
    lines = []
    asset = item.asset
    if asset is not None:
        if asset.kind:
            lines.append(asset.kind.capitalize())
        if asset.taken:
            lines.append(f"Taken {escape(_when(asset.taken, tz))}")
        if asset.place:
            lat, lon = asset.place
            url = f"https://www.openstreetmap.org/?mlat={lat:.6f}&amp;mlon={lon:.6f}#map=16/{lat:.6f}/{lon:.6f}"
            lines.append(f'At <a href="{url}" rel="noopener noreferrer">{lat:.5f}, {lon:.5f}</a>')
        if asset.trashed:
            lines.append(f"Deleted {escape(_when(asset.trashed_on, tz))}" if asset.trashed_on else "Deleted")
        if asset.hidden:
            lines.append("In the Hidden album")
        if asset.original_name:
            lines.append(f"Stored in the backup as {escape(stored_as)}")
    elif item.thumbnail_saved:
        lines.append(
            f"Thumbnail saved {escape(_when(item.thumbnail_saved, tz))}, about when the photo reached the phone"
        )
    return (
        f'<figure><a class="picture" href="{href}">{picture}</a>'
        f"<figcaption><strong>{escape(name)}</strong>"
        f"<ul>{''.join(f'<li>{line}</li>' for line in lines)}</ul></figcaption></figure>"
    )


def _when(moment: datetime | None, tz: tzinfo | None) -> str:
    return "date unknown" if moment is None else moment.astimezone(tz).strftime("%a %d %b %Y %H:%M %Z")

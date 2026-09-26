#!/usr/bin/env python3
"""Re-record the yt-dlp JSON fixtures used by the test suite.

The tests must never touch the network (Phase 1 exit criterion), so they run against recorded
yt-dlp output. This script is how that output is (re-)captured — by a human, deliberately, when
yt-dlp's JSON shape changes:

    python tools/record_fixtures.py

It hits YouTube. It is not run by the test suite or by CI.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

#: Bulky keys we never read. Dropping them keeps fixtures reviewable (600 kB -> ~10 kB) without
#: touching any field :mod:`tunebox.sources.ytdlp_tags` consumes.
DROP_KEYS = (
    "formats",
    "automatic_captions",
    "subtitles",
    "heatmap",
    "requested_formats",
    "requested_downloads",
    "http_headers",
    "chapters",
    "_format_sort_fields",
)

TARGETS = {
    # ordinary upload: no music metadata, "Artist - Title" in the title
    "video_plain_upload.json": ("video", "https://www.youtube.com/watch?v=iukUMRlaBBE"),
    # art track: real track/artist/album fields from YouTube Music
    "video_art_track.json": ("video", "https://www.youtube.com/watch?v=vkOZNJYAZ7c"),
    # CJK conventions: "Artist《Title》[Official MV]" on a record label's channel, where the
    # channel name must NOT become the artist
    "video_cjk_label_upload.json": ("video", "https://www.youtube.com/watch?v=Sm_DVsixxsk"),
    # CJK, inverted: "《Title》Artist【Official MV】" on the artist's own channel
    "video_cjk_inverted.json": ("video", "https://www.youtube.com/watch?v=9elvYViwP4g"),
    # CJK, unsplittable: a lyric-video channel with no separator in the title
    "video_cjk_lyric_channel.json": ("video", "https://www.youtube.com/watch?v=7MEuDXdjbIE"),
    # CJK, bare "MV" suffix glued to the title: "陳奕迅 - 好久不見MV"
    "video_cjk_mv_suffix.json": ("video", "https://www.youtube.com/watch?v=UsFQ9kbpQhw"),
    # CJK, leading hashtag names the performer; the channel is a TV show
    "video_cjk_hashtag.json": ("video", "https://www.youtube.com/watch?v=nGQR5nyyhnA"),
    # CJK, square-bracket title on a label channel: "JAMES 楊永聰 [好久不見 ...] Official MV"
    "video_cjk_square_brackets.json": ("video", "https://www.youtube.com/watch?v=5gx4LiFCfUU"),
    # CJK clickbait prose around the title — must not become an artist or a directory
    "video_cjk_clickbait.json": ("video", "https://www.youtube.com/watch?v=c1e0OSiVxSo"),
    # CJK with a lyric teaser in 『』 after the real title
    "video_cjk_lyric_teaser.json": ("video", "https://www.youtube.com/watch?v=I2hva376wbk"),
    "search_kraftwerk_autobahn.json": ("search", "ytsearch2:kraftwerk autobahn"),
    "search_cjk.json": ("search", "ytsearch3:k歌之王"),
}


def run_ytdlp(args: list[str]) -> dict:
    argv = [sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-colors", *args]
    completed = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=180)
    if completed.returncode != 0:
        raise SystemExit(f"yt-dlp failed: {completed.stderr.strip()[-500:]}")
    return json.loads(completed.stdout)


def trim(payload: dict) -> dict:
    cleaned = {k: v for k, v in payload.items() if k not in DROP_KEYS}
    if isinstance(cleaned.get("description"), str):
        cleaned["description"] = cleaned["description"][:300]
    # Keep the best thumbnail only: the parsing path stays exercised, the fixture stays small.
    thumbs = cleaned.get("thumbnails")
    if isinstance(thumbs, list) and thumbs:
        cleaned["thumbnails"] = thumbs[-1:]
    entries = cleaned.get("entries")
    if isinstance(entries, list):
        cleaned["entries"] = [
            trim(entry) if isinstance(entry, dict) else entry for entry in entries
        ]
    return cleaned


def main() -> int:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for name, (kind, target) in TARGETS.items():
        if kind == "video":
            args = ["--no-playlist", "--skip-download", "--dump-single-json", target]
        else:
            args = ["--flat-playlist", "--dump-single-json", target]
        print(f"recording {name} ...", file=sys.stderr)
        payload = trim(run_ytdlp(args))
        path = FIXTURES / name
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"  wrote {path} ({path.stat().st_size} bytes)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

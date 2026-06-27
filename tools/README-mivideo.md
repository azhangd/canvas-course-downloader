# UMich MiVideo Export Companion

`umich_mivideo_export.py` downloads MiVideo/Kaltura assets embedded in a
UMich Canvas course. It is meant for personal course archiving from your own
logged-in Chrome session.

The script:

- reads Canvas cookies from a local Chrome profile
- discovers Canvas pages that embed MiVideo/Kaltura tools
- opens each video page with Playwright so the player exposes its official
  Kaltura asset metadata
- downloads direct Kaltura `serveFlavor` video files, captions, and transcript
  text when available
- writes a `videos_manifest.json` without signed media URLs

It does not archive HLS stream segments directly, and it does not store signed
Kaltura URLs.

## Setup

From this repository:

```bash
python3 -m venv .venv-mivideo
. .venv-mivideo/bin/activate
python -m pip install -r tools/requirements-mivideo.txt
```

The script uses the installed Google Chrome app by default, so a separate
Playwright browser install is usually not needed on macOS.

## Run

```bash
python tools/umich_mivideo_export.py \
  --course-url https://umich.instructure.com/courses/834963 \
  --chrome-profile "Profile 2" \
  --output-dir ~/Downloads/canvas-video-exports \
  --quality max-720
```

Useful options:

- `--quality max-720`: choose the best MP4 at or below 720p, falling back when
  needed. This is the default.
- `--quality best`: choose the best non-source web flavor.
- `--quality smallest`: choose the smallest non-source web flavor.
- `--quality source`: choose the original upload when exposed.
- `--metadata-only`: discover videos, captions, and flavors without downloading
  video files.
- `--max-videos 1`: test the flow on one video before a full run.
- `--headful`: show the browser window during export.


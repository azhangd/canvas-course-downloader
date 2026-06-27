# UMich Course Archive Workflow

Use `umich_course_archive.py` to archive a UMich Canvas course into the local
course library at `/Users/alexzhang/Developer/projects/umich`.

It runs both exporters:

- `canvas_course_export.py` for Canvas pages, assignments, grades, submissions,
  linked files, syllabus, and modules
- `umich_mivideo_export.py` for MiVideo/Kaltura MP4s, captions, and transcripts

The final course folder is organized as:

```text
Course Name/
├── archives/
├── canvas/
└── mivideo/
```

## Setup

From this repository:

```bash
python3 -m venv .venv-umich-export
. .venv-umich-export/bin/activate
python -m pip install -r tools/requirements-umich-export.txt
```

The scripts use the installed Google Chrome app and your logged-in Chrome
Canvas profile. On this machine that has been `Profile 2`.

## Archive One Course

```bash
python tools/umich_course_archive.py \
  --course-url https://umich.instructure.com/courses/844406 \
  --chrome-profile "Profile 2" \
  --target-root /Users/alexzhang/Developer/projects/umich \
  --quality max-720 \
  --overwrite
```

Replace only the `--course-url` value for future courses.

Useful options:

- `--quality max-720`: best video flavor at or below 720p, the default
- `--quality smallest`: smaller MP4s when disk size matters more than quality
- `--metadata-only`: export Canvas materials plus video/caption metadata only
- `--keep-work`: keep the temporary work folder for debugging
- `--headful`: show Chrome while the MiVideo exporter runs


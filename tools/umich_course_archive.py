#!/usr/bin/env python3
"""One-command UMich Canvas + MiVideo course archive workflow."""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import shutil
from types import SimpleNamespace

import canvas_course_export
import umich_mivideo_export


DEFAULT_TARGET_ROOT = "/Users/alexzhang/Developer/projects/umich"
DEFAULT_WORK_ROOT = ".umich-export-work"


def copy_tree_contents(src: pathlib.Path, dest: pathlib.Path, overwrite: bool) -> None:
    if dest.exists():
        if not overwrite:
            raise FileExistsError(f"Destination already exists: {dest} (pass --overwrite to replace it)")
        shutil.rmtree(dest)
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".DS_Store", "__pycache__"))


def only_export_folder(root: pathlib.Path, marker: str) -> pathlib.Path:
    matches = sorted(path for path in root.iterdir() if path.is_dir() and marker in path.name)
    if len(matches) != 1:
        raise RuntimeError(f"Expected one folder containing {marker!r} under {root}, found {len(matches)}")
    return matches[0]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--course-url", required=True, help="Canvas course URL")
    parser.add_argument("--chrome-profile", default=canvas_course_export.DEFAULT_CHROME_PROFILE)
    parser.add_argument("--target-root", default=DEFAULT_TARGET_ROOT)
    parser.add_argument("--work-root", default=DEFAULT_WORK_ROOT)
    parser.add_argument("--quality", default="max-720", choices=["max-720", "best", "smallest", "source"])
    parser.add_argument("--metadata-only", action="store_true", help="Skip video MP4 downloads")
    parser.add_argument("--headful", action="store_true", help="Show the browser during MiVideo export")
    parser.add_argument("--keep-work", action="store_true", help="Keep temporary export work folder")
    parser.add_argument("--overwrite", action="store_true", help="Replace existing target course folder contents")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    canvas_domain, course_id = canvas_course_export.parse_course(
        args.course_url, None, canvas_course_export.DEFAULT_CANVAS_DOMAIN
    )
    work_root = pathlib.Path(args.work_root).expanduser().resolve() / f"course-{course_id}"
    if work_root.exists():
        if not args.overwrite:
            raise FileExistsError(f"Work folder already exists: {work_root} (pass --overwrite to replace it)")
        shutil.rmtree(work_root)
    work_root.mkdir(parents=True)

    try:
        canvas_args = SimpleNamespace(
            course_url=args.course_url,
            course_id=None,
            canvas_domain=canvas_domain,
            chrome_profile=args.chrome_profile,
            output_dir=str(work_root / "canvas-export"),
            zip=str(work_root / "canvas-export.zip"),
            overwrite=True,
        )
        canvas_exporter = canvas_course_export.CanvasExporter(canvas_args)
        canvas_zip, canvas_manifest = canvas_exporter.export_course()

        video_args = SimpleNamespace(
            course_url=args.course_url,
            course_id=None,
            canvas_domain=canvas_domain,
            chrome_profile=args.chrome_profile,
            chrome_executable=umich_mivideo_export.DEFAULT_CHROME_EXECUTABLE,
            output_dir=str(work_root / "mivideo-export"),
            zip=str(work_root / "mivideo-export.zip"),
            quality=args.quality,
            metadata_only=args.metadata_only,
            max_videos=None,
            wait_after_play_ms=12000,
            overwrite=True,
            headful=args.headful,
        )
        asyncio.run(umich_mivideo_export.run_export(video_args))

        course_name = canvas_manifest["course"]
        course_dir_name = canvas_course_export.safe_name(course_name, max_len=120)
        dest_root = pathlib.Path(args.target_root).expanduser().resolve() / course_dir_name
        archives_dir = dest_root / "archives"
        archives_dir.mkdir(parents=True, exist_ok=True)

        if args.overwrite:
            for subdir in ("canvas", "mivideo"):
                path = dest_root / subdir
                if path.exists():
                    shutil.rmtree(path)
        elif (dest_root / "canvas").exists() or (dest_root / "mivideo").exists():
            raise FileExistsError(f"Course folder already has exports: {dest_root} (pass --overwrite to replace them)")

        video_output = only_export_folder(work_root / "mivideo-export", " - MiVideo Export ")

        canvas_archive = archives_dir / f"{pathlib.Path(canvas_exporter.out_root).name}.zip"
        video_archive = archives_dir / f"{video_output.name}.zip"
        if not args.overwrite and (canvas_archive.exists() or video_archive.exists()):
            raise FileExistsError(f"Archive already exists under {archives_dir} (pass --overwrite to replace it)")
        copy_tree_contents(pathlib.Path(canvas_exporter.out_root), dest_root / "canvas", overwrite=True)
        copy_tree_contents(video_output, dest_root / "mivideo", overwrite=True)
        shutil.copy2(canvas_zip, canvas_archive)
        shutil.copy2(work_root / "mivideo-export.zip", video_archive)

        summary = {
            "course": course_name,
            "course_id": course_id,
            "target": str(dest_root),
            "canvas_archive": str(canvas_archive),
            "mivideo_archive": str(video_archive),
            "canvas_counts": canvas_manifest.get("counts", {}),
        }
        video_manifest = dest_root / "mivideo" / "videos_manifest.json"
        if video_manifest.exists():
            video_data = json.loads(video_manifest.read_text(encoding="utf-8"))
            summary["mivideo_counts"] = video_data.get("counts", {})
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    finally:
        if not args.keep_work and work_root.exists():
            shutil.rmtree(work_root)


if __name__ == "__main__":
    main()

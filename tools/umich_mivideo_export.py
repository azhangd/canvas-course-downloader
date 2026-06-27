#!/usr/bin/env python3
"""Export UMich MiVideo/Kaltura assets linked from a Canvas course.

This companion script is intentionally separate from the browser extension.
It uses your existing Chrome Canvas cookies, opens each Canvas video page in
Playwright, lets the Kaltura player expose its official asset metadata, then
downloads direct Kaltura download URLs and caption files. Signed media URLs are
used only during the run and are not written to the manifest.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import json
import pathlib
import re
import shutil
import sys
import time
import zipfile
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

import browser_cookie3
import requests
from bs4 import BeautifulSoup
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright


DEFAULT_CANVAS_DOMAIN = "https://umich.instructure.com"
DEFAULT_CHROME_PROFILE = "Profile 2"
DEFAULT_CHROME_EXECUTABLE = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def safe_name(value: str | None, fallback: str = "untitled", max_len: int = 130) -> str:
    value = str(value or "").strip()
    value = re.sub(r"[\x00-\x1f\x7f]", "", value)
    value = re.sub(r"[\u200b-\u200d\ufeff]", "", value)
    value = value.replace("\u00a0", " ")
    value = re.sub(r'[/\\?%*:|"<>]', "-", value)
    value = re.sub(r"^\.+", "", value)
    value = re.sub(r"[. ]+$", "", value).strip()
    value = re.sub(r"\s+", " ", value)
    if not value:
        value = fallback
    return value[:max_len].rstrip(" .") or fallback


def parse_course(value: str | None, course_id: str | None, canvas_domain: str) -> tuple[str, str]:
    if course_id:
        return canvas_domain.rstrip("/"), str(course_id)
    if not value:
        raise SystemExit("Pass --course-url or --course-id.")
    match = re.search(r"https?://[^/]+/courses/(\d+)", value)
    if match:
        parsed = urlparse(value)
        return f"{parsed.scheme}://{parsed.netloc}", match.group(1)
    if re.fullmatch(r"\d+", value):
        return canvas_domain.rstrip("/"), value
    raise SystemExit(f"Could not parse Canvas course from: {value}")


def chrome_cookie_file(profile: str) -> pathlib.Path:
    return pathlib.Path.home() / "Library/Application Support/Google/Chrome" / profile / "Cookies"


def load_cookie_jar(profile: str, host: str):
    cookie_file = chrome_cookie_file(profile)
    if not cookie_file.exists():
        raise FileNotFoundError(f"Chrome cookie DB not found: {cookie_file}")
    return browser_cookie3.chrome(cookie_file=str(cookie_file), domain_name=host)


def cookie_jar_to_playwright(cj) -> list[dict[str, Any]]:
    cookies = []
    for c in cj:
        cookies.append(
            {
                "name": c.name,
                "value": c.value,
                "domain": c.domain,
                "path": c.path or "/",
                "expires": int(c.expires) if c.expires else -1,
                "httpOnly": bool(getattr(c, "_rest", {}).get("HttpOnly")),
                "secure": bool(c.secure),
                "sameSite": "Lax",
            }
        )
    return cookies


def make_session(cj) -> requests.Session:
    session = requests.Session()
    session.cookies.update(cj)
    session.headers.update(
        {
            "Accept": "application/json+canvas-string-ids",
            "User-Agent": "Mozilla/5.0 Canvas MiVideo Export",
        }
    )
    return session


def get_with_retries(session: requests.Session, url: str, **kwargs) -> requests.Response:
    timeout = kwargs.pop("timeout", 45)
    for attempt in range(4):
        try:
            response = session.get(url, timeout=timeout, **kwargs)
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 3:
                    time.sleep(min(2**attempt, 8))
                    continue
            return response
        except requests.RequestException:
            if attempt == 3:
                raise
            time.sleep(min(2**attempt, 8))
    raise RuntimeError("unreachable")


def get_json(session: requests.Session, url: str) -> Any:
    response = get_with_retries(session, url)
    if not response.ok:
        raise requests.HTTPError(f"{response.status_code} for {url}", response=response)
    return response.json()


def all_pages(session: requests.Session, url: str) -> list[Any]:
    items: list[Any] = []
    while url:
        response = get_with_retries(session, url)
        if not response.ok:
            raise requests.HTTPError(f"{response.status_code} for {url}", response=response)
        data = response.json()
        if isinstance(data, list):
            items.extend(data)
        else:
            items.append(data)
        link = response.headers.get("Link", "")
        match = re.search(r'<([^>]+)>;\s*rel="next"', link)
        url = match.group(1) if match else ""
    return items


@dataclass
class VideoPage:
    title: str
    module: str
    module_position: int
    source_url: str
    external_tool_url: str


@dataclass
class VideoState:
    entry_id: str | None = None
    player_url: str | None = None
    media_entries: dict[str, dict[str, Any]] = field(default_factory=dict)
    flavors: dict[str, dict[str, Any]] = field(default_factory=dict)
    captions: dict[str, dict[str, Any]] = field(default_factory=dict)
    download_urls: list[str] = field(default_factory=list)
    caption_playlists: list[dict[str, str]] = field(default_factory=list)
    transcript_cues: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def find_embedded_video_links(canvas_domain: str, html: str) -> list[str]:
    soup = BeautifulSoup(html or "", "html.parser")
    links: list[str] = []
    for tag in soup.find_all(["iframe", "embed", "video", "source"]):
        src = tag.get("src")
        if not src:
            continue
        absolute = urljoin(canvas_domain, src)
        if re.search(r"external_tools/retrieve|mivideo|kaltura", absolute, re.I):
            links.append(absolute)
    return list(dict.fromkeys(links))


def collect_video_pages(
    session: requests.Session, canvas_domain: str, course_id: str
) -> tuple[dict[str, Any], list[VideoPage]]:
    api_root = f"{canvas_domain}/api/v1/courses/{course_id}"
    course = get_json(session, f"{api_root}?include[]=term&include[]=syllabus_body")
    modules = all_pages(session, f"{api_root}/modules?per_page=100")
    videos: list[VideoPage] = []
    seen: set[tuple[str, str]] = set()

    for module_index, module in enumerate(modules, 1):
        module_name = module.get("name") or f"Module {module_index}"
        items = all_pages(session, f"{api_root}/modules/{module['id']}/items?per_page=100")
        for item in items:
            if item.get("type") != "Page" or not item.get("url"):
                continue
            page = get_json(session, item["url"])
            title = page.get("title") or item.get("title") or "Video"
            links = find_embedded_video_links(canvas_domain, page.get("body") or "")
            for link in links:
                key = (page.get("html_url") or item.get("html_url") or title, link)
                if key in seen:
                    continue
                seen.add(key)
                videos.append(
                    VideoPage(
                        title=title,
                        module=module_name,
                        module_position=module_index,
                        source_url=page.get("html_url") or item.get("html_url") or "",
                        external_tool_url=link,
                    )
                )
    return course, videos


def clean_manifest_url(url: str | None) -> str | None:
    if not url:
        return url
    text = str(url)
    text = re.sub(r"(/ks/)[^/?#]+", r"\1[REDACTED]", text, flags=re.I)
    text = re.sub(r"([?&](?:ks|aeauth|token|id_token|state|lti_message_hint|jwt|session)=[^&#]+)", "[REDACTED]", text, flags=re.I)
    return text


def as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return [value]


def collect_kaltura_object(state: VideoState, value: Any) -> None:
    if isinstance(value, list):
        if value and all(isinstance(v, str) for v in value):
            for url in value:
                if "serveFlavor" in url and url not in state.download_urls:
                    state.download_urls.append(url)
            return
        for item in value:
            collect_kaltura_object(state, item)
        return

    if not isinstance(value, dict):
        return

    object_type = value.get("objectType")
    if object_type == "KalturaMediaEntry" and value.get("id"):
        state.media_entries[str(value["id"])] = value
        state.entry_id = state.entry_id or str(value["id"])
    elif object_type == "KalturaFlavorAsset" and value.get("id"):
        state.flavors[str(value["id"])] = value
    elif object_type == "KalturaCaptionAsset" and value.get("id"):
        state.captions[str(value["id"])] = value

    objects = value.get("objects")
    if isinstance(objects, list) and objects:
        if all(isinstance(cue, dict) and "startTime" in cue and "content" in cue for cue in objects):
            state.transcript_cues.extend(objects)
        for item in objects:
            collect_kaltura_object(state, item)

    for key in ("results", "sources", "flavorAssets", "captionAssets"):
        if key in value:
            collect_kaltura_object(state, value[key])


def extract_entry_id(url: str | None) -> str | None:
    if not url:
        return None
    match = re.search(r"/entryid/([^/]+)|/entryId/([^/]+)|entry_id/([^/?#]+)", url, re.I)
    if not match:
        return None
    return next(group for group in match.groups() if group)


def extract_flavor_id(url: str) -> str | None:
    match = re.search(r"/flavorId/([^/]+)", url)
    return match.group(1) if match else None


def filename_ext_from_url(url: str, fallback: str = "mp4") -> str:
    decoded = unquote(url)
    match = re.search(r"/name/a\.([a-zA-Z0-9]+)", decoded)
    if match:
        return match.group(1).lower()
    match = re.search(r"\.([a-zA-Z0-9]{2,5})(?:\?|$)", decoded)
    return match.group(1).lower() if match else fallback


def flavor_score(flavor: dict[str, Any], quality: str) -> tuple:
    width = int(flavor.get("width") or 0)
    height = int(flavor.get("height") or 0)
    bitrate = int(float(flavor.get("bitrate") or 0))
    size = int(float(flavor.get("sizeInBytes") or flavor.get("size") or 0))
    is_original = bool(flavor.get("isOriginal"))
    is_web = bool(flavor.get("isWeb"))
    if quality == "source":
        return (1 if is_original else 0, size, height, width, bitrate)
    if quality == "smallest":
        return (0 if is_original else 1, 0 if size else 1, -size, -height, -width, -bitrate)
    if quality == "best":
        return (0 if is_original else 1, 1 if is_web else 0, height, width, bitrate, size)
    if quality == "max-720":
        under = height and height <= 720
        return (0 if is_original else 1, 1 if under else 0, -max(height - 720, 0), height, width, bitrate, size)
    return (0 if is_original else 1, height, width, bitrate, size)


def choose_download_url(state: VideoState, quality: str) -> tuple[str | None, dict[str, Any] | None]:
    candidates: list[tuple[tuple, str, dict[str, Any]]] = []
    for url in state.download_urls:
        flavor_id = extract_flavor_id(url)
        flavor = state.flavors.get(flavor_id or "", {})
        if flavor and str(flavor.get("fileExt", "")).lower() not in {"mp4", "mov", "m4v", "webm"}:
            continue
        candidates.append((flavor_score(flavor, quality), url, flavor | {"id": flavor_id or ""}))
    if not candidates:
        return None, None
    candidates.sort(reverse=True, key=lambda item: item[0])
    return candidates[0][1], candidates[0][2]


def parse_caption_playlist(url: str, body: str) -> list[str]:
    urls: list[str] = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        urls.append(urljoin(url, line))
    return urls


def combine_vtt(chunks: list[str]) -> str:
    output = ["WEBVTT", ""]
    for chunk in chunks:
        for line in chunk.splitlines():
            if line.strip() == "WEBVTT":
                continue
            if line.startswith("X-TIMESTAMP-MAP"):
                continue
            output.append(line)
        if output[-1] != "":
            output.append("")
    return "\n".join(output).rstrip() + "\n"


def format_ms(value: Any) -> str:
    try:
        ms = int(float(value))
    except (TypeError, ValueError):
        ms = 0
    seconds, milli = divmod(ms, 1000)
    minutes, sec = divmod(seconds, 60)
    hours, minute = divmod(minutes, 60)
    return f"{hours:02d}:{minute:02d}:{sec:02d}.{milli:03d}"


def transcript_text(cues: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    seen: set[tuple[str, str]] = set()
    for cue in cues:
        parts = []
        for content in cue.get("content") or []:
            if isinstance(content, dict) and content.get("text"):
                parts.append(str(content["text"]).replace("\n", " ").strip())
        text = " ".join(part for part in parts if part)
        if not text:
            continue
        key = (str(cue.get("startTime")), text)
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"[{format_ms(cue.get('startTime'))}] {text}")
    return "\n".join(lines).rstrip() + "\n" if lines else ""


def download_stream(session: requests.Session, url: str, path: pathlib.Path, overwrite: bool) -> int:
    if path.exists() and path.stat().st_size > 0 and not overwrite:
        return path.stat().st_size
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    with get_with_retries(session, url, stream=True, allow_redirects=True, timeout=120) as response:
        if not response.ok:
            raise requests.HTTPError(f"{response.status_code} for media download", response=response)
        with tmp.open("wb") as fh:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    fh.write(chunk)
    tmp.replace(path)
    return path.stat().st_size


async def click_player(frame, page, state: VideoState) -> None:
    selectors = [
        'button[aria-label*="Play" i]:visible',
        '[role="button"][aria-label*="Play" i]:visible',
        ".playkit-pre-playback-play-button",
        ".playkit-control-play-pause",
        'button[title*="Play" i]',
    ]
    for selector in selectors:
        try:
            loc = frame.locator(selector).first
            if await loc.count():
                await loc.click(timeout=6000, force=True)
                return
        except Exception as exc:
            state.warnings.append(f"Play click with {selector}: {exc}")
    try:
        handle = await frame.frame_element()
        box = await handle.bounding_box()
        if box:
            await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    except Exception as exc:
        state.warnings.append(f"Frame-center play click failed: {exc}")


async def click_download_menu(frame, state: VideoState) -> None:
    selectors = [
        'button[aria-label*="Download" i]',
        '[role="button"][aria-label*="Download" i]',
        'a[aria-label*="Download" i]',
        'button[title*="Download" i]',
        'a[title*="Download" i]',
    ]
    for selector in selectors:
        try:
            loc = frame.locator(selector).first
            if await loc.count():
                await loc.click(timeout=4000, force=True)
                return
        except Exception as exc:
            state.warnings.append(f"Download menu click with {selector}: {exc}")


async def inspect_video(context, video: VideoPage, wait_after_play_ms: int) -> VideoState:
    state = VideoState()
    page = await context.new_page()
    response_tasks: list[asyncio.Task] = []

    async def handle_response(response) -> None:
        url = response.url
        try:
            if "api_v3" in url and ("multirequest" in url or "user/action/get" in url or "userentry/action/list" in url):
                text = await response.text()
                with contextlib.suppress(json.JSONDecodeError):
                    collect_kaltura_object(state, json.loads(text))
            elif "playManifest" in url and "applehttp" in url:
                text = await response.text()
                # Keep only caption playlist URLs; HLS video segment URLs are not persisted.
                for line in text.splitlines():
                    if "caption_captionasset/action/serveWebVTT" in line:
                        match = re.search(r'URI="([^"]+)"', line)
                        if match:
                            state.caption_playlists.append({"url": match.group(1), "body": ""})
            elif "caption_captionasset/action/serveWebVTT" in url and url.endswith("a.m3u8"):
                text = await response.text()
                state.caption_playlists.append({"url": url, "body": text})
        except Exception as exc:
            state.warnings.append(f"Response parse failed for {clean_manifest_url(url)}: {exc}")

    page.on("response", lambda response: response_tasks.append(asyncio.create_task(handle_response(response))))
    try:
        await page.goto(video.source_url, wait_until="domcontentloaded", timeout=60000)
        with contextlib.suppress(PlaywrightTimeoutError):
            await page.wait_for_load_state("networkidle", timeout=45000)
        await page.wait_for_timeout(3000)
        frame = next((f for f in page.frames if re.search(r"mivideo|kaltura|entryid", f.url, re.I)), None)
        if frame is None and video.external_tool_url:
            await page.goto(video.external_tool_url, wait_until="domcontentloaded", timeout=60000)
            with contextlib.suppress(PlaywrightTimeoutError):
                await page.wait_for_load_state("networkidle", timeout=45000)
            await page.wait_for_timeout(3000)
            frame = next((f for f in page.frames if re.search(r"mivideo|kaltura|entryid", f.url, re.I)), None)
        if frame is None:
            state.warnings.append("Could not find MiVideo/Kaltura frame")
            return state
        state.player_url = frame.url
        state.entry_id = extract_entry_id(frame.url) or state.entry_id
        await click_player(frame, page, state)
        await page.wait_for_timeout(wait_after_play_ms)
        if not state.download_urls:
            await click_download_menu(frame, state)
            await page.wait_for_timeout(5000)
        if response_tasks:
            await asyncio.gather(*response_tasks, return_exceptions=True)
    finally:
        await page.close()
    return state


def write_json(path: pathlib.Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def record_from_video(video: VideoPage, state: VideoState) -> dict[str, Any]:
    entry = state.media_entries.get(state.entry_id or "", {})
    return {
        "title": video.title,
        "module": video.module,
        "module_position": video.module_position,
        "source_url": video.source_url,
        "external_tool_url": video.external_tool_url,
        "entry_id": state.entry_id,
        "kaltura_name": entry.get("name"),
        "duration_seconds": entry.get("duration"),
        "player_url": clean_manifest_url(state.player_url),
        "available_flavors": [
            {
                "id": flavor_id,
                "file_ext": flavor.get("fileExt"),
                "width": flavor.get("width"),
                "height": flavor.get("height"),
                "bitrate": flavor.get("bitrate"),
                "size_bytes": flavor.get("sizeInBytes") or flavor.get("size"),
                "is_original": flavor.get("isOriginal"),
                "is_web": flavor.get("isWeb"),
            }
            for flavor_id, flavor in sorted(state.flavors.items())
        ],
        "caption_assets": [
            {
                "id": caption_id,
                "label": caption.get("label"),
                "language": caption.get("language"),
                "language_code": caption.get("languageCode"),
                "file_ext": caption.get("fileExt"),
                "size_bytes": caption.get("sizeInBytes") or caption.get("size"),
                "accuracy": caption.get("accuracy"),
            }
            for caption_id, caption in sorted(state.captions.items())
        ],
        "warnings": state.warnings,
    }


def save_captions_and_transcript(
    session: requests.Session,
    out_root: pathlib.Path,
    video_name: str,
    state: VideoState,
    overwrite: bool,
) -> dict[str, Any]:
    saved: dict[str, Any] = {"caption_files": [], "transcript_file": None}
    seen_playlists: set[str] = set()
    for index, playlist in enumerate(state.caption_playlists, 1):
        url = playlist.get("url") or ""
        if not url or url in seen_playlists:
            continue
        seen_playlists.add(url)
        body = playlist.get("body") or ""
        if not body:
            response = get_with_retries(session, url, timeout=45)
            if not response.ok:
                state.warnings.append(f"Caption playlist HTTP {response.status_code}")
                continue
            body = response.text
        segment_urls = parse_caption_playlist(url, body)
        chunks: list[str] = []
        for segment_url in segment_urls:
            response = get_with_retries(session, segment_url, timeout=45)
            if response.ok:
                chunks.append(response.text)
            else:
                state.warnings.append(f"Caption segment HTTP {response.status_code}")
        if chunks:
            label = "captions" if index == 1 else f"captions-{index}"
            path = out_root / "Captions" / f"{video_name} - {label}.vtt"
            if overwrite or not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(combine_vtt(chunks), encoding="utf-8")
            saved["caption_files"].append(str(path.relative_to(out_root)))

    text = transcript_text(state.transcript_cues)
    if text:
        path = out_root / "Transcripts" / f"{video_name}.txt"
        if overwrite or not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        saved["transcript_file"] = str(path.relative_to(out_root))
    return saved


async def run_export(args: argparse.Namespace) -> pathlib.Path:
    canvas_domain, course_id = parse_course(args.course_url, args.course_id, args.canvas_domain)
    host = urlparse(canvas_domain).hostname or "umich.instructure.com"
    cookie_jar = load_cookie_jar(args.chrome_profile, host)
    session = make_session(cookie_jar)
    course, videos = collect_video_pages(session, canvas_domain, course_id)
    if args.max_videos:
        videos = videos[: args.max_videos]

    course_name = course.get("name") or f"Course {course_id}"
    stamp = dt.datetime.now().strftime("%Y-%m-%d")
    out_root = pathlib.Path(args.output_dir).expanduser().resolve() / f"{safe_name(course_name, max_len=90)} - MiVideo Export {stamp}"
    if out_root.exists() and args.overwrite:
        shutil.rmtree(out_root)
    out_root.mkdir(parents=True, exist_ok=True)

    manifest_path = out_root / "videos_manifest.json"
    manifest: dict[str, Any] = {
        "course": course_name,
        "course_id": course_id,
        "course_code": course.get("course_code"),
        "term": (course.get("term") or {}).get("name"),
        "source_url": f"{canvas_domain}/courses/{course_id}",
        "exported_at": utc_now(),
        "chrome_profile": args.chrome_profile,
        "quality": args.quality,
        "metadata_only": args.metadata_only,
        "video_count": len(videos),
        "counts": {
            "videos_discovered": len(videos),
            "videos_downloaded": 0,
            "captions_downloaded": 0,
            "transcripts_written": 0,
            "failures": 0,
        },
        "videos": [],
    }
    write_json(manifest_path, manifest)

    print(f"Course: {course_name}")
    print(f"Video pages: {len(videos)}")
    print(f"Output: {out_root}")

    playwright_cookies = cookie_jar_to_playwright(cookie_jar)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not args.headful, executable_path=args.chrome_executable)
        context = await browser.new_context(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
        await context.add_cookies(playwright_cookies)
        try:
            for index, video in enumerate(videos, 1):
                print(f"[{index}/{len(videos)}] {video.title}", flush=True)
                record: dict[str, Any]
                try:
                    state = await inspect_video(context, video, args.wait_after_play_ms)
                    record = record_from_video(video, state)
                    video_name = safe_name(f"{video.module_position:02d} - {video.title}", max_len=120)
                    saved = save_captions_and_transcript(session, out_root, video_name, state, args.overwrite)
                    record.update(saved)
                    manifest["counts"]["captions_downloaded"] += len(saved["caption_files"])
                    if saved["transcript_file"]:
                        manifest["counts"]["transcripts_written"] += 1

                    if args.metadata_only:
                        record["video_file"] = None
                        record["selected_flavor"] = None
                    else:
                        download_url, flavor = choose_download_url(state, args.quality)
                        if not download_url:
                            record["video_file"] = None
                            record["selected_flavor"] = None
                            record.setdefault("warnings", []).append("No official direct download URL captured")
                        else:
                            ext = filename_ext_from_url(download_url, str((flavor or {}).get("fileExt") or "mp4"))
                            path = out_root / "Videos" / f"{video_name} - {state.entry_id or 'entry'}.{ext}"
                            bytes_written = download_stream(session, download_url, path, args.overwrite)
                            record["video_file"] = str(path.relative_to(out_root))
                            record["video_bytes"] = bytes_written
                            record["selected_flavor"] = {
                                "id": (flavor or {}).get("id"),
                                "file_ext": (flavor or {}).get("fileExt") or ext,
                                "width": (flavor or {}).get("width"),
                                "height": (flavor or {}).get("height"),
                                "bitrate": (flavor or {}).get("bitrate"),
                                "size_bytes": (flavor or {}).get("sizeInBytes") or (flavor or {}).get("size"),
                                "is_original": (flavor or {}).get("isOriginal"),
                            }
                            manifest["counts"]["videos_downloaded"] += 1
                    manifest["videos"].append(record)
                except Exception as exc:
                    manifest["counts"]["failures"] += 1
                    manifest["videos"].append(
                        {
                            "title": video.title,
                            "module": video.module,
                            "source_url": video.source_url,
                            "external_tool_url": video.external_tool_url,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
                finally:
                    write_json(manifest_path, manifest)
        finally:
            await context.close()
            await browser.close()

    readme = [
        f"# {course_name} - MiVideo Export",
        "",
        f"- Course ID: {course_id}",
        f"- Source: {canvas_domain}/courses/{course_id}",
        f"- Exported: {utc_now()}",
        f"- Quality: {args.quality}",
        f"- Video pages discovered: {manifest['counts']['videos_discovered']}",
        f"- Videos downloaded: {manifest['counts']['videos_downloaded']}",
        f"- Caption files downloaded: {manifest['counts']['captions_downloaded']}",
        f"- Transcripts written: {manifest['counts']['transcripts_written']}",
        f"- Failures: {manifest['counts']['failures']}",
        "",
        "Signed Kaltura URLs were used during this run but are not stored in the manifest.",
        "HLS stream segments are not archived by this script; it downloads only direct Kaltura download assets and captions exposed by the player session.",
        "",
    ]
    (out_root / "README.md").write_text("\n".join(readme), encoding="utf-8")

    if args.zip:
        zip_path = pathlib.Path(args.zip).expanduser().resolve()
    else:
        zip_path = out_root.with_suffix(".zip")
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(out_root.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(out_root))
    print(f"ZIP: {zip_path}")
    return zip_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--course-url", help="Canvas course URL, e.g. https://umich.instructure.com/courses/834963")
    parser.add_argument("--course-id", help="Canvas course ID. Use with --canvas-domain.")
    parser.add_argument("--canvas-domain", default=DEFAULT_CANVAS_DOMAIN)
    parser.add_argument("--chrome-profile", default=DEFAULT_CHROME_PROFILE, help="Chrome profile directory name")
    parser.add_argument("--chrome-executable", default=DEFAULT_CHROME_EXECUTABLE)
    parser.add_argument("--output-dir", default="mivideo-exports")
    parser.add_argument("--zip", help="ZIP path to write. Defaults beside output folder.")
    parser.add_argument("--quality", default="max-720", choices=["max-720", "best", "smallest", "source"])
    parser.add_argument("--metadata-only", action="store_true", help="Discover videos/captions without downloading video files")
    parser.add_argument("--max-videos", type=int, help="Limit for testing")
    parser.add_argument("--wait-after-play-ms", type=int, default=12000)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--headful", action="store_true", help="Show the browser while exporting")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        asyncio.run(run_export(args))
    except KeyboardInterrupt:
        raise SystemExit(130)


if __name__ == "__main__":
    main()

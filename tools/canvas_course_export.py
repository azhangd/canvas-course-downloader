#!/usr/bin/env python3
import argparse
import csv
import datetime as dt
import json
import pathlib
import re
import shutil
import sys
import time
import zipfile
from urllib.parse import urljoin, urlparse

import browser_cookie3
import requests
from bs4 import BeautifulSoup
from markdownify import markdownify as html_to_markdown


DEFAULT_CANVAS_DOMAIN = "https://umich.instructure.com"
DEFAULT_CHROME_PROFILE = "Profile 2"


def safe_name(value, fallback="untitled", max_len=120):
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


def iso_now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def parse_course(value, course_id, canvas_domain):
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


class CanvasExporter:
    def __init__(self, args):
        self.domain, self.course_id = parse_course(args.course_url, args.course_id, args.canvas_domain)
        self.chrome_profile = args.chrome_profile
        self.output_dir = pathlib.Path(args.output_dir).expanduser().resolve()
        self.zip_path = pathlib.Path(args.zip).expanduser().resolve() if args.zip else None
        self.overwrite = args.overwrite
        host = urlparse(self.domain).hostname or "umich.instructure.com"
        chrome_base = pathlib.Path.home() / "Library/Application Support/Google/Chrome"
        cookie_file = chrome_base / self.chrome_profile / "Cookies"
        if not cookie_file.exists():
            raise FileNotFoundError(f"Chrome cookie DB not found: {cookie_file}")
        self.session = requests.Session()
        self.session.cookies.update(
            browser_cookie3.chrome(cookie_file=str(cookie_file), domain_name=host)
        )
        self.session.headers.update(
            {
                "Accept": "application/json+canvas-string-ids",
                "User-Agent": "Mozilla/5.0 Canvas Course Export Local",
            }
        )
        self.out_root = None
        self.used_paths = set()
        self.file_ids_seen = set()
        self.counts = {
            "modules": 0,
            "module_items": 0,
            "pages": 0,
            "assignments": 0,
            "submissions": 0,
            "announcements": 0,
            "discussions": 0,
            "linked_files": 0,
            "downloaded_files": 0,
            "failed_downloads": 0,
        }
        self.warnings = []
        self.download_failures = []
        self.page_targets = {}
        self.assignment_targets = {}

    def log(self, message):
        print(message, flush=True)

    def api_url(self, path):
        return f"{self.domain}/api/v1/courses/{self.course_id}/{path}"

    def get(self, url, **kwargs):
        for attempt in range(4):
            try:
                response = self.session.get(url, timeout=45, **kwargs)
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < 3:
                        time.sleep(min(2 ** attempt, 8))
                        continue
                return response
            except requests.RequestException:
                if attempt == 3:
                    raise
                time.sleep(min(2 ** attempt, 8))
        raise RuntimeError("unreachable")

    def get_json(self, url, ok=(200,)):
        response = self.get(url)
        if response.status_code not in ok:
            raise requests.HTTPError(f"{response.status_code} for {url}", response=response)
        return response.json()

    def all_pages(self, url):
        items = []
        while url:
            response = self.get(url)
            if not response.ok:
                raise requests.HTTPError(f"{response.status_code} for {url}", response=response)
            data = response.json()
            if isinstance(data, list):
                items.extend(data)
            else:
                items.append(data)
            link = response.headers.get("Link", "")
            match = re.search(r'<([^>]+)>;\s*rel="next"', link)
            url = match.group(1) if match else None
        return items

    def unique_relpath(self, relpath):
        relpath = pathlib.PurePosixPath(str(relpath).replace("\\", "/"))
        parent = str(relpath.parent)
        stem = relpath.stem
        suffix = relpath.suffix
        if parent == ".":
            parent = ""
        candidate = f"{parent + '/' if parent else ''}{stem}{suffix}"
        key = candidate.lower()
        n = 2
        while key in self.used_paths:
            candidate = f"{parent + '/' if parent else ''}{stem} ({n}){suffix}"
            key = candidate.lower()
            n += 1
        self.used_paths.add(key)
        return pathlib.PurePosixPath(candidate)

    def write_text(self, relpath, text):
        relpath = self.unique_relpath(relpath)
        path = self.out_root / pathlib.Path(str(relpath))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return relpath

    def write_bytes(self, relpath, data):
        relpath = self.unique_relpath(relpath)
        path = self.out_root / pathlib.Path(str(relpath))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return relpath

    def clean_html(self, html):
        soup = BeautifulSoup(html or "", "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        for tag in soup.find_all(True):
            for attr in list(tag.attrs):
                attr_lower = attr.lower()
                value = " ".join(tag.get(attr)) if isinstance(tag.get(attr), list) else str(tag.get(attr) or "")
                if attr_lower.startswith("on"):
                    del tag.attrs[attr]
                elif attr_lower in {"href", "src"} and value.strip().lower().startswith("javascript:"):
                    del tag.attrs[attr]
        return str(soup)

    def embedded_links_markdown(self, html):
        soup = BeautifulSoup(html or "", "html.parser")
        links = []
        for tag in soup.find_all(["iframe", "embed", "video", "source"]):
            src = tag.get("src")
            if src:
                links.append(urljoin(self.domain, src))
        if not links:
            return ""
        rows = "\n".join(f"- {link}" for link in dict.fromkeys(links))
        return f"\n\n## Embedded Resources\n\n{rows}\n"

    def doc_markdown(self, title, html_body="", metadata=None, source_url=None):
        metadata = metadata or []
        clean = self.clean_html(html_body)
        md = html_to_markdown(clean, heading_style="ATX").strip()
        parts = [f"# {title}\n"]
        if source_url:
            parts.append(f"Source: {source_url}\n")
        if metadata:
            parts.append("\n".join(f"- {k}: {v}" for k, v in metadata if v not in (None, "")))
            parts.append("")
        if md:
            parts.append(md)
        parts.append(self.embedded_links_markdown(html_body))
        return "\n\n".join(p for p in parts if p is not None).rstrip() + "\n"

    def canvas_file_ids_from_html(self, html):
        if not html:
            return []
        ids = re.findall(r"/files/(\d+)", html)
        return list(dict.fromkeys(ids))

    def download_canvas_file_id(self, file_id, destination_dir="Extracted_Files"):
        file_id = str(file_id)
        if file_id in self.file_ids_seen:
            return None
        self.file_ids_seen.add(file_id)
        response = self.get(f"{self.domain}/api/v1/files/{file_id}")
        if not response.ok:
            self.warnings.append(f"Linked file {file_id}: metadata HTTP {response.status_code}")
            return None
        data = response.json()
        url = data.get("url")
        filename = safe_name(data.get("display_name") or data.get("filename") or f"file_{file_id}")
        if not url:
            self.warnings.append(f"Linked file {file_id}: no download URL")
            return None
        return self.download_url(url, f"{destination_dir}/{filename}", file_id=file_id, expected_size=data.get("size"))

    def download_url(self, url, relpath, file_id=None, expected_size=None):
        try:
            response = self.get(url, stream=True, allow_redirects=True)
            if not response.ok:
                raise requests.HTTPError(f"HTTP {response.status_code}", response=response)
            relpath = self.unique_relpath(relpath)
            path = self.out_root / pathlib.Path(str(relpath))
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("wb") as fh:
                for chunk in response.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        fh.write(chunk)
            self.counts["downloaded_files"] += 1
            if file_id:
                self.file_ids_seen.add(str(file_id))
            return relpath
        except Exception as exc:
            self.counts["failed_downloads"] += 1
            self.download_failures.append({"url": url, "path": relpath, "error": str(exc)})
            return None

    def extract_linked_files(self, html):
        paths = []
        for file_id in self.canvas_file_ids_from_html(html):
            rel = self.download_canvas_file_id(file_id)
            if rel:
                self.counts["linked_files"] += 1
                paths.append(str(rel))
        return paths

    def export_course(self):
        course = self.get_json(f"{self.domain}/api/v1/courses/{self.course_id}?include[]=term&include[]=syllabus_body")
        course_name = course.get("name") or f"Course {self.course_id}"
        stamp = dt.datetime.now().strftime("%Y-%m-%d")
        folder_name = f"{safe_name(course_name, max_len=90)} - Canvas Export {stamp}"
        self.out_root = self.output_dir / folder_name
        if self.out_root.exists():
            if not self.overwrite:
                raise FileExistsError(f"Output folder already exists: {self.out_root} (pass --overwrite to replace it)")
            shutil.rmtree(self.out_root)
        self.out_root.mkdir(parents=True)

        self.log(f"Exporting: {course_name}")
        self.write_text(
            "README.md",
            "\n".join(
                [
                    f"# {course_name}",
                    "",
                    f"- Course ID: {self.course_id}",
                    f"- Course code: {course.get('course_code', '')}",
                    f"- Term: {(course.get('term') or {}).get('name', '')}",
                    f"- Source: {self.domain}/courses/{self.course_id}",
                    f"- Exported: {iso_now()}",
                    "",
                    "This archive was generated locally from your logged-in Chrome Canvas session.",
                    "External LTI/video systems may be represented as links rather than downloaded files.",
                    "",
                ]
            ),
        )

        if course.get("syllabus_body"):
            self.log("Writing syllabus")
            linked = self.extract_linked_files(course.get("syllabus_body"))
            md = self.doc_markdown(
                f"Syllabus - {course_name}",
                course.get("syllabus_body"),
                metadata=[("Linked files downloaded", ", ".join(linked))] if linked else None,
                source_url=f"{self.domain}/courses/{self.course_id}/assignments/syllabus",
            )
            self.write_text("Syllabus.md", md)

        modules = self.all_pages(self.api_url("modules?per_page=100"))
        self.counts["modules"] = len(modules)
        module_lines = [f"# Modules - {course_name}", ""]
        page_slugs = set()
        assignment_ids = set()

        self.log(f"Fetching {len(modules)} modules")
        for module_index, module in enumerate(modules, 1):
            module_name = module.get("name") or f"Module {module_index}"
            module_lines.append(f"## {module_name}")
            module_lines.append("")
            items = self.all_pages(self.api_url(f"modules/{module['id']}/items?per_page=100"))
            self.counts["module_items"] += len(items)
            for item_index, item in enumerate(items, 1):
                item_type = item.get("type") or "Item"
                item_title = item.get("title") or f"{item_type} {item_index}"
                module_lines.append(f"- [{item_type}] {item_title} ({item.get('html_url', '')})")
                if item_type == "Page" and item.get("url"):
                    try:
                        page = self.get_json(item["url"])
                        slug = page.get("url") or item_title
                        if slug in page_slugs:
                            continue
                        page_slugs.add(slug)
                        linked = self.extract_linked_files(page.get("body"))
                        md = self.doc_markdown(
                            page.get("title") or item_title,
                            page.get("body") or "",
                            metadata=[
                                ("Module", module_name),
                                ("Linked files downloaded", ", ".join(linked) if linked else ""),
                            ],
                            source_url=page.get("html_url") or item.get("html_url"),
                        )
                        rel = self.write_text(
                            f"Pages/{module_index:02d} - {safe_name(page.get('title') or item_title)}.md",
                            md,
                        )
                        self.page_targets[slug] = str(rel)
                        self.counts["pages"] += 1
                    except Exception as exc:
                        self.warnings.append(f"Page {item_title}: {exc}")
                elif item_type == "Assignment" and item.get("content_id"):
                    assignment_ids.add(str(item["content_id"]))
                elif item_type == "File" and item.get("url"):
                    try:
                        file_meta = self.get_json(item["url"])
                        file_id = str(file_meta.get("id") or item.get("content_id") or "")
                        if file_id and file_id not in self.file_ids_seen:
                            self.download_url(
                                file_meta.get("url"),
                                f"Module_Files/{safe_name(module_name)}/{safe_name(file_meta.get('display_name') or item_title)}",
                                file_id=file_id,
                                expected_size=file_meta.get("size"),
                            )
                    except Exception as exc:
                        self.warnings.append(f"Module file {item_title}: {exc}")
            module_lines.append("")
        self.write_text("Modules.md", "\n".join(module_lines).rstrip() + "\n")

        self.log("Fetching assignments and grades")
        assignments = []
        try:
            assignments = self.all_pages(
                self.api_url("assignments?per_page=100&include[]=submission&include[]=score_statistics")
            )
        except Exception as exc:
            self.warnings.append(f"Assignments list: {exc}")

        assignment_by_id = {str(a.get("id")): a for a in assignments}
        for assignment_id in sorted(assignment_ids):
            if assignment_id not in assignment_by_id:
                try:
                    assignment_by_id[assignment_id] = self.get_json(
                        self.api_url(f"assignments/{assignment_id}?include[]=submission")
                    )
                except Exception as exc:
                    self.warnings.append(f"Assignment {assignment_id}: {exc}")
        assignments = list(assignment_by_id.values())
        assignments.sort(key=lambda a: (a.get("position") or 9999, a.get("name") or ""))

        if assignments:
            grades_path = self.out_root / "Grades.csv"
            with grades_path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(
                    [
                        "Assignment",
                        "Due Date",
                        "Points Possible",
                        "Score",
                        "Grade",
                        "Low",
                        "Lower Quartile",
                        "Median",
                        "Mean",
                        "Upper Quartile",
                        "High",
                    ]
                )
                for a in assignments:
                    stats = a.get("score_statistics") or {}
                    sub = a.get("submission") or {}
                    writer.writerow(
                        [
                            a.get("name") or "",
                            (a.get("due_at") or "")[:10],
                            a.get("points_possible") or "",
                            sub.get("score") if sub else "",
                            sub.get("grade") if sub else "",
                            stats.get("min", ""),
                            stats.get("lower_quartile", ""),
                            stats.get("median", ""),
                            stats.get("mean", ""),
                            stats.get("upper_quartile", ""),
                            stats.get("max", ""),
                        ]
                    )
            self.used_paths.add("grades.csv")

        for a in assignments:
            assignment_id = str(a.get("id"))
            title = a.get("name") or f"Assignment {assignment_id}"
            linked = self.extract_linked_files(a.get("description"))
            metadata = [
                ("Due", a.get("due_at")),
                ("Points possible", a.get("points_possible")),
                ("Submission types", ", ".join(a.get("submission_types") or [])),
                ("Linked files downloaded", ", ".join(linked) if linked else ""),
            ]
            md = self.doc_markdown(title, a.get("description") or "", metadata=metadata, source_url=a.get("html_url"))
            rel = self.write_text(f"Assignments/{safe_name(title)}.md", md)
            self.assignment_targets[assignment_id] = str(rel)
            self.counts["assignments"] += 1

            try:
                sub = self.get_json(
                    self.api_url(
                        f"assignments/{assignment_id}/submissions/self"
                        "?include[]=user&include[]=submission_comments&include[]=submission_history&include[]=rubric_assessment"
                    )
                )
            except Exception as exc:
                self.warnings.append(f"Submission for {title}: {exc}")
                continue
            history = sub.get("submission_history") or [sub]
            attempts = [h for h in history if h.get("submitted_at") or h.get("body") or h.get("url") or h.get("attachments")]
            if not attempts:
                continue
            sub_lines = [f"# Submission - {title}", "", f"Source: {a.get('html_url') or ''}", ""]
            for h in attempts:
                attempt = h.get("attempt") or "?"
                sub_lines.extend(
                    [
                        f"## Attempt {attempt}",
                        "",
                        f"- Submitted: {h.get('submitted_at') or ''}",
                        f"- Score: {h.get('score') if h.get('score') is not None else ''}",
                        f"- Grade: {h.get('grade') or ''}",
                        f"- Workflow state: {h.get('workflow_state') or ''}",
                        "",
                    ]
                )
                if h.get("body"):
                    self.extract_linked_files(h.get("body"))
                    sub_lines.append(html_to_markdown(self.clean_html(h.get("body")), heading_style="ATX").strip())
                    sub_lines.append("")
                if h.get("url"):
                    sub_lines.append(f"- Submitted URL: {h.get('url')}")
                for att in h.get("attachments") or []:
                    file_id = str(att.get("id") or "")
                    filename = safe_name(att.get("display_name") or att.get("filename") or f"attachment_{file_id}")
                    if att.get("url") and file_id not in self.file_ids_seen:
                        rel_file = self.download_url(
                            att.get("url"),
                            f"Submissions/{safe_name(title)}/{filename}",
                            file_id=file_id,
                            expected_size=att.get("size"),
                        )
                        if rel_file:
                            sub_lines.append(f"- Attachment: {rel_file}")
                sub_lines.append("")
            comments = sub.get("submission_comments") or []
            if comments:
                sub_lines.extend(["## Comments", ""])
                for c in comments:
                    sub_lines.append(f"- {c.get('created_at') or ''} {c.get('author_name') or 'Unknown'}: {c.get('comment') or ''}")
                sub_lines.append("")
            self.write_text(f"Submissions/{safe_name(title)}/submission.md", "\n".join(sub_lines).rstrip() + "\n")
            self.counts["submissions"] += 1

        self.log("Fetching announcements and discussions")
        try:
            announcements = self.all_pages(self.api_url("discussion_topics?only_announcements=true&per_page=100"))
            for ann in announcements:
                linked = self.extract_linked_files(ann.get("message"))
                md = self.doc_markdown(
                    ann.get("title") or "Announcement",
                    ann.get("message") or "",
                    metadata=[
                        ("Posted", ann.get("posted_at")),
                        ("Linked files downloaded", ", ".join(linked) if linked else ""),
                    ],
                    source_url=ann.get("html_url"),
                )
                self.write_text(f"Announcements/{safe_name(ann.get('title') or 'Announcement')}.md", md)
            self.counts["announcements"] = len(announcements)
        except Exception as exc:
            self.warnings.append(f"Announcements: {exc}")

        try:
            topics = self.all_pages(self.api_url("discussion_topics?per_page=100"))
            discussions = [d for d in topics if not d.get("is_announcement")]
            for d in discussions:
                linked = self.extract_linked_files(d.get("message"))
                md = self.doc_markdown(
                    d.get("title") or "Discussion",
                    d.get("message") or "",
                    metadata=[
                        ("Posted", d.get("posted_at")),
                        ("Author", d.get("user_name")),
                        ("Linked files downloaded", ", ".join(linked) if linked else ""),
                    ],
                    source_url=d.get("html_url"),
                )
                self.write_text(f"Discussions/{safe_name(d.get('title') or 'Discussion')}.md", md)
            self.counts["discussions"] = len(discussions)
        except Exception as exc:
            self.warnings.append(f"Discussions: {exc}")

        self.log("Checking quizzes endpoint")
        response = self.get(self.api_url("quizzes?per_page=100"))
        if response.ok:
            quizzes = response.json()
            for q in quizzes:
                md = self.doc_markdown(
                    q.get("title") or "Quiz",
                    q.get("description") or "",
                    metadata=[
                        ("Due", q.get("due_at")),
                        ("Points possible", q.get("points_possible")),
                        ("Question count", q.get("question_count")),
                    ],
                    source_url=q.get("html_url"),
                )
                self.write_text(f"Quizzes/{safe_name(q.get('title') or 'Quiz')}.md", md)
        else:
            self.warnings.append(f"Quizzes endpoint returned HTTP {response.status_code}; likely hidden/unavailable.")

        manifest = {
            "course": course_name,
            "course_id": self.course_id,
            "course_code": course.get("course_code"),
            "term": (course.get("term") or {}).get("name"),
            "source_url": f"{self.domain}/courses/{self.course_id}",
            "exported_at": iso_now(),
            "chrome_profile": self.chrome_profile,
            "counts": self.counts,
            "warnings": self.warnings,
            "download_failures": self.download_failures,
        }
        self.write_text("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")

        zip_name = f"{safe_name(course_name, max_len=80)} - Canvas Export {stamp}.zip"
        zip_path = self.zip_path or (self.output_dir / zip_name)
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        if zip_path.exists():
            if not self.overwrite:
                raise FileExistsError(f"ZIP already exists: {zip_path} (pass --overwrite to replace it)")
            zip_path.unlink()
        self.log(f"Creating ZIP: {zip_path}")
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for path in sorted(self.out_root.rglob("*")):
                if path.is_file():
                    zf.write(path, path.relative_to(self.out_root))
        return zip_path, manifest


def build_parser():
    parser = argparse.ArgumentParser(description="Export Canvas course materials from a logged-in Chrome session.")
    parser.add_argument("--course-url", help="Canvas course URL, e.g. https://umich.instructure.com/courses/844406")
    parser.add_argument("--course-id", help="Canvas course ID. Use with --canvas-domain.")
    parser.add_argument("--canvas-domain", default=DEFAULT_CANVAS_DOMAIN)
    parser.add_argument("--chrome-profile", default=DEFAULT_CHROME_PROFILE, help="Chrome profile directory name")
    parser.add_argument("--output-dir", default="canvas-exports", help="Directory for extracted course export folder")
    parser.add_argument("--zip", help="ZIP path to write. Defaults beside the extracted export folder.")
    parser.add_argument("--overwrite", action="store_true", help="Replace an existing export folder or ZIP")
    return parser


def main():
    args = build_parser().parse_args()
    exporter = CanvasExporter(args)
    zip_path, manifest = exporter.export_course()
    print(
        json.dumps(
            {
                "output_dir": str(exporter.out_root),
                "zip_path": str(zip_path),
                "manifest": manifest,
            },
            indent=2,
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise

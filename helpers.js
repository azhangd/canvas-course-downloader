/**
 * Canvas Course Downloader — Pure Utility Helpers
 *
 * Stateless utility functions used across the extension.
 * These have no DOM or Chrome API dependencies (except getCanvasBrandColor/darkenColor
 * which read computed styles).
 */

// ---------------------------------------------------------------------------
// Canvas Theme Detection
// ---------------------------------------------------------------------------

const FALLBACK_COLOR = "#e82429";

/** Reads the institution's Canvas brand color from CSS custom properties. */
function getCanvasBrandColor() {
  const root = document.documentElement;
  const style = getComputedStyle(root);
  return (
    style.getPropertyValue("--ic-brand-primary").trim() ||
    style.getPropertyValue("--ic-brand-button--primary-bgd").trim() ||
    style.getPropertyValue("--ic-brand-global-nav-bgd").trim() ||
    FALLBACK_COLOR
  );
}

/** Returns a darker shade of a hex color for hover states. */
function darkenColor(hex, amount = 0.15) {
  const num = parseInt(hex.replace("#", ""), 16);
  const r = Math.max(0, Math.min(255, ((num >> 16) & 0xFF) - Math.round(255 * amount)));
  const g = Math.max(0, Math.min(255, ((num >> 8) & 0xFF) - Math.round(255 * amount)));
  const b = Math.max(0, Math.min(255, (num & 0xFF) - Math.round(255 * amount)));
  return `#${((1 << 24) + (r << 16) + (g << 8) + b).toString(16).slice(1)}`;
}

// ---------------------------------------------------------------------------
// String & Path Sanitization
// ---------------------------------------------------------------------------

/** Replaces characters that are invalid or problematic in file paths. */
function sanitizeFilename(name) {
  if (!name) return "untitled";
  const cleaned = name
    .replace(/[\u0000-\u001F\u007F]/g, "")                          // control chars
    .replace(/[\u200B-\u200D\uFEFF]/g, "")                          // zero-width chars
    .replace(/\u00A0/g, " ")                                          // non-breaking space
    .replace(/[/\\?%*:|"<>]/g, "-")                                   // OS-reserved chars
    .replace(/^\.+/, "")                                              // leading dots
    .replace(/[. ]+$/, "")                                            // trailing dots/spaces
    .replace(/^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\.|$)/i, "_$1$2") // Windows reserved names
    .trim();
  return cleaned || "untitled";
}

/** Strips script tags from HTML to prevent XSS when opening exported files. */
function sanitizeHtml(html) {
  return html.replace(/<script\b[^<]*(?:(?!<\/script>)<[^<]*)*<\/script>/gi, "");
}

/**
 * Removes the kind of empty-tag litter that Canvas's WYSIWYG editor leaves
 * behind. Conservative — only strips tags whose contents are whitespace-only
 * (or a single &nbsp;), and leaves anything with real content untouched.
 */
function cleanCanvasHtml(html) {
  if (!html) return "";
  return html
    .replace(/<p[^>]*>\s*(?:&nbsp;| )?\s*<\/p>/gi, "")
    .replace(/<span[^>]*>\s*(?:&nbsp;| )?\s*<\/span>/gi, "");
}

/**
 * Formats an ISO date string as `YYYY-MM-DD HH:MM` in the user's local timezone.
 * Returns "" for falsy or invalid input.
 */
function formatDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/**
 * Minimal fallback stylesheet used when Canvas's own CSS can't be fetched.
 * Kept readable rather than Canvas-branded.
 */
const FALLBACK_EXPORT_CSS = `
body { font: 15px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, system-ui, sans-serif; max-width: 820px; margin: 32px auto; padding: 0 24px; color: #2d3b45; background: #fff; }
h1, h2, h3, h4 { line-height: 1.3; }
h1 { font-size: 1.6em; border-bottom: 1px solid #e8eaec; padding-bottom: 8px; margin-bottom: 16px; }
a { color: #0374b5; }
img { max-width: 100%; height: auto; }
table { border-collapse: collapse; width: 100%; margin: 1em 0; }
th, td { border: 1px solid #d7dade; padding: 8px 12px; text-align: left; vertical-align: top; }
th { background: #f5f5f5; }
blockquote { border-left: 4px solid #d7dade; padding: 4px 16px; color: #556; margin: 1em 0; }
code { background: #f5f5f5; padding: 2px 6px; border-radius: 4px; font-size: 0.92em; }
pre { background: #f5f5f5; padding: 12px; border-radius: 6px; overflow-x: auto; }
pre code { background: transparent; padding: 0; }
ul, ol { padding-left: 1.5em; }
hr { border: none; border-top: 1px solid #e8eaec; margin: 2em 0; }
`;

/**
 * Computes the relative path from a file at `filePath` to a sibling at the course root.
 * Examples: "" → ".", "Pages/" → "..", "deep/nested/" → "../..".
 */
function relativePathToCourseRoot(filePath) {
  const depth = filePath.split("/").filter(Boolean).length;
  return depth === 0 ? "." : Array(depth).fill("..").join("/");
}

/** Wraps content in an HTML page that links to `styles.css` at the course root. */
function toHtmlDataUri(title, body, filePath = "") {
  const safeBody = sanitizeHtml(body);
  const cssHref = `${relativePathToCourseRoot(filePath)}/styles.css`;
  const html = `<!doctype html><html lang="en"><head><meta charset="utf-8"><title>${title}</title><link rel="stylesheet" href="${cssHref}"></head><body><h1>${title}</h1>${safeBody}</body></html>`;
  return `data:text/html;charset=utf-8,${encodeURIComponent(html)}`;
}

/** Wraps a Markdown body in a `# Title` heading and returns a data-URI. */
function toMarkdownDataUri(title, mdBody) {
  const md = `# ${title}\n\n${mdBody}`;
  return `data:text/markdown;charset=utf-8,${encodeURIComponent(md)}`;
}

/**
 * Computes the relative URL needed to navigate from a file at `fromPath`
 * (a "Subfolder/" or "" string identifying the *folder* the file lives in)
 * to a target file whose full path-from-course-root is `toPath`.
 *
 * Examples:
 *   relativeUrlFromTo("Pages/", "Assignments/bar.html") → "../Assignments/bar.html"
 *   relativeUrlFromTo("Pages/", "Pages/foo.html") → "../Pages/foo.html"
 *   relativeUrlFromTo("", "Pages/foo.html") → "Pages/foo.html"
 */
function relativeUrlFromTo(fromPath, toPath) {
  const depth = fromPath.split("/").filter(Boolean).length;
  const upDirs = depth === 0 ? "" : Array(depth).fill("..").join("/") + "/";
  return upDirs + toPath;
}

/**
 * Scans `href`/`src` attributes in `html` and rewrites any that match a
 * Canvas URL in `urlMap` to a relative local path. Anything not in the map
 * is left alone (could be an external link or a resource we didn't export).
 *
 * `urlMap` maps full Canvas URLs to their target path-from-course-root
 * (e.g. "Pages/foo.html"). `fromPath` is the folder of the file currently
 * being rewritten.
 */
function rewriteCanvasLinks(html, urlMap, fromPath) {
  if (!html || !urlMap || urlMap.size === 0) return html;
  return html.replace(/(href|src)="([^"]+)"/gi, (match, attr, url) => {
    let target = urlMap.get(url);
    if (!target) {
      const normalized = url.split("?")[0].split("#")[0].replace(/\/$/, "");
      target = urlMap.get(normalized);
    }
    if (!target) return match;
    return `${attr}="${relativeUrlFromTo(fromPath, target)}"`;
  });
}

let _turndownService = null;

/**
 * Lazily-built Turndown converter with the GFM plugin for tables/strikethrough.
 * If Turndown isn't loaded (e.g. in the test page), returns the input untouched
 * so callers don't need to special-case.
 */
function htmlToMarkdown(html) {
  if (typeof TurndownService === "undefined") return html;
  if (!_turndownService) {
    _turndownService = new TurndownService({
      headingStyle: "atx",
      codeBlockStyle: "fenced",
      bulletListMarker: "-",
    });
    if (typeof turndownPluginGfm !== "undefined") {
      _turndownService.use(turndownPluginGfm.gfm);
    }
  }
  return _turndownService.turndown(sanitizeHtml(html));
}

// ---------------------------------------------------------------------------
// Pagination Parsing
// ---------------------------------------------------------------------------

/**
 * Parses a Link header and returns the URL for the "next" page, or null.
 * Canvas API uses RFC 5988 Link headers for pagination.
 *
 * @param {string|null} linkHeader - The raw Link header value
 * @returns {string|null} The next page URL, or null if there is none
 */
function parsePaginationLink(linkHeader) {
  if (!linkHeader) return null;
  const nextLink = linkHeader.split(",").find((s) => s.includes('rel="next"'));
  return nextLink ? nextLink.match(/<([^>]+)>/)?.[1] ?? null : null;
}

// ---------------------------------------------------------------------------
// Path Length Safety
// ---------------------------------------------------------------------------

/**
 * Truncates a filename to fit within maxPath characters when combined with
 * the course name and file path. Preserves the file extension.
 *
 * @param {string} filename - The original filename
 * @param {string} courseName - Sanitized course name
 * @param {string} filePath - The file's subdirectory path
 * @param {number} maxPath - Maximum total path length (default 250)
 * @returns {string} The possibly-truncated filename
 */
function truncateFilename(filename, courseName, filePath, maxPath = 250) {
  const fullLen = courseName.length + 1 + filePath.length + filename.length;
  if (fullLen <= maxPath) return filename;

  const ext = filename.includes(".") ? filename.slice(filename.lastIndexOf(".")) : "";
  const maxName = maxPath - courseName.length - 1 - filePath.length - ext.length;
  if (maxName > 10) {
    return filename.slice(0, maxName) + ext;
  }
  return filename;
}

/**
 * Canvas Course Downloader — Download Orchestration
 *
 * Settings management, ZIP bundling, course content fetching,
 * and download handoff to the background service worker.
 */

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

const SETTING_DEFAULTS = {
  contentTypes: {
    files: true, pages: true, assignments: true, discussions: true,
    announcements: true, modules: true, syllabus: true, grades: true,
    linkedFiles: true,
  },
  conflictAction: "uniquify",
  throttleMs: 250,
  folderPrefix: "",
  zipMode: false,
  incrementalMode: false,
  excludeVideos: false,
  maxFileSizeMB: 0,
  preset: "full-archive",
  exportFormat: "html",
};

/** Loads user settings from chrome.storage.sync, falling back to defaults. */
function loadSettings() {
  return new Promise((resolve) => {
    chrome.storage.sync.get(SETTING_DEFAULTS, (s) => resolve(s));
  });
}

// ---------------------------------------------------------------------------
// ZIP Download Helper
// ---------------------------------------------------------------------------

// Above this estimated total size we skip ZIP bundling and fall through to
// per-file streaming downloads. ZIP output is buffered into a single Blob
// before chrome.downloads ingests it; tab processes get unstable above ~2 GB,
// so the threshold is conservative.
const ZIP_MAX_TOTAL_BYTES = 1.5 * 1024 * 1024 * 1024;

async function downloadAsZip(files, courseName, settings, log) {
  const safeName = sanitizeFilename(courseName);
  const totalFiles = files.length;
  let completed = 0;
  let failed = 0;
  const failedFiles = [];

  createDownloadPanel();

  // Bridge the existing cancel flag (set synchronously by the panel's Cancel
  // button) to a standard AbortController, so in-flight fetches actually stop
  // instead of running to completion and being discarded.
  const abortController = new AbortController();
  const cancelWatch = setInterval(() => {
    if (downloadCancelled && !abortController.signal.aborted) abortController.abort();
  }, 100);

  // client-zip pulls from this generator lazily: each yield happens after the
  // previous file's body has been fully streamed into the archive. We use the
  // yield boundary to update the panel, so progress reflects real archive
  // state, not just enqueue order.
  async function* fileSource() {
    const now = new Date();
    for (const file of files) {
      if (abortController.signal.aborted) return;

      const fullPath = `${file.path}${file.filename}`;
      updateDownloadPanel({
        total: totalFiles, completed, failed, queued: totalFiles - completed - failed,
        downloading: 1, currentFile: file.filename, failedFiles, done: false, cancelled: false,
      });

      try {
        let input;
        if (file.url.startsWith("data:")) {
          const res = await fetch(file.url);
          input = new Uint8Array(await res.arrayBuffer());
        } else {
          const res = await fetch(file.url, { signal: abortController.signal });
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          input = res.body;
        }
        completed++;
        yield {
          name: fullPath,
          input,
          lastModified: now,
          ...(file.size ? { size: file.size } : {}),
        };
      } catch (err) {
        if (err && err.name === "AbortError") return;
        console.warn(`[Canvas Downloader] ZIP: failed to fetch ${file.filename}:`, err);
        failed++;
        failedFiles.push(file.filename);
      }
    }
  }

  log("Generating ZIP file (streaming)...");

  let blob;
  try {
    // client-zip returns a Response whose body streams ZIP bytes as the
    // generator is consumed. Calling .blob() lets the browser materialize the
    // archive from the stream — peak memory is bounded by the network chunk
    // size per file plus the final Blob, vs JSZip which buffered every input
    // file plus the entire output before emitting.
    const response = downloadZip(fileSource(), { buffersAreUTF8: true });
    blob = await response.blob();
  } catch (err) {
    clearInterval(cancelWatch);
    if (abortController.signal.aborted || downloadCancelled) {
      log("Cancelled during ZIP generation.");
      updateDownloadPanel({
        total: totalFiles, completed, failed, queued: 0, downloading: 0,
        currentFile: null, failedFiles, done: true, cancelled: true,
      });
      return;
    }
    console.error("[Canvas Downloader] ZIP generation failed:", err);
    log(`ZIP generation failed: ${err && err.message ? err.message : err}.`);
    updateDownloadPanel({
      total: totalFiles, completed, failed, queued: 0, downloading: 0,
      currentFile: "ZIP generation failed — see console", failedFiles, done: true, cancelled: false,
    });
    throw err;
  }

  clearInterval(cancelWatch);

  if (downloadCancelled) {
    log("Cancelled after ZIP was generated — not saving.");
    updateDownloadPanel({
      total: totalFiles, completed, failed, queued: 0, downloading: 0,
      currentFile: null, failedFiles, done: true, cancelled: true,
    });
    return;
  }

  const url = URL.createObjectURL(blob);
  const prefix = settings.folderPrefix ? `${sanitizeFilename(settings.folderPrefix)}/` : "";
  const filename = `${prefix}${safeName}.zip`;

  await new Promise((resolve, reject) => {
    chrome.runtime.sendMessage(
      { type: "START_DOWNLOAD", payload: { files: [{ url, filename, path: "" }], courseName: "", conflictAction: settings.conflictAction, throttleMs: 0, folderPrefix: "" } },
      (response) => {
        if (chrome.runtime.lastError) return reject(chrome.runtime.lastError);
        resolve(response);
      }
    );
  });

  setTimeout(() => URL.revokeObjectURL(url), 60000);

  updateDownloadPanel({
    total: totalFiles, completed, failed, queued: 0, downloading: 0,
    currentFile: null, failedFiles, done: true, cancelled: false,
  });

  log(`ZIP created: ${safeName}.zip (${completed} files, ${failed} failed)`);
}

// ---------------------------------------------------------------------------
// Course Content Fetcher
// ---------------------------------------------------------------------------

/**
 * Downloads every available resource from a single Canvas course.
 *
 * @param {string} courseId   - The numeric Canvas course ID
 * @param {string} courseName - Human-readable course name (used for folder paths)
 * @param {string} domain     - Origin URL of the Canvas instance
 * @param {function} onProgress - Optional callback for UI status updates
 */
async function downloadCourse(courseId, courseName, domain, onProgress) {
  const settings = await loadSettings();
  const types = settings.contentTypes;
  const isMarkdown = settings.exportFormat === "markdown";
  const docExt = isMarkdown ? "md" : "html";
  const log = (msg) => {
    console.log(`[Canvas Downloader] [${courseName}] ${msg}`);
    if (onProgress) onProgress(msg);
  };

  const api = (path) => `${domain}/api/v1/courses/${courseId}/${path}`;
  const filesToDownload = [];
  const seenFileIds = new Set();
  // Maps Canvas module-item ids to the underlying { type, key } so cross-reference
  // rewriting can dereference /modules/items/<id> URLs to the actual resource.
  const moduleItemIdToResource = new Map();

  // Teacher/TA/designer sessions can reach endpoints students can't (all
  // submissions, full discussion threads, every student's grades). Detect the
  // role once and let each content block opt into the richer teacher fetches.
  // Students (or any failure) fall back to the exact behavior as before.
  const isTeacher = await fetchCourseRole(domain, courseId);
  log(isTeacher ? "Teacher role detected — archiving student data" : "Student role — archiving own view");

  // Counts for teacher-only data, surfaced in the export manifest.
  let discussionReplyCount = 0;
  let studentSubmissionCount = 0;
  let gradebookStudentCount = 0;

  /**
   * Builds a *raw* document entry (no data-URI encoding yet). Bodies stay as
   * HTML strings on the entry so the cross-reference rewrite pass can mutate
   * hrefs before final encoding. `resourceType` + `resourceId` let the URL
   * map identify this entry as the target of Canvas links pointing at it.
   */
  const buildDocEntry = (title, htmlBody, filenameStem, path, resourceType, resourceId = null) => ({
    rawBody: htmlBody,
    title,
    resourceType,
    resourceId,
    filename: `${filenameStem}.${docExt}`,
    path,
  });

  /** True if this entry is a generated document (raw body) OR a synthesized data-URI (CSV, manifest). */
  const isSynthetic = (f) => f.rawBody !== undefined || (f.url && f.url.startsWith("data:"));

  // --- Stylesheet for exported HTML ----------------------------------------
  if (!isMarkdown) {
    filesToDownload.push({
      url: `data:text/css;charset=utf-8,${encodeURIComponent(FALLBACK_EXPORT_CSS)}`,
      filename: "styles.css",
      path: "",
    });
  }

  // --- Files & Folders -------------------------------------------------------
  let files = [];
  if (types.files) {
    log("Fetching files...");
    const folders = await fetchAllPages(api("folders?per_page=100"));
    const folderPathById = {};
    folders.forEach((f) => (folderPathById[f.id] = f.full_name || f.name));

    files = await fetchAllPages(api("files?per_page=100"));

    files.forEach((file) => {
      let folder = folderPathById[file.folder_id] || "";
      if (folder.startsWith("course files")) folder = folder.slice("course files".length);
      if (folder && !folder.endsWith("/")) folder += "/";
      if (folder.startsWith("/")) folder = folder.slice(1);

      seenFileIds.add(String(file.id));
      filesToDownload.push({ url: file.url, filename: file.display_name, path: `Files/${folder}`, size: file.size || 0, contentType: file["content-type"] || "", canvasId: String(file.id) });
    });
  }

  // --- Hidden file extraction ------------------------------------------------
  async function extractLinkedFiles(html, source) {
    const doc = new DOMParser().parseFromString(html, "text/html");
    const links = doc.querySelectorAll('a[href*="/files/"]');

    for (const link of links) {
      const id = link.getAttribute("href")?.match(/\/files\/(\d+)/)?.[1];
      if (!id || seenFileIds.has(id)) continue;

      try {
        const res = await fetchWithRetry(`${domain}/api/v1/files/${id}`);
        if (!res.ok) continue;
        const data = await res.json();

        const fileId = String(data.id || id);
        if (!seenFileIds.has(fileId)) {
          seenFileIds.add(fileId);
          filesToDownload.push({
            url: data.url,
            filename: data.display_name || link.textContent.trim() || `file_${id}`,
            path: "Extracted_Files/",
            size: data.size || 0,
            contentType: data["content-type"] || "",
            canvasId: fileId,
          });
        }
      } catch (err) {
        console.error(`[Canvas Downloader] Error fetching linked file ${id} from ${source}:`, err);
      }
    }
  }

  // --- Pages: collect slugs from the Pages API list -------------------------
  // Bodies are fetched later, AFTER Modules, so we can also pull in pages that
  // only appear as module items (Canvas's Pages list omits those for students
  // when the page isn't in the main pages navigation).
  let pages = [];
  const pageSlugsToFetch = new Set();
  if (types.pages) {
    log("Fetching pages list...");
    pages = await fetchAllPages(api("pages?per_page=100"));
    for (const p of pages) {
      if (p.url) pageSlugsToFetch.add(p.url);
    }
  }

  // --- Assignments -----------------------------------------------------------
  // The assignment list is also needed to build the teacher gradebook columns,
  // so fetch it whenever assignments OR (teacher + grades) are requested, but
  // only emit the per-assignment documents when assignments itself is on.
  let assignments = [];
  const needAssignmentList = types.assignments || (isTeacher && types.grades);
  if (needAssignmentList) {
    log("Fetching assignments...");
    assignments = await fetchAllPages(api("assignments?per_page=100"));
  }
  if (types.assignments) {
    for (const a of assignments) {
      let body = "";
      if (a.due_at) body += `<p><strong>Due:</strong> ${formatDate(a.due_at)}</p>`;
      if (a.description) {
        body += `<div>${cleanCanvasHtml(a.description)}</div>`;
        if (types.linkedFiles) await extractLinkedFiles(a.description, `Assignment: ${a.name}`);
      }
      const safeName = sanitizeFilename(a.name).substring(0, 100);
      filesToDownload.push(buildDocEntry(a.name, body, safeName, "Assignments/", "assignment", String(a.id)));
    }
  }

  // --- Student submissions (teacher only) ------------------------------------
  // For each assignment, pull every student's submission: attached files, the
  // typed/online body, grade and instructor comments — organized as
  // Submissions/<Assignment>/<Student>/. Skipped entirely for students (whose
  // session would only return their own submission anyway).
  if (isTeacher && types.assignments && assignments.length > 0) {
    log("Fetching student submissions...");
    for (const a of assignments) {
      const safeAssignment = sanitizeFilename(a.name).substring(0, 80);
      const subs = await fetchAllPages(
        api(`assignments/${a.id}/submissions?per_page=100&include[]=user&include[]=submission_comments`)
      );

      for (const s of subs) {
        // Skip slots that were never submitted and never graded.
        if (s.workflow_state === "unsubmitted" && s.score == null && !s.submission_comments?.length) continue;

        const studentName = s.user?.sortable_name || s.user?.name || `user_${s.user_id}`;
        const safeStudent = sanitizeFilename(studentName).substring(0, 80);
        const studentPath = `Submissions/${safeAssignment}/${safeStudent}/`;

        // Submitted file attachments.
        for (const att of s.attachments || []) {
          const fileId = String(att.id || "");
          if (att.url && fileId && !seenFileIds.has(fileId)) {
            seenFileIds.add(fileId);
            filesToDownload.push({
              url: att.url,
              filename: att.display_name || att.filename || `attachment_${fileId}`,
              path: studentPath,
              size: att.size || 0,
              contentType: att["content-type"] || "",
              canvasId: fileId,
            });
          }
        }

        // Per-student summary doc: grade, state, text/URL entry, comments.
        let body = `<p><strong>Student:</strong> ${studentName}</p>`;
        body += `<p><strong>Status:</strong> ${s.workflow_state || "—"}</p>`;
        if (s.submitted_at) body += `<p><strong>Submitted:</strong> ${formatDate(s.submitted_at)}</p>`;
        if (s.score != null || s.grade != null) {
          body += `<p><strong>Grade:</strong> ${s.grade ?? ""} (${s.score ?? ""} / ${a.points_possible ?? "—"})${s.late ? " · <em>late</em>" : ""}</p>`;
        }
        if (s.body) {
          body += `<h3>Submission text</h3><div>${cleanCanvasHtml(s.body)}</div>`;
          if (types.linkedFiles) await extractLinkedFiles(s.body, `Submission: ${a.name} — ${studentName}`);
        }
        if (s.url) body += `<p><strong>Submitted URL:</strong> <a href="${s.url}">${s.url}</a></p>`;
        const comments = s.submission_comments || [];
        if (comments.length) {
          body += "<h3>Comments</h3><ul>";
          for (const c of comments) {
            body += `<li><strong>${c.author_name || "Unknown"}</strong>${c.created_at ? ` · ${formatDate(c.created_at)}` : ""}: ${cleanCanvasHtml(c.comment || "")}</li>`;
          }
          body += "</ul>";
        }

        filesToDownload.push(buildDocEntry(`${a.name} — ${studentName}`, body, "submission", studentPath, "submission", null));
        studentSubmissionCount++;
      }
    }
  }

  // --- Announcements ---------------------------------------------------------
  let announcements = [];
  if (types.announcements) {
    log("Fetching announcements...");
    announcements = await fetchAllPages(api("discussion_topics?only_announcements=true&per_page=100"));

    for (const a of announcements) {
      let body = "";
      if (a.posted_at) body += `<p><strong>Posted:</strong> ${formatDate(a.posted_at)}</p>`;
      if (a.message) {
        body += `<div>${cleanCanvasHtml(a.message)}</div>`;
        if (types.linkedFiles) await extractLinkedFiles(a.message, `Announcement: ${a.title}`);
      }
      const safeName = sanitizeFilename(a.title).substring(0, 100);
      filesToDownload.push(buildDocEntry(a.title, body, safeName, "Announcements/", "announcement", String(a.id)));
    }
  }

  // --- Discussions -----------------------------------------------------------
  let discussions = [];
  if (types.discussions) {
    log("Fetching discussions...");
    const allTopics = await fetchAllPages(api("discussion_topics?per_page=100"));
    discussions = allTopics.filter((d) => !d.is_announcement);

    // Recursively renders the threaded reply tree from a topic's /view payload.
    // Side effects: extracts linked files and pushes reply attachments as files.
    const renderEntries = async (entries, participantName, topicTitle, attachmentPath, depth = 0) => {
      if (!Array.isArray(entries) || entries.length === 0) return "";
      let html = `<ul class="discussion-replies" style="list-style:none;margin:0;padding-left:${depth ? 24 : 0}px;border-left:${depth ? "2px solid #ddd" : "none"}">`;
      for (const e of entries) {
        const author = participantName.get(String(e.user_id)) || "Unknown user";
        const when = e.created_at ? formatDate(e.created_at) : "";
        const message = e.deleted ? "<em>[deleted]</em>" : cleanCanvasHtml(e.message || "");
        html += `<li style="margin:12px 0"><p style="margin:0 0 4px"><strong>${author}</strong>${when ? ` · <span style="color:#666">${when}</span>` : ""}</p><div>${message}</div>`;

        if (types.linkedFiles && e.message) await extractLinkedFiles(e.message, `Discussion reply: ${topicTitle}`);

        const atts = e.attachments || (e.attachment ? [e.attachment] : []);
        for (const att of atts) {
          const fileId = String(att.id || "");
          if (att.url && fileId && !seenFileIds.has(fileId)) {
            seenFileIds.add(fileId);
            filesToDownload.push({
              url: att.url,
              filename: att.display_name || att.filename || `attachment_${fileId}`,
              path: attachmentPath,
              size: att.size || 0,
              contentType: att["content-type"] || "",
              canvasId: fileId,
            });
          }
        }

        discussionReplyCount++;
        html += await renderEntries(e.replies, participantName, topicTitle, attachmentPath, depth + 1);
        html += "</li>";
      }
      html += "</ul>";
      return html;
    };

    for (const d of discussions) {
      let body = "";
      if (d.user_name) body += `<p><strong>Author:</strong> ${d.user_name}</p>`;
      if (d.posted_at) body += `<p><strong>Posted:</strong> ${formatDate(d.posted_at)}</p>`;
      if (d.message) {
        body += `<div>${cleanCanvasHtml(d.message)}</div>`;
        if (types.linkedFiles) await extractLinkedFiles(d.message, `Discussion: ${d.title}`);
      }

      // Teachers get the full threaded view (all student replies); students only
      // ever see the opening message, so the /view fetch is gated on the role.
      if (isTeacher) {
        try {
          const res = await fetchWithRetry(api(`discussion_topics/${d.id}/view`), {
            headers: { Accept: "application/json+canvas-string-ids" },
          });
          if (res.ok) {
            const view = await res.json();
            const participantName = new Map((view.participants || []).map((p) => [String(p.id), p.display_name || p.name]));
            const safeTopic = sanitizeFilename(d.title).substring(0, 80);
            const repliesHtml = await renderEntries(view.view, participantName, d.title, `Discussions/${safeTopic}/`);
            if (repliesHtml) body += `<hr><h3>Replies</h3>${repliesHtml}`;
          }
        } catch (err) {
          console.error(`[Canvas Downloader] Discussion thread error (${d.title}):`, err);
        }
      }

      const safeName = sanitizeFilename(d.title).substring(0, 100);
      filesToDownload.push(buildDocEntry(d.title, body, safeName, "Discussions/", "discussion", String(d.id)));
    }
  }

  // --- Modules ---------------------------------------------------------------
  let modules = [];
  if (types.modules) {
    log("Fetching modules...");
    modules = await fetchAllPages(api("modules?per_page=100"));

    let modulesBody = "";
    for (const mod of modules) {
      modulesBody += `<h2>${mod.name}</h2><ul>`;
      const items = await fetchAllPages(api(`modules/${mod.id}/items?per_page=100`));

      for (const item of items) {
        const label = item.html_url ? `<a href="${item.html_url}">${item.title}</a>` : item.title;
        modulesBody += `<li>${label} (${item.type})</li>`;

        // Track module-item → underlying-resource linkage for cross-ref rewriting.
        if (item.id) {
          if (item.type === "Page" && item.page_url) {
            moduleItemIdToResource.set(String(item.id), { type: "page", key: item.page_url });
          } else if (item.content_id) {
            const typeKey = { Assignment: "assignment", Discussion: "discussion", File: "file" }[item.type];
            if (typeKey) moduleItemIdToResource.set(String(item.id), { type: typeKey, key: String(item.content_id) });
          }
        }

        // Surface module-only Pages so their bodies (and embedded files) get
        // exported. We don't gate on `types.pages` because the embedded files
        // (PDF slides etc.) are what makes the export usable — the page HTML
        // itself is only pushed downstream when `types.pages` is on.
        if (item.type === "Page") {
          let slug = item.page_url;
          // Some Canvas instances don't return `page_url` on module items; fall
          // back to parsing the slug out of the per-item API URL or html URL.
          if (!slug && item.url) {
            const m = item.url.match(/\/pages\/([^/?#]+)/);
            if (m) slug = decodeURIComponent(m[1]);
          }
          if (!slug && item.html_url) {
            const m = item.html_url.match(/\/pages\/([^/?#]+)/);
            if (m) slug = decodeURIComponent(m[1]);
          }
          if (slug) pageSlugsToFetch.add(slug);
        }

        if (item.type === "File" && item.url) {
          try {
            const res = await fetchWithRetry(item.url);
            if (!res.ok) continue;
            const data = await res.json();

            const fileId = String(data.id || "");
            if (fileId && !seenFileIds.has(fileId)) {
              seenFileIds.add(fileId);
              const safeModName = sanitizeFilename(mod.name);
              filesToDownload.push({
                url: data.url,
                filename: data.display_name || item.title,
                path: `Modules/${safeModName}/`,
                size: data.size || 0,
                contentType: data["content-type"] || "",
                canvasId: fileId,
              });
            }
          } catch (err) {
            console.error(`[Canvas Downloader] Module file error (${item.title}):`, err);
          }
        }
      }
      modulesBody += "</ul>";
    }

    filesToDownload.push(buildDocEntry("Modules", modulesBody, "Modules", "", "module-index"));
  }

  // --- Pages: fetch bodies for every slug (from Pages list + Modules) -------
  // We deduplicate via the Set, so a page appearing both in the Pages list
  // and as a module item is exported once. The fetch runs whenever we have
  // any slugs at all — even if the user unchecked Pages — so that embedded
  // files (PDF slides etc.) inside module-only pages still get extracted.
  let exportedPagesCount = 0;
  if (pageSlugsToFetch.size > 0) {
    log(`Fetching ${pageSlugsToFetch.size} page${pageSlugsToFetch.size === 1 ? "" : "s"}...`);
    console.log("[Canvas Downloader] Page slugs to fetch:", [...pageSlugsToFetch]);
    for (const slug of pageSlugsToFetch) {
      try {
        const res = await fetchWithRetry(`${domain}/api/v1/courses/${courseId}/pages/${slug}`);
        if (!res.ok) {
          console.warn(`[Canvas Downloader] Page fetch failed for "${slug}" — HTTP ${res.status}`);
          continue;
        }
        const page = await res.json();

        if (types.pages) {
          filesToDownload.push(
            buildDocEntry(
              page.title,
              cleanCanvasHtml(page.body || ""),
              sanitizeFilename(page.url).substring(0, 100),
              "Pages/",
              "page",
              page.url
            )
          );
          exportedPagesCount++;
        }

        if (types.linkedFiles && page.body) await extractLinkedFiles(page.body, `Page: ${page.title}`);
      } catch (err) {
        console.warn(`[Canvas Downloader] Could not fetch page ${slug}:`, err);
      }
    }
  } else if (types.pages) {
    console.log("[Canvas Downloader] No pages found via Pages API list or Modules — nothing to fetch.");
  }

  // --- Syllabus --------------------------------------------------------------
  if (types.syllabus) {
    log("Fetching syllabus...");
    try {
      const res = await fetchWithRetry(`${domain}/api/v1/courses/${courseId}?include[]=syllabus_body`);
      if (res.ok) {
        const data = await res.json();
        if (data.syllabus_body) {
          if (types.linkedFiles) await extractLinkedFiles(data.syllabus_body, "Syllabus");
          filesToDownload.push(
            buildDocEntry(
              `Syllabus — ${courseName}`,
              cleanCanvasHtml(data.syllabus_body),
              "Syllabus",
              "",
              "syllabus"
            )
          );
        }
      }
    } catch (err) {
      console.error("[Canvas Downloader] Syllabus error:", err);
    }
  }

  // --- Gradebook (teacher only) ----------------------------------------------
  // A teacher has no own submission, so the personal Grades.csv below would be
  // empty. Instead emit a full Gradebook.csv: one row per student, one column
  // per assignment, built from the bulk submissions endpoint.
  if (isTeacher && types.grades && assignments.length > 0) {
    log("Fetching gradebook...");
    try {
      const students = await fetchAllPages(api("users?enrollment_type[]=student&per_page=100"));
      const allSubs = await fetchAllPages(api("students/submissions?student_ids[]=all&per_page=100"));

      // Index scores by "<userId>:<assignmentId>".
      const scoreByKey = new Map();
      for (const s of allSubs) scoreByKey.set(`${s.user_id}:${s.assignment_id}`, s.score ?? s.grade ?? "");

      const q = (v) => `"${String(v).replace(/"/g, '""')}"`;
      const header = ["Student", "Login/SIS ID", ...assignments.map((a) => a.name)].map(q).join(",");
      const pointsRow = [q("Points Possible"), q(""), ...assignments.map((a) => a.points_possible ?? "")].join(",");
      const rows = [header, pointsRow];

      for (const stu of students) {
        const cells = [q(stu.sortable_name || stu.name || `user_${stu.id}`), q(stu.login_id || stu.sis_user_id || "")];
        for (const a of assignments) cells.push(scoreByKey.get(`${stu.id}:${a.id}`) ?? "");
        rows.push(cells.join(","));
        gradebookStudentCount++;
      }

      filesToDownload.push({
        url: `data:text/csv;charset=utf-8,${encodeURIComponent(rows.join("\n"))}`,
        filename: "Gradebook.csv",
        path: "",
      });
    } catch (err) {
      console.error("[Canvas Downloader] Gradebook error:", err);
    }
  }

  // --- Grades (student: own scores + class statistics) -----------------------
  if (types.grades && !isTeacher) {
    log("Fetching grades...");
    try {
      const gradeAssignments = await fetchAllPages(
        api("assignments?per_page=100&include[]=submission&include[]=score_statistics")
      );
      if (gradeAssignments.length > 0) {
        const csvRows = [
          "Assignment,Due Date,Points Possible,Score,Grade,Low,Lower Quartile,Median,Mean,Upper Quartile,High",
        ];
        for (const a of gradeAssignments) {
          const name = (a.name || "").replace(/"/g, '""');
          const due = formatDate(a.due_at).slice(0, 10);
          const possible = a.points_possible ?? "";
          const score = a.submission?.score ?? "";
          const grade = a.submission?.grade ?? "";
          const stats = a.score_statistics || {};
          const low = stats.min ?? "";
          const lowerQuartile = stats.lower_quartile ?? "";
          const median = stats.median ?? "";
          const mean = stats.mean ?? "";
          const upperQuartile = stats.upper_quartile ?? "";
          const high = stats.max ?? "";
          csvRows.push(
            `"${name}","${due}",${possible},${score},"${grade}",${low},${lowerQuartile},${median},${mean},${upperQuartile},${high}`
          );
        }
        filesToDownload.push({
          url: `data:text/csv;charset=utf-8,${encodeURIComponent(csvRows.join("\n"))}`,
          filename: "Grades.csv",
          path: "",
        });
      }
    } catch (err) {
      console.error("[Canvas Downloader] Grades error:", err);
    }
  }

  // --- Incremental mode: filter out unchanged files --------------------------
  let skippedCount = 0;
  if (settings.incrementalMode) {
    const storageKey = `incremental_${courseId}`;
    const stored = await new Promise((r) => chrome.storage.local.get(storageKey, (d) => r(d[storageKey] || {})));

    const filtered = [];
    for (const file of filesToDownload) {
      if (!isSynthetic(file)) {
        const fileKey = file.path + file.filename;
        if (stored[fileKey]) {
          skippedCount++;
          continue;
        }
      }
      filtered.push(file);
    }

    const newRecord = {};
    for (const file of filesToDownload) {
      if (!isSynthetic(file)) {
        newRecord[file.path + file.filename] = Date.now();
      }
    }
    chrome.storage.local.set({ [storageKey]: newRecord });

    if (skippedCount > 0) {
      log(`Incremental mode: skipping ${skippedCount} previously downloaded files.`);
    }
    filesToDownload.length = 0;
    filesToDownload.push(...filtered);
  }

  // --- File filters: exclude videos and large files --------------------------
  const VIDEO_EXTENSIONS = /\.(mp4|mov|avi|mkv|webm|wmv|flv|m4v)$/i;
  let filteredOutCount = 0;

  if (settings.excludeVideos || settings.maxFileSizeMB > 0) {
    const maxBytes = settings.maxFileSizeMB > 0 ? settings.maxFileSizeMB * 1024 * 1024 : Infinity;
    const before = filesToDownload.length;

    const kept = [];
    for (const file of filesToDownload) {
      // Always keep generated docs / synthesized data URIs (CSV, manifest, styles).
      if (isSynthetic(file)) { kept.push(file); continue; }

      if (settings.excludeVideos) {
        if (VIDEO_EXTENSIONS.test(file.filename) || (file.contentType && file.contentType.startsWith("video/"))) {
          continue;
        }
      }
      if (settings.maxFileSizeMB > 0 && file.size > maxBytes) {
        continue;
      }
      kept.push(file);
    }

    filteredOutCount = before - kept.length;
    filesToDownload.length = 0;
    filesToDownload.push(...kept);

    if (filteredOutCount > 0) {
      log(`File filters: excluded ${filteredOutCount} file(s).`);
    }
  }

  // --- Export manifest -------------------------------------------------------
  const manifest = {
    course: courseName,
    courseId,
    sourceUrl: `${domain}/courses/${courseId}`,
    exportDate: new Date().toISOString(),
    extensionVersion: chrome.runtime.getManifest().version,
    counts: {
      files: files.length,
      pages: exportedPagesCount,
      assignments: assignments.length,
      announcements: announcements.length,
      discussions: discussions.length,
      discussionReplies: discussionReplyCount,
      studentSubmissions: studentSubmissionCount,
      gradebookStudents: gradebookStudentCount,
      modules: modules.length,
      extractedFiles: filesToDownload.filter((f) => f.path === "Extracted_Files/").length,
      skippedIncremental: skippedCount,
      skippedFilters: filteredOutCount,
      total: filesToDownload.length,
    },
  };

  filesToDownload.push({
    url: `data:application/json;charset=utf-8,${encodeURIComponent(JSON.stringify(manifest, null, 2))}`,
    filename: "manifest.json",
    path: "",
  });

  // --- Path length safety (Windows 260-char limit) -------------------------
  const safeCourse = sanitizeFilename(courseName);
  for (const file of filesToDownload) {
    file.filename = truncateFilename(file.filename, safeCourse, file.path);
  }

  // --- Cross-reference URL map -------------------------------------------
  // Walk filesToDownload (after truncation, so paths are final) and map every
  // exported resource's Canvas URL to its local "<path><filename>" target.
  const urlMap = new Map();
  for (const f of filesToDownload) {
    const target = `${f.path}${f.filename}`;
    if (f.resourceType === "page" && f.resourceId) {
      urlMap.set(`${domain}/courses/${courseId}/pages/${f.resourceId}`, target);
    } else if (f.resourceType === "assignment" && f.resourceId) {
      urlMap.set(`${domain}/courses/${courseId}/assignments/${f.resourceId}`, target);
    } else if (f.resourceType === "announcement" && f.resourceId) {
      urlMap.set(`${domain}/courses/${courseId}/discussion_topics/${f.resourceId}`, target);
      urlMap.set(`${domain}/courses/${courseId}/announcements/${f.resourceId}`, target);
    } else if (f.resourceType === "discussion" && f.resourceId) {
      urlMap.set(`${domain}/courses/${courseId}/discussion_topics/${f.resourceId}`, target);
      urlMap.set(`${domain}/courses/${courseId}/discussions/${f.resourceId}`, target);
    } else if (f.canvasId) {
      urlMap.set(`${domain}/courses/${courseId}/files/${f.canvasId}`, target);
      urlMap.set(`${domain}/files/${f.canvasId}`, target);
    }
  }
  // Dereference /modules/items/<id> URLs through the items→resource map.
  for (const [itemId, info] of moduleItemIdToResource) {
    let sourceUrl;
    if (info.type === "page") sourceUrl = `${domain}/courses/${courseId}/pages/${info.key}`;
    else if (info.type === "assignment") sourceUrl = `${domain}/courses/${courseId}/assignments/${info.key}`;
    else if (info.type === "discussion") sourceUrl = `${domain}/courses/${courseId}/discussion_topics/${info.key}`;
    else if (info.type === "file") sourceUrl = `${domain}/courses/${courseId}/files/${info.key}`;
    const resolved = sourceUrl && urlMap.get(sourceUrl);
    if (resolved) urlMap.set(`${domain}/courses/${courseId}/modules/items/${itemId}`, resolved);
  }

  // --- Rewrite + encode pass on generated docs ---------------------------
  for (const f of filesToDownload) {
    if (f.rawBody === undefined) continue;
    const rewritten = rewriteCanvasLinks(f.rawBody, urlMap, f.path);
    f.url = isMarkdown
      ? toMarkdownDataUri(f.title, htmlToMarkdown(rewritten))
      : toHtmlDataUri(f.title, rewritten, f.path);
    delete f.rawBody;
    delete f.title;
    delete f.resourceType;
    delete f.resourceId;
  }

  // --- ZIP mode or individual download handoff --------------------------------
  log(`${filesToDownload.length} files ready.`);

  if (settings.zipMode && typeof downloadZip !== "undefined") {
    const estimatedBytes = filesToDownload.reduce((sum, f) => sum + (f.size || 0), 0);
    if (estimatedBytes > ZIP_MAX_TOTAL_BYTES) {
      const gb = (estimatedBytes / (1024 * 1024 * 1024)).toFixed(1);
      log(`Estimated archive size ~${gb} GB exceeds the bundling ceiling — falling back to individual file downloads.`);
      showToast(`Course is too large for ZIP bundling (~${gb} GB). Files will download individually.`, "info");
    } else {
      return await downloadAsZip(filesToDownload, courseName, settings, log);
    }
  }

  return new Promise((resolve, reject) => {
    chrome.runtime.sendMessage(
      {
        type: "START_DOWNLOAD",
        payload: {
          files: filesToDownload,
          courseName,
          conflictAction: settings.conflictAction,
          throttleMs: settings.throttleMs,
          folderPrefix: settings.folderPrefix,
        },
      },
      (response) => {
        if (chrome.runtime.lastError) {
          console.error("[Canvas Downloader] Background error:", chrome.runtime.lastError);
          return reject(chrome.runtime.lastError);
        }
        resolve(response);
      }
    );
  });
}

// ---------------------------------------------------------------------------
// Single-Course Download (triggered from a course page)
// ---------------------------------------------------------------------------

async function downloadCurrentCourse() {
  const courseId = getCourseId();
  if (!courseId) {
    showToast("Could not determine course ID. Navigate to a Canvas course page.", "error");
    return;
  }

  downloadCancelled = false;

  const courseName = getCourseName();
  const domain = window.location.origin;

  const btn = document.getElementById("canvas-downloader-btn");
  const originalText = btn?.textContent ?? "";
  if (btn) {
    btn.textContent = "Fetching data...";
    btn.disabled = true;
  }

  try {
    await downloadCourse(courseId, courseName, domain, (msg) => {
      if (btn) btn.textContent = msg;
    });
    if (btn) {
      btn.textContent = "Downloads Queued!";
      setTimeout(() => { btn.textContent = originalText; btn.disabled = false; }, 3000);
    }
  } catch (err) {
    console.error("[Canvas Downloader] Error:", err);
    showToast("An error occurred. Check the developer console for details.", "error");
    if (btn) { btn.textContent = originalText; btn.disabled = false; }
  }
}

# Privacy Policy — Canvas Course Downloader

**Last updated:** March 14, 2026

## Overview

Canvas Course Downloader is a free, open-source browser extension. This policy explains what data the extension accesses and how it's handled.

## Data Collection

**Canvas Course Downloader does not collect, store, transmit, or share any user data.** The extension does not use analytics, tracking, cookies, or any form of telemetry.

## How the Extension Works

- The extension runs entirely in your browser.
- When you choose to download course content, it communicates **only** with the Canvas LMS server you are currently logged into, using your existing browser session.
- Downloaded files are saved directly to your computer via your browser's built-in download manager.
- No data is ever sent to the extension developer, any third-party server, or any external service.

## Permissions

The extension requests the following browser permissions:

| Permission | Why it's needed |
|---|---|
| `activeTab` | To communicate with the content script on the current tab |
| `downloads` | To save course files to your computer |
| `notifications` | To notify you when a download batch finishes |
| `storage` | To save your settings and track which files have been downloaded in incremental mode |
| Content script (`https://*/*`) | Canvas LMS can be hosted on any domain, so the content script loads on any HTTPS page to detect Canvas. It exits immediately on non-Canvas pages |
| Host permissions (`*://*.instructure.com/*`) | Elevated access for Instructure-hosted Canvas instances. On self-hosted instances the extension works through same-origin requests from the page you're already on |
| `declarativeNetRequest` | Allows the extension to declare rules that modify network responses. Used solely to add a CORS header to responses from Canvas's file CDN (`canvas-user-content.com`), which is required for the extension to read file bytes when building ZIP archives |
| Host permissions (`*://*.canvas-user-content.com/*`) | Canvas stores uploaded files on this CDN domain. This permission is required for the CORS rule above to be permitted to apply to responses from that domain |

## Third-Party Services

The extension does not integrate with or send data to any third-party services.

## Open Source

The full source code is publicly available at [github.com/jasp-nerd/canvas-course-downloader](https://github.com/jasp-nerd/canvas-course-downloader). You can audit exactly what the extension does.

## Contact

If you have questions about this privacy policy, please open an issue on [GitHub](https://github.com/jasp-nerd/canvas-course-downloader/issues).

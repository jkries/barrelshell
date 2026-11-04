# Web and PDF reading — upgrade and testing

## Upgrade an existing Barrel

1. Stop the running bot and back up your installation.
2. Update the platform files, including **the entire new `barrel_web/` directory**,
   `barrel_v1.py`, `bundled/fetch.py`, `bundled/file.py`, `bundled/system.py`,
   `.gitignore`, and the requirements files. Do not overwrite your `config.json`,
   `identity.md`, `history.md`, `pulse.md`, workspace, or personal skills.
3. Activate the same virtual environment/interpreter your bot service uses:

   ```bash
   python -m pip install -r requirements.txt
   ```

4. For optional JavaScript rendering:

   ```bash
   python -m pip install -r requirements-browser.txt
   python -m playwright install chromium
   ```

   On Linux, Chromium may need OS libraries. As appropriate for your machine,
   use `python -m playwright install --with-deps chromium` (this can install
   system packages and require elevated privileges). Install browser binaries
   **as the OS user that runs the bot**, not just your interactive account.

5. Merge the desired settings into your existing `config.json`:

   ```json
   {
     "fetch_browser_enabled": true,
     "fetch_timeout_seconds": 30,
     "fetch_browser_timeout_seconds": 25,
     "fetch_cache_ttl_seconds": 86400,
     "fetch_cache_max_documents": 64,
     "fetch_max_chars": 4000,
     "download_max_bytes": 20000000
   }
   ```

   All new settings have defaults. Browser rendering defaults to **false**;
   you do not need to add any settings for HTML/text-PDF support. Keep JSON
   booleans unquoted. `fetch_max_chars` must be 1,000–32,000; the byte limit
   must be 1,024–50,000,000. Invalid retrieval settings produce an explicit error.
   For multi-step research you may also want `"max_tool_rounds": 6` instead of 3.
6. Restart the bot after updating code/dependencies. Later config-only changes
   can use `/reload`.

If a custom `skills/fetch.py` overrides the bundled fetch tool, update or remove
that override; otherwise the new implementation will not be used.

## Test without Ollama

Run from the repository root using the bot's Python environment:

```bash
python -m unittest discover -s tests -v
python -m barrel_web "https://example.com" --refresh
python -m barrel_web "https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf" --refresh
python -m barrel_web "https://YOUR-JAVASCRIPT-PAGE" --render --refresh
```

The CLI's `--render` is an explicit one-command opt-in, even if browser rendering
is disabled in config. It still needs Playwright and Chromium installed. Its
cache partition is `cli`, separate from bot chats. Output includes a document ID:

```bash
python -m barrel_web "read DOCUMENT_ID | 5-8"
python -m barrel_web "search DOCUMENT_ID | annual revenue"
python -m barrel_web "pages DOCUMENT_ID | 2-4"
python -m barrel_web --file "path/to/report.pdf"
```

`pages` is for PDF page numbers, while `read` uses chunk numbers. A request for
many chunks/pages returns only complete chunks that fit the response budget;
check **Chunks returned** and continue from the next chunk. Chunks are at most
800 characters; `fetch_max_chars` budgets the returned content, with metadata
added separately. A single PDF page can span many chunks.

Optional **real Chromium integration tests** use local/mocked fixture responses,
including a JavaScript app that loads a script and JSON, then tries private
network access, POST and WebSocket traffic. They do not depend on public sites:

```bash
# Linux/macOS
BARREL_BROWSER_TESTS=1 python -m unittest discover -s tests -v
```

```powershell
# PowerShell
$env:BARREL_BROWSER_TESTS = "1"
python -m unittest discover -s tests -v
```

The GitHub Actions workflow also runs the unit suite on Windows/Linux and
Python 3.10/3.14, plus browser integration tests on Linux. CI results must be
checked on your fork; local verification does not establish those platforms.

## Test through your bot

Try these requests using documents/sites you actually need:

- “Read this PDF and summarize it: URL. Cite the page numbers.”
- “Read page 5 of that PDF.”
- “Search the document for cancellation fees and read the relevant chunks.”
- “Read this page using JavaScript rendering: URL.”
- “Fetch the page again; do not use the cached copy.”
- “Read report.pdf from the workspace.”

The existing `<fetch>URL</fetch>` syntax still works. New tool forms:

```text
<fetch>https://example.org/article | render</fetch>
<fetch>https://example.org/article | refresh</fetch>
<fetch>read DOCUMENT_ID | 5-8</fetch>
<fetch>pages DOCUMENT_ID | 2-4</fetch>
<fetch>search DOCUMENT_ID | cancellation fees</fetch>
```

`render` and `refresh` can be combined. The bot is told that partial content is
not a complete document read. Model compliance still depends on your model;
the direct CLI distinguishes retrieval failures from model/tool-use mistakes.

## Behavior and limits

- PDFs are recognized by content type or `%PDF-` signature, even without a
  `.pdf` URL suffix. Text extraction retains page numbers. Workspace PDF reads
  use the same extraction/cache path rather than decoding binary data as UTF-8.
- HTML uses Trafilatura main-content extraction, with a structural fallback.
  Headings, paragraphs, links and tables are retained where practical.
- Static retrieval runs first. With browser support enabled, HTML with under
  250 extracted characters triggers fallback. Rich navigation can fool this
  heuristic; explicitly request `render` when necessary.
- Browser settling is bounded (up to three seconds after DOM readiness), not an
  indefinite wait for every network request. Some slow sites need more advanced
  site-specific handling; rendering is not guaranteed.
- PDF extraction is capped at 300 pages and 500,000 characters; other extracted
  text is capped at 500,000 characters. Limit warnings identify incomplete
  extraction. Later pages beyond those limits are not available in the cache.
- Parsers run in a separate killable process with a 20-second extraction limit.
  Unix has a best-effort 768 MiB address-space limit; Windows needs OS/container
  controls for a hard memory limit. Run untrusted-content processing with an
  appropriately restricted OS account.
- Network reads have redirect, byte, decompression, inactivity and total
  deadline limits. Browser resources share a byte budget and 60-request cap.
  HTML download, extraction and optional rendering have separate stage budgets;
  the full fetch can therefore take longer than `fetch_timeout_seconds`.
- Scanned/image-only PDFs report that OCR is needed. OCR is not included.
  Password-protected/malformed PDFs produce explicit errors. Reading order in
  multi-column PDFs and complex tables remains best-effort.
- Browser mode does not use personal profiles, saved cookies, credentials,
  login sessions, paywall bypasses or CAPTCHA solvers. Cookies and mutating
  HTTP methods are deliberately unsupported; some legitimate sites will fail.

## Cache and privacy

Extracted text and metadata are saved under `.barrel_cache/`, outside the
workspace and partitioned by conversation ID. Document IDs from one chat cannot
be read in another. IDs expire after 24 hours by default; the cache retains at
most 64 documents globally. Expiration/eviction occurs on access or new fetches,
not on a background timer. Stop the bot and delete `.barrel_cache/` to clear it
immediately. Raw PDFs, scripts and browser cookies are not cached.

The cache contains potentially sensitive document text. `.gitignore` excludes
it; preserve appropriate permissions and backups. Normal tool tracing can show
content previews, and user/model messages continue to use the existing agent
log. Remote websites see your fetch/browser requests; no third-party extraction
service is used. This transport intentionally ignores environment HTTP proxies.
Corporate TLS roots can be supplied through the usual `SSL_CERT_FILE` mechanism;
Mozilla certificate roots are also loaded. TLS verification stays enabled.

## Security changes and compatibility notes

- Fetch and workspace download share a DNS-pinned HTTP transport. It rejects
  non-public addresses, mixed public/private DNS results, credentials in URLs,
  common IPv6 transition-address bypasses, and unsafe redirect destinations.
  HTTPS keeps hostname verification and SNI for the original hostname.
- Browser HTTP requests are fulfilled by that transport. A dead proxy prevents
  unintercepted browser traffic from connecting directly. Service workers,
  dedicated/shared workers, WebRTC, WebTransport, real WebSockets, subframe
  navigation, images/media/fonts and POST-like methods are blocked. Use OS/container network isolation as additional defense in depth.
- Workspace files are now **downloads**, including HTML, SVG, images and PDFs;
  they no longer execute or preview inline on the dashboard origin. This is an
  intentional compatibility change to avoid same-origin script execution.
- Dashboard requests validate Host/Origin, chat POSTs require JSON and bounded
  bodies, token cookies are HttpOnly, and responses disable MIME sniffing and
  caching. Remote dashboards should still use TLS/reverse proxy or an SSH tunnel.
- External tool results are labelled as untrusted and JSON-escaped. The legacy
  XML protocol/user-message representation is retained for local-model
  compatibility; this is not a complete prompt-injection defense.
- `<remember>` writes are blocked unless the current human text/caption matches
  the explicit remember/save-request grammar. Scheduled tasks and image
  descriptions cannot authorize writes; forgetting requires a human
  forget/remove/delete/erase request. Facts are stored on one line with limits.
  This changes unsolicited automatic memory behavior: explicitly ask the bot
  to remember something. An authorized request can still be misinterpreted by a
  model; approvals for every proposed fact would be a stronger future policy.
- Script startup now aliases the running core as `barrel_v1`, so bundled skills
  share the live config/state instead of importing a duplicate runtime. Parser
  workers use a separate interpreter/module rather than re-executing the bot
  entrypoint or personal skill initializers.
- The previously misnamed `gitignore` is now `.gitignore`, and tracked Python
  bytecode has been removed. Ignore rules do not remove secrets already tracked
  in your own fork; audit and untrack those separately.

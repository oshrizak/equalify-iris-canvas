/*
 * Equalify Iris for Canvas — Panorama-style Canvas overlay.
 *
 * Surfaces:
 *   - Files index rows (data-testid="table-row")
 *   - Module items wrapping a File
 *   - Any anchor referencing /files/<id> (Pages, Discussions, Modules, etc.)
 *
 * Each PDF gets a circular score dial. Clicking the dial opens the
 * Alternative Formats Menu modal. Instructors also get an "Edit
 * Accessible HTML" button that opens an in-modal editor with live
 * preview; saved edits become the canonical source for every other
 * format (ePub, braille, the reader, audio, translations).
 */
(function () {
  "use strict";

  var SCRIPT = document.currentScript || (function () {
    var all = document.querySelectorAll('script[src*="/lti/panorama.js"]');
    return all[all.length - 1] || null;
  })();
  var ORIGIN = (function () { if (!SCRIPT) return ""; try { return new URL(SCRIPT.src).origin; } catch (_) { return ""; } })();
  var INST = (function () { if (!SCRIPT) return ""; try { return new URL(SCRIPT.src).searchParams.get("inst") || ""; } catch (_) { return ""; } })();
  if (!ORIGIN || !INST) { console.warn("[iris] panorama.js loaded without origin/inst; aborting"); return; }
  // One overlay per page, however many loaders include this file (a theme
  // can carry an old loader and a new one at once while it moves from one
  // to the other): every copy would put its own dial on every link.
  if (window.__irisPanoramaLoaded) return;
  window.__irisPanoramaLoaded = true;

  var STATE = { courseId: null, userRole: null, scoresByFilename: null, csrfToken: null, oauth: null };
  // File extensions Reflow can process. Mirrors the watcher's _CONVERTIBLE_EXTS
  // on the backend so the dial appears on the same set of files. Add new
  // formats here when the backend learns to handle them.
  var CONVERTIBLE_EXT_RE = /\.(pdf|docx|doc|pptx|ppt|html|htm|epub)$/i;
  var FILE_HREF_RE = /\/files\/(\d+)(?:[\/?#"']|$)/;
  // The accessible-version link inside a published Canvas Page points at the
  // tool endpoint ``/canvas/panorama/alt/<job_id>/<fmt>``. We decorate it with
  // the OUTPUT (WCAG) score — the "after" — distinct from the source-PDF dial.
  var ACCESSIBLE_HREF_RE = /\/canvas\/panorama\/alt\/([^\/?#]+)\//;
  // Sent on every backend fetch. The ngrok-skip header bypasses ngrok's
  // free-tier browser interstitial — without it, ngrok returns an HTML warning
  // page instead of our JSON/HTML and the overlay's fetches fail to parse
  // (dials disappear). Harmless on Cloudflare/other hosts (ignored), so it's
  // safe to always send.
  var BACKEND_HEADERS = { "ngrok-skip-browser-warning": "1" };
  // Colours tuned to meet WCAG 2.2 AA non-text contrast (3:1 vs row
   // background) and text contrast (4.5:1 vs white) for the percentage
   // label rendered inside the dial. ``amber`` darkened from #cc7a00
   // (~4.36:1, fails AA) to #8a5500 (~6.2:1).
  var COLORS = { "red": "#b00020", "amber": "#8a5500", "green": "#2e7d32",
                 "dark-green": "#155724", "unscanned": "#6b7280" };

  // Catalogue of formats the modal shows. live=true means the backend
  // is wired; the rest are visible-but-disabled placeholders.
  // ``download: true`` means the format card saves a file to the user's
  // downloads folder without opening a new tab (see _wireDownload). That's
  // the right UX for binary / file artifacts (PDF, EPUB, MP3, BRF, plain
  // text, markdown). Formats that render as a webpage in the browser
  // (HTML preview, HTML-with-math, Translate output, Immersive Reader)
  // stay as ``target="_blank"`` so faculty actually see the rendered
  // page in a new tab.
  // Groups organise the alt-formats into mental buckets so the grid
  // is scannable instead of a wall of squares. ``order`` matters
  // because the rendered sections follow this list top-to-bottom.
  // The original source PDF lives in Canvas Files where faculty
  // uploaded it — surfacing it again in this menu was confusing
  // ("why does the accessible-formats menu offer me my original
  // PDF?") so it's removed. HTML with math, Plain text and Markdown
  // left the menu too: Accessible HTML shows math itself, plain text
  // throws away the headings, lists and tables the other formats keep,
  // and Markdown served no student. Their URLs still answer, for links
  // people saved.
  var FORMAT_GROUPS = [
    { id: "read",      label: "Read online" },
    { id: "document",  label: "Download" },
    { id: "listen",    label: "Listen and translate" }
  ];
  var FORMATS = [
    { id: "html",      group: "read",     label: "Accessible HTML",    icon: "🌐", color: "#0a5fb5", live: true },
    { id: "immersive", group: "read",     label: "Immersive Reader",   icon: "👁", color: "#5b3da6", live: true },
    { id: "audio",     group: "listen",   label: "Audio (MP3)",        icon: "🎧", color: "#2e7d32", live: true, download: true },
    { id: "translate", group: "listen",   label: "Translate…",         icon: "🌐", color: "#2e7d32", live: true, picker: "language" },
    { id: "ocr",       group: "document", label: "Searchable PDF",     icon: "🔎", color: "#cc7a00", live: true, download: true },
    { id: "epub",      group: "document", label: "ePub",               icon: "📚", color: "#0e7a8a", live: true, download: true },
    { id: "braille",   group: "document", label: "Braille (BRF)",      icon: "⠿", color: "#5b3da6", live: true, download: true }
  ];

  // Language options for the Translate dialog. Covers CSUEB's largest
  // non-English-speaking communities plus globally common languages.
  var TRANSLATE_LANGUAGES = [
    { code: "es", label: "Spanish (Español)" },
    { code: "zh", label: "Chinese (中文)" },
    { code: "vi", label: "Vietnamese (Tiếng Việt)" },
    { code: "tl", label: "Tagalog / Filipino" },
    { code: "ko", label: "Korean (한국어)" },
    { code: "ja", label: "Japanese (日本語)" },
    { code: "ar", label: "Arabic (العربية)" },
    { code: "hi", label: "Hindi (हिन्दी)" },
    { code: "fr", label: "French (Français)" },
    { code: "de", label: "German (Deutsch)" },
    { code: "pt", label: "Portuguese (Português)" },
    { code: "it", label: "Italian (Italiano)" },
    { code: "ru", label: "Russian (Русский)" },
    { code: "pl", label: "Polish (Polski)" },
    { code: "tr", label: "Turkish (Türkçe)" },
    { code: "fa", label: "Persian / Farsi (فارسی)" },
    { code: "ur", label: "Urdu (اردو)" },
    { code: "bn", label: "Bengali (বাংলা)" },
    { code: "id", label: "Indonesian (Bahasa Indonesia)" },
    { code: "th", label: "Thai (ไทย)" }
  ];

  function getEnv() {
    var env = window.ENV || {};
    var cid = (env.COURSE && env.COURSE.id) || env.COURSE_ID || env.course_id;
    if (!cid && typeof env.context_asset_string === "string" && env.context_asset_string.indexOf("course_") === 0) {
      cid = env.context_asset_string.slice("course_".length);
    }
    if (!cid) { var m = location.pathname.match(/\/courses\/(\d+)/); if (m) cid = m[1]; }
    STATE.courseId = cid ? String(cid) : null;
    var roles = env.current_user_roles || [];
    STATE.userRole = (roles.indexOf("teacher") >= 0 || roles.indexOf("ta") >= 0 || roles.indexOf("admin") >= 0)
      ? "Instructor" : "Student";
  }

  async function loadScoresByFilename() {
    if (!STATE.courseId) return {};
    try {
      var r = await fetch(
        ORIGIN + "/canvas/panorama/scored_files?course_id=" + encodeURIComponent(STATE.courseId),
        { credentials: "omit", headers: BACKEND_HEADERS }
      );
      if (!r.ok) return {};
      var data = await r.json();
      STATE.scoresByFilename = data.by_filename || {};
      return STATE.scoresByFilename;
    } catch (e) { console.warn("[iris] scored_files failed", e); return {}; }
  }

  // Fetch the per-session CSRF token bound to the LTI session cookie.
  // Required header on every state-changing request (approve / reject /
  // edit / pii-decision). 401 means no session cookie -- fall back to
  // anonymous behavior; state-changing actions will simply fail.
  async function loadCsrfToken() {
    try {
      var r = await fetch(
        ORIGIN + "/canvas/panorama/csrf",
        { credentials: "include", headers: BACKEND_HEADERS }
      );
      if (!r.ok) { STATE.csrfToken = null; return null; }
      var data = await r.json();
      STATE.csrfToken = data.csrf_token || null;
      return STATE.csrfToken;
    } catch (e) {
      console.warn("[iris] csrf token fetch failed", e);
      STATE.csrfToken = null;
      return null;
    }
  }

  // Build the standard set of headers for any state-changing request.
  // ``Content-Type`` is included because every endpoint accepts JSON.
  function csrfHeaders() {
    var h = Object.assign({ "Content-Type": "application/json" }, BACKEND_HEADERS);
    if (STATE.csrfToken) h["X-CSRF-Token"] = STATE.csrfToken;
    return h;
  }

  // Consent / authorization status for the current LTI session.
  // The actual gate is the LTI launch flow — this is just so the modal
  // footer can show a "View authorization terms" reminder and surface a
  // "please authorize" prompt to instructors who never launched the tool.
  async function loadConsentStatus() {
    try {
      var r = await fetch(
        ORIGIN + "/canvas/consent/status",
        { credentials: "include", headers: BACKEND_HEADERS }
      );
      if (!r.ok) { STATE.consent = { agreed: false, reason: "fetch_failed" }; return STATE.consent; }
      STATE.consent = await r.json();
      return STATE.consent;
    } catch (e) {
      console.warn("[iris] consent status fetch failed", e);
      STATE.consent = { agreed: false, reason: "network_error" };
      return STATE.consent;
    }
  }

  // Canvas-OAuth authorization status for the current LTI session. This is
  // distinct from consent (terms agreement): it's whether the instructor has
  // granted Reflow permission to read their files and publish Pages in their
  // courses. Without it, the watcher/bridge can't act for this instructor,
  // so we surface an "Authorize Reflow" prompt.
  async function loadOAuthStatus() {
    try {
      var r = await fetch(
        ORIGIN + "/canvas/panorama/oauth_status",
        { credentials: "include", headers: BACKEND_HEADERS }
      );
      if (!r.ok) { STATE.oauth = { authorized: false, is_instructor: false, has_session: false }; return STATE.oauth; }
      STATE.oauth = await r.json();
      return STATE.oauth;
    } catch (e) {
      console.warn("[iris] oauth status fetch failed", e);
      STATE.oauth = { authorized: false, is_instructor: false, has_session: false };
      return STATE.oauth;
    }
  }

  // Open the Canvas OAuth consent flow in a popup window (instead of a
  // full-window navigation that yanks the instructor out of their page).
  // The callback returns a self-closing page that postMessages us back; we
  // listen for it, refresh status, and re-decorate so the prompt disappears.
  function openAuthPopup() {
    var url = ORIGIN + "/canvas/oauth/authorize?popup=1&return_url=" +
              encodeURIComponent(ORIGIN + "/canvas/oauth/authorized");
    var w = 600, h = 720;
    var y = window.top.outerHeight ? Math.max(0, (window.top.outerHeight - h) / 2) : 80;
    var x = window.top.outerWidth ? Math.max(0, (window.top.outerWidth - w) / 2) : 80;
    var popup = window.open(
      url, "reflow_authorize",
      "width=" + w + ",height=" + h + ",left=" + x + ",top=" + y + ",menubar=no,toolbar=no"
    );
    if (!popup) {
      // Popup blocked — fall back to a new tab so the user isn't stuck.
      window.open(url, "_blank", "noopener");
    }
    return popup;
  }

  // One-time listener: when the popup reports success, re-check status and
  // re-scan so badges/prompts update without a page reload.
  function _installOAuthMessageListener() {
    if (_installOAuthMessageListener._done) return;
    _installOAuthMessageListener._done = true;
    window.addEventListener("message", function (ev) {
      var d = ev && ev.data;
      if (!d || d.type !== "reflow-oauth" || !d.ok) return;
      loadOAuthStatus().then(function () {
        // Refresh any open modal banner and re-decorate the page.
        var banner = document.getElementById("reflow-pn-auth-banner");
        if (banner && STATE.oauth && STATE.oauth.authorized) {
          banner.outerHTML = "";
        }
        try { scheduleScan(); } catch (_) {}
      });
    }, false);
  }

  function fmtConsentDate(epoch) {
    if (!epoch) return "";
    try {
      var d = new Date(epoch * 1000);
      return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
    } catch (_) { return ""; }
  }

  function cleanFilename(text) {
    if (!text) return "";
    // Canvas prefixes filenames in screen-reader spans by file type:
    // "PDF File <name>", "Doc File <name>", "Word File <name>", etc.
    // Strip any "<type> File " or plain "File " prefix so the cleaned
    // filename matches the API's by_filename keys exactly.
    return text.trim()
      .replace(/^(?:PDF|Word|Doc|Document|Excel|Spreadsheet|PowerPoint|Slide|Slides|Image|Audio|Video|Text|HTML|EPub)?\s*File\s+/i, "")
      .trim();
  }

  // Spans that exist only for screen readers — they typically carry the
  // filename text prefixed with "PDF File" or similar, so a naive scan
  // matches them and anchors the dial to the checkbox column instead of
  // the visible name cell. Detect both Instructure's Emotion class and
  // common ``sr-only`` variants.
  var _SR_ONLY_RE = /\b(screenReaderContent|sr-only|visually-?hidden)\b/;
  function _isScreenReaderOnly(el) {
    var cls = el.className;
    if (typeof cls !== "string") return false;
    return _SR_ONLY_RE.test(cls);
  }

  function findFileRows() {
    var rows = document.querySelectorAll('[data-testid="table-row"]');
    var out = [];
    for (var i = 0; i < rows.length; i++) {
      var row = rows[i];
      if (row.dataset.reflowDecorated) continue;
      // Prefer the dedicated name cell when Canvas marks one — that's where
      // the user-visible filename lives in the new Files UI. Falls back to
      // scanning the whole row for legacy / non-table contexts.
      var nameCell = row.querySelector('[data-testid="table-cell-name"]');
      var scanRoots = nameCell ? [nameCell, row] : [row];
      var filename = "";
      var filenameSpan = null;
      for (var r = 0; r < scanRoots.length && !filename; r++) {
        var spans = scanRoots[r].querySelectorAll("span");
        for (var j = 0; j < spans.length; j++) {
          // Skip non-leaf spans: Canvas wraps the file-icon SVG and the
          // filename text inside the SAME outer flex container, and the
          // SVG carries an aria-labelling ``<title>PDF File</title>``
          // element. Calling .textContent on the outer span concatenates
          // the title text and the filename text — producing strings like
          // ``"PDF File01 SPRITE Chimera Student Module.pdf"`` that match
          // the extension regex but don't match any scored_files key.
          // Only the innermost text-bearing span (no element children)
          // carries the bare filename we need.
          if (spans[j].children.length > 0) continue;
          if (_isScreenReaderOnly(spans[j])) continue;
          var t = cleanFilename(spans[j].textContent);
          if (CONVERTIBLE_EXT_RE.test(t)) {
            filename = t;
            filenameSpan = spans[j];
            break;
          }
        }
      }
      if (!filename) continue;
      out.push({ row: row, filename: filename, filenameSpan: filenameSpan, nameCell: nameCell });
    }
    return out;
  }

  function findModuleFiles() {
    var items = document.querySelectorAll(
      'li.context_module_item.type_file, li.context_module_item.File, li.context_module_item[class*="attachment"]'
    );
    var out = [];
    for (var i = 0; i < items.length; i++) {
      var item = items[i];
      if (item.dataset.reflowDecorated) continue;
      var titleEl = item.querySelector('.ig-title, .item_name a, a.title');
      var filename = titleEl ? titleEl.textContent.trim() : item.textContent.trim().split(/\s{2,}/)[0];
      if (!filename || !CONVERTIBLE_EXT_RE.test(filename)) continue;
      out.push({ item: item, filename: filename, anchor: titleEl || item });
    }
    return out;
  }

  function findFileLinkAnchors() {
    // Anchor surfaces. The first two ``a[href*="/files/"]`` and
    // ``a.instructure_file_link`` already cover most cases; the rest
    // explicitly call out surfaces where Canvas's themes/templates use
    // distinct classes:
    //
    //   * ``a.instructure_scribd_file``    -- legacy file preview popup
    //   * ``a.file_preview_link``          -- newer file preview link
    //   * ``a.instructure_inline_media_comment[href*="/files/"]``
    //                                      -- inline media inside pages
    //   * ``.user_content a[href*="/files/"]``
    //                                      -- WYSIWYG page bodies
    //   * ``.assignment_description a[href*="/files/"]``
    //                                      -- assignment instructions
    //   * ``.discussion-section a[href*="/files/"]``
    //                                      -- discussion topics
    //   * ``.context_module_item a[href*="/files/"]``
    //                                      -- module item rows
    //   * ``.attachment a[href*="/files/"]``
    //                                      -- assignment attachments
    var selectors = ['a[href*="/files/"]',
                     'a.instructure_file_link',
                     'a.instructure_scribd_file',
                     'a.file_preview_link',
                     'a.instructure_inline_media_comment[href*="/files/"]',
                     '.user_content a[href*="/files/"]',
                     '.assignment_description a[href*="/files/"]',
                     '.discussion-section a[href*="/files/"]',
                     '.context_module_item a[href*="/files/"]',
                     '.attachment a[href*="/files/"]'];
    var seen = new Set();
    var out = [];
    selectors.forEach(function (sel) {
      var links = document.querySelectorAll(sel);
      for (var i = 0; i < links.length; i++) {
        var a = links[i];
        if (a.dataset.reflowDecorated || seen.has(a)) continue;
        var href = a.getAttribute("href") || "";
        var m = href.match(FILE_HREF_RE);
        if (!m) continue;
        if (/\/files\/folder\//.test(href)) continue;
        seen.add(a);
        out.push({ anchor: a, fileId: m[1] });
      }
    });

    // Iframe-embedded PDF previews. Canvas's WYSIWYG inserts iframes
    // pointing to ``/courses/.../files/<id>/preview`` for inline file
    // previews. These don't have an ``href`` like an anchor, so we
    // synthesize a placeholder anchor sibling that the rest of the
    // overlay can attach to.
    var iframeSelectors = [
      'iframe.preview_overlay',
      'iframe[src*="/files/"][src*="/preview"]',
      'iframe.preview_iframe',
    ];
    iframeSelectors.forEach(function (sel) {
      var frames = document.querySelectorAll(sel);
      for (var i = 0; i < frames.length; i++) {
        var f = frames[i];
        if (f.dataset.reflowDecorated) continue;
        var src = f.getAttribute("src") || "";
        var m = src.match(FILE_HREF_RE);
        if (!m) continue;
        // Synthesize an anchor next to the iframe so the existing
        // button-attachment code path works unchanged.
        var anchor = document.createElement("a");
        anchor.href = src;
        anchor.style.display = "none";
        anchor.dataset.reflowSynthesized = "1";
        f.parentNode.insertBefore(anchor, f);
        f.dataset.reflowDecorated = "1";
        seen.add(anchor);
        out.push({ anchor: anchor, fileId: m[1], iframeHost: f });
      }
    });

    return out;
  }

  async function fetchScoresByFileId(fileIds) {
    if (!STATE.courseId || !fileIds.length) return {};
    var url = ORIGIN + "/canvas/panorama/score?course_id=" + encodeURIComponent(STATE.courseId)
            + "&file_ids=" + fileIds.join(",");
    var r = await fetch(url, { credentials: "omit", headers: BACKEND_HEADERS });
    if (!r.ok) return {};
    var data = await r.json();
    return data.scores || {};
  }

  // Accessible-version links point at the tool endpoint
  // ``/canvas/panorama/alt/<job_id>/<fmt>``. Collect them so we can show the
  // OUTPUT (WCAG) score on the "Open the accessible version" link inside a
  // published Canvas Page — the empty dial the old file-link path produced.
  function findAccessibleLinks() {
    var links = document.querySelectorAll('a[href*="/canvas/panorama/alt/"]');
    var seen = new Set();
    var out = [];
    for (var i = 0; i < links.length; i++) {
      var a = links[i];
      if (a.dataset.reflowDecorated || seen.has(a)) continue;
      // Only decorate accessible links that live in Canvas's own content (the
      // published Page stub). Our own overlay UI — the formats modal, the
      // student CTA, the language picker, the editor — is full of
      // ``/canvas/panorama/alt/…`` links too (every format card is one), and
      // those must NOT get a dial. All overlay UI uses ``reflow-pn-`` classes,
      // so an anchor with such an ancestor (or class) is ours; skip it.
      if (a.closest('[class*="reflow-pn"]')) continue;
      var href = a.getAttribute("href") || "";
      var m = href.match(ACCESSIBLE_HREF_RE);
      if (!m) continue;
      seen.add(a);
      out.push({ anchor: a, jobId: decodeURIComponent(m[1]) });
    }
    return out;
  }

  async function fetchScoresByJobId(jobIds) {
    if (!jobIds.length) return {};
    var url = ORIGIN + "/canvas/panorama/score_by_job?job_ids=" + jobIds.map(encodeURIComponent).join(",");
    var r = await fetch(url, { credentials: "omit", headers: BACKEND_HEADERS });
    if (!r.ok) return {};
    var data = await r.json();
    return data.scores || {};
  }

  // Local mirror of the backend severity buckets, for the rare case a
  // payload carries a score without its severity.
  function severityFor(score) {
    if (typeof score !== "number") return "unscanned";
    if (score >= 90) return "dark-green";
    if (score >= 67) return "green";
    if (score >= 33) return "amber";
    return "red";
  }

  // Does the accessible version have something an instructor should fix
  // or improve: an error or a warning on the automated checks? Items to
  // confirm by eye (an image marked decorative) don't count; the menu
  // lists them.
  function _needsWork(payload) {
    if (!payload || payload.status !== "scored" || typeof payload.score !== "number") return false;
    var c = payload.checks;
    return c ? !!(c.errors || c.warnings) : payload.score < 100;
  }

  var _CHECK_VIEW = { score: 100, label: "✓", color: COLORS.green, severity: "green" };

  function _scoreView(payload, note) {
    var sc = payload.score;
    var sev = payload.severity || severityFor(sc);
    return { score: sc, label: sc + "%", color: COLORS[sev] || COLORS.red, severity: sev,
             title: "Accessible version: " + sc + " out of 100 on the automated checks." + (note || "") + " Open the formats menu.",
             aria: "Accessible version scores " + sc + " out of 100 on the automated checks." + (note || "")
                   + " Open the alternative formats menu." };
  }

  // What the dial beside a file shows. It speaks for the accessible
  // version, the thing students get, never for the original file: the
  // original's PDF/UA result is in the menu, in words. (As a share of
  // PDF/UA rules passed it read 93% for a PDF with no tags at all, and a
  // number beside the original file's link reads as that file's score.)
  //
  // Students get a dial only once the document is published (see
  // shouldDecorate), and it says one thing: there is an accessible version.
  // Instructors get what needs doing: review a draft, wait for a
  // conversion, look at a failure, or fix the accessible version (its
  // score, shown only when something needs fixing). A check means
  // published and passing.
  function dialView(payload) {
    var js = String(payload.job_status || "").toLowerCase();
    if (STATE.userRole !== "Instructor") {
      return Object.assign({}, _CHECK_VIEW, {
        title: "Accessible version available. Open the formats menu.",
        aria: "Accessible version available. Open the alternative formats menu." });
    }
    if (js === "awaiting_approval") {
      return { score: 0, label: "!", color: COLORS.amber, severity: "amber",
               title: "Privacy review needed. Open the menu to approve or deny.",
               aria: "A privacy review is needed before this document can finish converting. Open the menu to approve or deny." };
    }
    if (js === "awaiting_review") {
      return { score: 0, label: "!", color: COLORS.amber, severity: "amber",
               title: "Draft: review it and publish it for students.",
               aria: "Draft accessible version. Open the menu to review and publish it for students." };
    }
    if (js === "processing" || js === "pii_scanning" || js === "processing_queued") {
      return { score: 0, label: "…", color: COLORS.unscanned, severity: "unscanned",
               title: "Converting. Check back in a few minutes.",
               aria: "Converting. Check back in a few minutes." };
    }
    if (js === "failed" || js === "denied" || js === "rejected") {
      return { score: 0, label: "!", color: COLORS.red, severity: "red",
               title: "Conversion needs attention. Open the menu for details.",
               aria: "Conversion needs attention. Open the menu for details." };
    }
    if (js === "page_failed") {
      // The conversion finished but Canvas refused the page (usually a
      // missing Pages permission on the connected account): an action.
      return { score: 0, label: "!", color: COLORS.amber, severity: "amber",
               title: "The Canvas page couldn’t be created. Open the menu for details.",
               aria: "The Canvas page couldn't be created. Open the menu for details." };
    }
    if (_needsWork(payload)) return _scoreView(payload);
    if (js === "published") {
      var passes = payload.status === "scored" && typeof payload.score === "number";
      return Object.assign({}, _CHECK_VIEW, {
        title: passes ? "Published. The accessible version passes the automated checks." : "Published. Open the formats menu.",
        aria: (passes ? "Published. The accessible version passes the automated checks." : "Published.")
              + " Open the alternative formats menu." });
    }
    return { score: 0, label: "—", color: COLORS.unscanned, severity: "unscanned",
             title: "Open the formats menu.",
             aria: "Open the alternative formats menu." };
  }

  // The dial on the accessible version's own link, inside the published
  // Canvas page. Same rule: a check, or for instructors the score when the
  // accessible version needs fixing.
  function accessibleDialView(payload) {
    var js = String((payload && payload.job_status) || "").toLowerCase();
    if (js === "processing" || js === "pii_scanning" || js === "processing_queued") {
      return { score: 0, label: "…", color: COLORS.unscanned, severity: "unscanned",
               title: "Converting. Check back in a few minutes.",
               aria: "Converting. Check back in a few minutes." };
    }
    if (STATE.userRole === "Instructor" && _needsWork(payload)) return _scoreView(payload);
    return Object.assign({}, _CHECK_VIEW, {
      title: "Accessible version. Open the formats menu.",
      aria: "Accessible version. Open the alternative formats menu." });
  }

  // Should this file get a dial? Only files Iris has converted or is
  // converting: a link to anything else (an image, a file nobody sent to
  // Iris) would open an empty menu. Students only once it is published,
  // because that is when they can open the formats.
  function shouldDecorate(payload) {
    if (!payload || !payload.job_id) return false;
    if (STATE.userRole === "Instructor") return true;
    return String(payload.job_status || "").toLowerCase() === "published";
  }

  // Typography helper shared between the row dial and the big modal dial.
  // For numeric percentages we split into a large number + a small
  // superscript "%" so the digits get the visual breathing room they need.
  // For single-character labels (—, …, !, ✓) a single centered glyph is
  // cleaner than padding it out with a percent sign that doesn't apply.
  // ``sizes`` is a ``{num, pct, single}`` triple so the same helper can
  // serve both the 36-px row dial and the 52-px modal dial.
  function _dialTextSvg(label, color, sizes) {
    var s = sizes || { num: 14, pct: 7, single: 16 };
    var fam = "system-ui,-apple-system,Segoe UI,sans-serif";
    var pctMatch = String(label).match(/^(\d+)%$/);
    if (pctMatch) {
      var num = pctMatch[1];
      // ``100`` is 3 chars so shave a point off the number font to keep
      // the kerning inside the ring; ``98`` is 2 chars and gets full size.
      var nfs = num.length >= 3 ? Math.max(s.num - 1, 10) : s.num;
      return (
        '<text x="18" y="21.5" text-anchor="middle" font-weight="700" font-family="' + fam + '" fill="' + color + '" letter-spacing="-0.4">' +
        '<tspan font-size="' + nfs + '">' + num + '</tspan>' +
        '<tspan font-size="' + s.pct + '" dy="-3" dx="0.5">%</tspan>' +
        '</text>'
      );
    }
    // Non-numeric labels: single glyph, slightly lower y to optically
    // center within the ring.
    return (
      '<text x="18" y="22.5" text-anchor="middle" font-size="' + s.single + '" font-weight="700" font-family="' + fam + '" fill="' + color + '">' + label + '</text>'
    );
  }

  function makeDial(payload, filename, opts) {
    var btn = document.createElement("button");
    btn.type = "button";
    btn._iris = { payload: payload, filename: filename, opts: opts };
    _paintDial(btn);
    btn.addEventListener("click", function (e) {
      e.preventDefault(); e.stopPropagation();
      openFormatsModal(payload, filename);
    });
    return btn;
  }

  // After Publish, Reject or Unpublish, every dial for that document says
  // its new state without a reload, and opens a menu that matches. Dials
  // are repainted in place, so the one that opened the menu still takes
  // focus back when it closes.
  var _STATUS_AFTER = { approve: "published", reject: "rejected", unpublish: "awaiting_review", request_edits: "awaiting_review" };

  function _restatusDials(jobId, action) {
    var status = _STATUS_AFTER[action];
    if (!jobId || !status) return;
    var dials = document.querySelectorAll(".reflow-pn-dial");
    for (var i = 0; i < dials.length; i++) {
      var bound = dials[i]._iris;
      if (!bound || bound.payload.job_id !== jobId) continue;
      bound.payload.job_status = status;
      _paintDial(dials[i]);
    }
  }

  function _paintDial(btn) {
    var bound = btn._iris;
    var payload = bound.payload, opts = bound.opts;
    var v = (opts && opts.accessible) ? accessibleDialView(payload) : dialView(payload);
    var score = v.score;
    var severity = v.severity;
    var color = v.color;
    var label = v.label;
    btn.className = "reflow-pn-dial reflow-pn-sev-" + severity;
    btn.title = v.title;
    btn.setAttribute("aria-label", v.aria);
    // Slimmer stroke + lighter track + split number/percent typography
    // give the dial a polished, production-ready feel without changing
    // the size of the slot in the actions cell. The viewBox stays 36×36
    // (with r=15.9155 so 2πr ≈ 100, letting stroke-dasharray take a raw
    // percentage value).
    btn.innerHTML =
      '<svg viewBox="0 0 36 36" width="36" height="36" aria-hidden="true">' +
      '  <circle cx="18" cy="18" r="15.9155" fill="none" stroke="#eef0f3" stroke-width="2.6"/>' +
      (score > 0
        ? '  <circle cx="18" cy="18" r="15.9155" fill="none" stroke="' + color + '" stroke-width="2.6"' +
          '          stroke-dasharray="' + score + ' ' + (100 - score) + '" stroke-dashoffset="25"' +
          '          stroke-linecap="' + (score >= 100 ? 'butt' : 'round') + '" transform="rotate(-90 18 18)"/>'
        : '') +
      _dialTextSvg(label, color, { num: 14, pct: 7, single: 16 }) +
      '</svg>';
  }

  function makeWidgetWrap(payload, filename) {
    var wrap = document.createElement("span");
    wrap.className = "reflow-pn-wrap";
    wrap.appendChild(makeDial(payload, filename));
    return wrap;
  }

  // Downloads go through fetch rather than a plain link. A link's
  // ``download`` attribute is ignored when it points at another site, and
  // the connector is not Canvas: a plain link took the Canvas tab away to
  // raw text, and a failure replaced Canvas with an error page. This keeps
  // the person on the page, says what is being prepared (Searchable PDF can
  // take a minute the first time), and explains a failure in words.
  var DOWNLOAD_EXT = { audio: "mp3", ocr: "pdf", epub: "epub", braille: "brf" };

  function _say(statusEl, text) {
    if (!statusEl) return;
    statusEl.textContent = "";
    // A fresh node each time, so the same message twice is announced twice.
    setTimeout(function () { statusEl.textContent = text; }, 50);
  }

  function _downloadName(disposition, fallback) {
    if (!disposition) return fallback;
    var m = /filename\*\s*=\s*UTF-8''([^;]+)/i.exec(disposition);
    if (m) {
      try { return decodeURIComponent(m[1].trim()); } catch (e) { /* fall through */ }
    }
    m = /filename\s*=\s*"([^"]+)"/i.exec(disposition);
    return m ? m[1] : fallback;
  }

  function _wireDownload(el, url, fmt, filename, statusEl) {
    el.addEventListener("click", function (e) {
      // Open-in-new-tab and similar clicks keep the browser's own behavior.
      if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      e.preventDefault();
      if (el.getAttribute("aria-busy") === "true") return;
      var stem = String(filename || "document").replace(/\.[A-Za-z0-9]{1,5}$/, "");
      var fallback = stem + "." + (DOWNLOAD_EXT[fmt.id] || "bin");
      el.setAttribute("aria-busy", "true");
      el.classList.add("is-busy");
      var typeEl = el.querySelector(".reflow-pn-format-type");
      var typeText = typeEl ? typeEl.textContent : "";
      if (typeEl) typeEl.textContent = "Preparing\u2026";
      _say(statusEl, "Preparing " + fmt.label + "\u2026");
      // With credentials: an instructor's draft preview needs the LTI
      // cookie. A published document needs none and works either way.
      fetch(url, { credentials: "include", headers: BACKEND_HEADERS })
        .then(function (r) {
          if (!r.ok) {
            return r.json().catch(function () { return {}; }).then(function (body) {
              var err = new Error(body && typeof body.detail === "string" ? body.detail : "");
              err.status = r.status;
              throw err;
            });
          }
          var name = _downloadName(r.headers.get("Content-Disposition"), fallback);
          return r.blob().then(function (blob) {
            var href = URL.createObjectURL(blob);
            var a = document.createElement("a");
            a.href = href;
            a.download = name;
            a.style.display = "none";
            document.body.appendChild(a);
            a.click();
            setTimeout(function () { URL.revokeObjectURL(href); a.remove(); }, 60000);
            _say(statusEl, fmt.label + " downloaded (" + name + ").");
          });
        })
        .catch(function (err) {
          console.warn("[iris] download failed", fmt.id, err);
          var status = err && err.status;
          // Two cases a plain navigation still handles: the request never got
          // an answer this page may read (a CORS or network failure), and an
          // instructor's draft refused because the browser kept the LTI
          // cookie out of a cross-site fetch (Safari does). A navigation is
          // first-party, and every download is an attachment, so the browser
          // saves it and Canvas stays put.
          if (!status || (status === 403 && STATE.userRole === "Instructor")) {
            _say(statusEl, "Downloading " + fmt.label + "…");
            window.location.href = url;
            return;
          }
          // 4xx details are written for people ("pending faculty review",
          // "under development"); anything else gets a plain sentence.
          var msg = (status < 500 && err.message)
            ? err.message
            : "It could not be prepared right now. Please try again in a few minutes.";
          _say(statusEl, fmt.label + ": " + msg);
        })
        .then(function () {
          el.removeAttribute("aria-busy");
          el.classList.remove("is-busy");
          if (typeEl) typeEl.textContent = typeText;
        });
    });
  }

  // ---- The Alternative Formats Menu ------------------------------------
  //
  // One dialog, two audiences. Students get the accessible version first
  // and the other formats under it. Instructors also get where the
  // document stands, with the publish actions, and the two accessibility
  // checks in words: the accessible version (what students get) beside
  // the original file (what was uploaded).

  var FORMAT_TEXT = {
    html:      { desc: "A web page that works with screen readers, zoom and text-to-speech.", type: "Opens in a new tab", kind: "page" },
    immersive: { desc: "Reads aloud, with larger text, a dark theme and a reading line.", type: "Opens in a new tab", kind: "page" },
    ocr:       { desc: "The original pages, tagged for screen readers and search.", type: "PDF", kind: "file" },
    epub:      { desc: "An e-book for apps such as Apple Books and Thorium Reader.", type: "EPUB", kind: "file" },
    braille:   { desc: "Contracted braille for embossers and braille displays.", type: "BRF", kind: "file" },
    audio:     { desc: "Listen to the document.", type: "MP3", kind: "file" },
    translate: { desc: "Read it in another language.", type: "Opens in a new tab", kind: "page" }
  };

  function _isInstructor() { return STATE.userRole === "Instructor"; }

  function _previewSuffix(payload) {
    return (payload.job_status !== "published" && _isInstructor()) ? "?preview=1" : "";
  }

  function _altHref(payload, fmtId) {
    return ORIGIN + "/canvas/panorama/alt/" + encodeURIComponent(payload.job_id || "") + "/" + fmtId + _previewSuffix(payload);
  }

  // The accessible Canvas page, unless this IS that page.
  function _canvasPageLink(payload) {
    var href = _canvasPageHref(payload);
    if (!href) return "";
    try {
      if (new URL(href, location.href).pathname.replace(/\/$/, "") === location.pathname.replace(/\/$/, "")) return "";
    } catch (e) { /* show the link */ }
    return href;
  }

  function openFormatsModal(payload, filename) {
    var existing = document.getElementById("reflow-pn-modal");
    if (existing) existing.remove();
    // The dial on a link inside a page passes the link's words ("Original
    // PDF"); the job knows the file's real name.
    var name = payload.file_name || filename || "Document";
    var instructor = _isInstructor();

    var overlay = document.createElement("div");
    overlay.id = "reflow-pn-modal";
    overlay.className = "reflow-pn-modal";

    var modal = document.createElement("div");
    modal.className = "reflow-pn-modal-card";
    modal.setAttribute("role", "dialog");
    modal.setAttribute("aria-modal", "true");
    modal.setAttribute("aria-labelledby", "reflow-pn-modal-title");
    modal.innerHTML =
      '<div class="reflow-pn-modal-header">' +
      '  <div class="reflow-pn-modal-titles">' +
      '    <h2 id="reflow-pn-modal-title">' + esc(name) + '</h2>' +
      '    <p>Alternative formats' + (payload.edited && instructor ? ' <span class="reflow-pn-edited">Edited by faculty</span>' : '') + '</p>' +
      '  </div>' +
      '  <button class="reflow-pn-modal-close" type="button" aria-label="Close">' +
      '    <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" focusable="false"><path d="M3.5 3.5l9 9m0-9l-9 9" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>' +
      '  </button>' +
      '</div>' +
      '<div class="reflow-pn-modal-body">' +
        (instructor ? _renderAuthBanner() + _renderConsentBanner() : '') +
        (instructor ? _renderApprovalBar(payload) : '') +
        (instructor ? _renderChecks(payload) : _renderStudentPrimary(payload)) +
        '<section class="reflow-pn-formats" aria-labelledby="reflow-pn-formats-title">' +
        '  <h3 id="reflow-pn-formats-title" class="reflow-pn-section-title">' + (instructor ? 'Formats for students' : 'Other formats') + '</h3>' +
        '  <div class="reflow-pn-modal-grid"></div>' +
        '</section>' +
      '</div>' +
      (instructor ? '<div class="reflow-pn-modal-footer">' + _renderConsentFooter() + '</div>' : '');

    overlay.appendChild(modal);
    document.body.appendChild(overlay);

    // Phase 13: WCAG 2.1 AA hygiene for the modal itself.
    // - Remember what was focused so we can restore on close
    // - Move focus to the first interactive control inside the modal
    // - Trap Tab/Shift+Tab inside the modal
    // - Close on Escape; close button is reachable via keyboard
    var previouslyFocused = document.activeElement;
    _wcagify_modal(overlay, modal, previouslyFocused);

    _buildFormats(modal.querySelector(".reflow-pn-modal-grid"), payload, name);

    function close() { overlay.remove(); }
    overlay.addEventListener("click", function (e) { if (e.target === overlay) close(); });
    modal.querySelector(".reflow-pn-modal-close").addEventListener("click", close);

    // "Authorize Iris" (instructors who haven't connected their Canvas
    // account). Opens the consent popup; the message listener refreshes
    // status on success.
    var authBtn = modal.querySelector(".reflow-pn-auth-btn");
    if (authBtn) {
      authBtn.addEventListener("click", function (e) {
        e.preventDefault();
        _installOAuthMessageListener();
        openAuthPopup();
      });
    }

    // Re-check consent on open so the "Authorize Iris" notice disappears for
    // instructors who already accepted, including in another tab, and the
    // footer says when.
    if (instructor) {
      loadConsentStatus().then(function (c) {
        if (!c || !c.agreed) return;
        var cb = modal.querySelector("#reflow-pn-consent-banner");
        if (cb) cb.remove();
        var ft = modal.querySelector(".reflow-pn-modal-footer .reflow-pn-footer-text");
        if (ft) ft.innerHTML = _consentAgreedText(c);
      }).catch(function () {});
    }

    // Publish / Reject / Unpublish, and the privacy decision.
    _wireApprovalActions(modal, payload, function () { close(); openFormatsModal(payload, filename); });

    var editBtn = modal.querySelector(".reflow-pn-edit-btn");
    if (editBtn) {
      editBtn.addEventListener("click", function () {
        openHtmlEditor(payload, name);
      });
    }

    document.addEventListener("keydown", function escK(e) {
      if (e.key === "Escape") {
        var m = document.getElementById("reflow-pn-modal");
        var ed = document.getElementById("reflow-pn-editor");
        if (ed) ed.remove();
        else if (m) {
          m.remove();
          try { if (previouslyFocused && previouslyFocused.focus) previouslyFocused.focus(); } catch (_) { /* gone */ }
        }
        document.removeEventListener("keydown", escK);
      }
    });
  }

  // Students: the accessible version is the answer, so it comes first.
  function _renderStudentPrimary(payload) {
    if (!payload.job_id) return "";
    var href = _canvasPageLink(payload) || _altHref(payload, "html");
    return '<section class="reflow-pn-student-cta">' +
      '  <a class="reflow-pn-student-primary" href="' + esc(href) + '" target="_blank" rel="noopener">' +
      '    <span class="reflow-pn-student-primary-text">Open the accessible version</span>' +
      '    <span class="reflow-pn-student-primary-hint">Opens in a new tab</span>' +
      '  </a>' +
      '  <p class="reflow-pn-student-note">Made for screen readers, zoom and text-to-speech. The original file is unchanged.</p>' +
      '</section>';
  }

  // The format list: what a student can open or download, grouped by what
  // they would do with it. Formats this server cannot make yet are named in
  // one line at the end rather than shown as dead buttons.
  function _buildFormats(grid, payload, name) {
    var available = payload.available_formats || ["html"];
    var held = payload.under_development || [];
    // A student's first button opens the accessible HTML when there is no
    // Canvas page to send them to; it needn't be listed again under it.
    var shown = (!_isInstructor() && !_canvasPageLink(payload)) ? ["html"] : [];
    var status = document.createElement("p");
    status.className = "reflow-pn-fmt-status";
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");
    var heldLabels = [];
    FORMAT_GROUPS.forEach(function (group) {
      var rows = [];
      FORMATS.filter(function (f) { return f.group === group.id; }).forEach(function (fmt) {
        if (held.indexOf(fmt.id) >= 0) { heldLabels.push(fmt.label.replace(/…$/, "")); return; }
        if (shown.indexOf(fmt.id) >= 0) return;
        var live = fmt.live && (available.indexOf(fmt.id) >= 0 || fmt.id === "immersive" || fmt.id === "braille" || fmt.id === "ocr");
        if (!live || !payload.job_id) return;
        rows.push(_formatRow(fmt, payload, name, status));
      });
      if (!rows.length) return;
      var section = document.createElement("div");
      section.className = "reflow-pn-fmt-group";
      var title = document.createElement("h4");
      title.className = "reflow-pn-fmt-group-label";
      title.textContent = group.label;
      var list = document.createElement("ul");
      list.className = "reflow-pn-fmt-list";
      rows.forEach(function (row) {
        var li = document.createElement("li");
        li.appendChild(row);
        list.appendChild(li);
      });
      section.appendChild(title);
      section.appendChild(list);
      grid.appendChild(section);
    });
    if (heldLabels.length) {
      var note = document.createElement("p");
      note.className = "reflow-pn-fmt-later";
      note.textContent = "Under development: " + _joinWords(heldLabels) + ".";
      grid.appendChild(note);
    }
    grid.appendChild(status);
  }

  function _joinWords(words) {
    if (words.length < 2) return words.join("");
    return words.slice(0, -1).join(", ") + " and " + words[words.length - 1];
  }

  function _formatRow(fmt, payload, name, statusEl) {
    var text = FORMAT_TEXT[fmt.id] || { desc: "", type: "", kind: "page" };
    var descId = "reflow-pn-fmt-desc-" + fmt.id;
    var el = document.createElement("a");
    el.className = "reflow-pn-format";
    el.innerHTML =
      '<span class="reflow-pn-format-name">' + esc(fmt.label) + '</span>' +
      '<span class="reflow-pn-format-type">' + esc(text.type) + '</span>' +
      '<span class="reflow-pn-format-desc" id="' + descId + '">' + esc(text.desc) + '</span>';
    el.setAttribute("aria-describedby", descId);
    if (fmt.picker === "language") {
      el.href = "#";
      el.setAttribute("aria-label", fmt.label.replace(/…$/, "") + ", choose a language");
      el.addEventListener("click", function (e) {
        e.preventDefault();
        openLanguagePicker(payload);
      });
      return el;
    }
    el.href = _altHref(payload, fmt.id);
    if (fmt.download) {
      // "Braille (BRF)" already names its type; "ePub" doesn't.
      var named = fmt.label.indexOf("(" + text.type + ")") >= 0 ? fmt.label : fmt.label + " (" + text.type + ")";
      el.setAttribute("aria-label", "Download " + named);
      _wireDownload(el, el.href, fmt, name, statusEl);
    } else {
      el.target = "_blank";
      el.rel = "noopener";
      el.setAttribute("aria-label", fmt.label + ", opens in a new tab");
    }
    return el;
  }

  // ---- The two checks (instructors) -----------------------------------

  function _renderChecks(payload) {
    if (!payload.job_id) return "";
    return '<section class="reflow-pn-checks" aria-label="Accessibility checks">' +
      _renderAccessibleCheck(payload) +
      _renderOriginalCheck(payload) +
      '</section>';
  }

  var _SEVERITY_WORD = { error: "Fix", warning: "Improve", info: "Review" };

  function _renderAccessibleCheck(payload) {
    var scored = payload.status === "scored" && typeof payload.score === "number";
    var checks = payload.checks || null;
    var verdict, tone;
    if (!scored) {
      verdict = String(payload.job_status || "") === "processing" ? "Being converted" : "Not checked yet";
      tone = "muted";
    } else if (checks && !checks.errors && !checks.warnings) {
      verdict = "Passes the automated checks";
      tone = "good";
    } else if (checks) {
      var parts = [];
      if (checks.errors) parts.push(checks.errors + (checks.errors === 1 ? " problem" : " problems") + " to fix");
      if (checks.warnings) parts.push(checks.warnings + (checks.warnings === 1 ? " thing" : " things") + " to improve");
      verdict = parts.join(", ");
      verdict = verdict.charAt(0).toUpperCase() + verdict.slice(1);
      tone = checks.errors ? "problem" : "caution";
    } else {
      verdict = payload.score + " out of 100 on the automated checks";
      tone = payload.score >= 90 ? "good" : (payload.score >= 67 ? "caution" : "problem");
    }
    var items = (checks && checks.items) || [];
    var list = items.length
      ? '<ul class="reflow-pn-check-items">' + items.slice(0, 4).map(function (it) {
          var sev = it.severity in _SEVERITY_WORD ? it.severity : "info";
          return '<li><span class="reflow-pn-sev reflow-pn-sev--' + sev + '">' + _SEVERITY_WORD[sev] + '</span> ' + esc(it.label) + '</li>';
        }).join("") + '</ul>'
      : "";
    var score = scored
      ? '<p class="reflow-pn-check-note">Automated score: ' + payload.score + ' out of 100. Automated checks find structure problems; they can’t judge whether descriptions are right.</p>'
      : "";
    var page = _canvasPageLink(payload);
    var actions =
      '<div class="reflow-pn-check-actions">' +
      (page ? '<a class="reflow-pn-action" href="' + esc(page) + '" target="_blank" rel="noopener" aria-label="Open in Canvas, opens in a new tab">Open in Canvas</a>' : '') +
      '<button class="reflow-pn-action reflow-pn-edit-btn" type="button">Edit</button>' +
      '</div>';
    return '<div class="reflow-pn-check reflow-pn-check--accessible">' +
      '  <h3 class="reflow-pn-section-title">Accessible version</h3>' +
      '  <p class="reflow-pn-check-verdict is-' + tone + '">' + esc(verdict) + '</p>' +
      list + score + actions +
      '</div>';
  }

  function _renderOriginalCheck(payload) {
    var o = payload.original || null;
    var body;
    var how = '<p class="reflow-pn-check-note">Checked against PDF/UA-1 with <a href="https://verapdf.org" target="_blank" rel="noopener">veraPDF</a> when the file was uploaded.</p>';
    if (!o) {
      body = '<p class="reflow-pn-check-verdict is-muted">Not checked</p>' +
        '<p class="reflow-pn-check-note">' + (/\.pdf$/i.test(payload.file_name || "")
          ? "The PDF/UA check didn’t run for this file."
          : "Only PDFs are checked against PDF/UA.") + '</p>';
    } else {
      var tone = o.verdict === "passes" ? "good" : "problem";
      var problems = o.problems || [];
      var shown = problems.filter(function (p) { return !(o.verdict === "untagged" && /has no tags/.test(p.label)); });
      var lead = o.verdict === "untagged"
        ? '<p class="reflow-pn-check-lead">Screen readers get no headings, lists or reading order from it.</p>'
        : "";
      var list = shown.length
        ? '<ul class="reflow-pn-check-items reflow-pn-check-items--plain">' + shown.slice(0, 3).map(function (p) {
            return '<li>' + esc(p.label) + '</li>';
          }).join("") + '</ul>'
        : "";
      var all = problems.length
        ? '<details class="reflow-pn-vrules">' +
          '  <summary>All ' + problems.length + (problems.length === 1 ? ' problem' : ' problems') + ' veraPDF found</summary>' +
          '  <ul class="reflow-pn-vrule-list">' + problems.map(function (p) {
              return '<li><span class="reflow-pn-vrule-label">' + esc(p.label) + '</span>' +
                '<span class="reflow-pn-vrule-desc">' + esc((p.rules || []).map(function (r) { return "Rule " + r; }).join(", ")) +
                (p.detail ? ': ' + esc(p.detail) : '') + '</span></li>';
            }).join("") + '</ul>' + how +
          '</details>'
        : how;
      body = '<p class="reflow-pn-check-verdict is-' + tone + '">' + esc(o.headline) + '</p>' + lead + list + all;
    }
    return '<div class="reflow-pn-check reflow-pn-check--original">' +
      '  <h3 class="reflow-pn-section-title">Original file</h3>' +
      body +
      '</div>';
  }

  // Faculty HTML editor with live preview. Loads canonical HTML from
  // /edit/{job_id}, lets faculty rewrite it, saves via PUT.
  async function openHtmlEditor(payload, filename) {
    var jobId = payload.job_id;
    if (!jobId) return;
    var existing = document.getElementById("reflow-pn-editor");
    if (existing) existing.remove();

    var overlay = document.createElement("div");
    overlay.id = "reflow-pn-editor";
    overlay.className = "reflow-pn-editor";
    overlay.innerHTML =
      '<div class="reflow-pn-editor-card">' +
      '  <div class="reflow-pn-editor-head">' +
      '    <div class="reflow-pn-editor-titles">' +
      '      <h2>Edit Accessible HTML</h2>' +
      '      <p>' + esc(filename || "Document") + ' <span id="reflow-pn-editor-state"></span></p>' +
      '    </div>' +
      '    <button class="reflow-pn-editor-close" type="button" aria-label="Close">&times;</button>' +
      '  </div>' +
      '  <div class="reflow-pn-editor-toolbar" role="toolbar" aria-label="Formatting">' +
      '    <button type="button" data-cmd="bold" title="Bold (Ctrl+B)"><b>B</b></button>' +
      '    <button type="button" data-cmd="italic" title="Italic (Ctrl+I)"><i>I</i></button>' +
      '    <button type="button" data-block="H2" title="Heading">H2</button>' +
      '    <button type="button" data-block="H3" title="Subheading">H3</button>' +
      '    <button type="button" data-block="P" title="Normal text" aria-label="Normal text">¶</button>' +
      '    <button type="button" data-cmd="insertUnorderedList" title="Bullet list">• List</button>' +
      '    <button type="button" data-cmd="insertOrderedList" title="Numbered list">1. List</button>' +
      '    <button type="button" data-act="link" title="Add a link">🔗 Link</button>' +
      '    <span class="reflow-pn-editor-toolbar-sep"></span>' +
      '    <button type="button" data-act="images" class="reflow-pn-editor-toggle" title="Add or fix image descriptions (alt text)">🖼 Image descriptions</button>' +
      '    <button type="button" data-act="toggle-source" class="reflow-pn-editor-toggle" title="Switch between the visual editor and raw HTML">&lt;/&gt; HTML</button>' +
      '    <span class="reflow-pn-editor-spacer"></span>' +
      '    <button type="button" data-act="revert" class="reflow-pn-btn-secondary">Revert to auto</button>' +
      '    <button type="button" data-act="save" class="reflow-pn-btn-primary">Save</button>' +
      '    <button type="button" data-act="publish" class="reflow-pn-btn-publish" title="Save your edits and publish — makes this accessible version visible to students in the course">✓ Publish</button>' +
      '  </div>' +
      '  <div class="reflow-pn-editor-body">' +
      '    <div class="reflow-pn-editor-visual" contenteditable="true" role="textbox" aria-multiline="true" aria-label="Document content — type and format directly" spellcheck="true"></div>' +
      '    <aside class="reflow-pn-editor-alt" hidden aria-label="Image descriptions"></aside>' +
      '    <div class="reflow-pn-editor-sourcewrap" hidden>' +
      '      <textarea class="reflow-pn-editor-source" placeholder="Loading…" spellcheck="false"></textarea>' +
      '      <iframe class="reflow-pn-editor-preview" title="Live preview"></iframe>' +
      '    </div>' +
      '  </div>' +
      '  <div class="reflow-pn-editor-status"></div>' +
      '</div>';
    document.body.appendChild(overlay);

    var card = overlay.querySelector(".reflow-pn-editor-card");
    var visual = card.querySelector(".reflow-pn-editor-visual");
    var altPanel = card.querySelector(".reflow-pn-editor-alt");
    var sourceWrap = card.querySelector(".reflow-pn-editor-sourcewrap");
    var textarea = card.querySelector(".reflow-pn-editor-source");
    var preview = card.querySelector(".reflow-pn-editor-preview");
    var status = card.querySelector(".reflow-pn-editor-status");
    var stateEl = card.querySelector("#reflow-pn-editor-state");
    var sourceMode = false;   // false = visual (contenteditable), true = raw HTML
    var altOpen = false;

    function setStatus(msg, isErr) {
      status.textContent = msg;
      status.className = "reflow-pn-editor-status" + (isErr ? " is-error" : "");
    }
    function close() { overlay.remove(); }
    card.querySelector(".reflow-pn-editor-close").addEventListener("click", close);

    // The HTML we persist. In visual mode it's the contenteditable body
    // (minus our transient bookkeeping attributes); in source mode the
    // textarea is authoritative.
    function getHtml() {
      if (sourceMode) return textarea.value;
      var clone = visual.cloneNode(true);
      clone.querySelectorAll("[data-reflow-img]").forEach(function (el) {
        el.removeAttribute("data-reflow-img");
      });
      return clone.innerHTML;
    }
    function setHtml(html) {
      visual.innerHTML = html || "";
      textarea.value = html || "";
    }

    function refreshPreview() {
      var doc = preview.contentDocument;
      if (!doc) return;
      doc.open();
      doc.write(
        '<!doctype html><html><head><meta charset="utf-8">' +
        '<script async src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js"></script>' +
        '<style>body{font-family:Georgia,serif;max-width:42rem;margin:1.5rem auto;padding:0 1rem;line-height:1.6;}' +
        'img{max-width:100%;}table{border-collapse:collapse;width:100%;}th,td{border:1px solid #ccc;padding:0.4rem;}</style>' +
        '</head><body>' + textarea.value + '</body></html>'
      );
      doc.close();
    }

    // Switch between the visual editor and the raw-HTML pane (which keeps the
    // live MathJax preview for power users editing math/tables).
    function setSourceMode(on) {
      if (on === sourceMode) return;
      sourceMode = on;
      var btn = card.querySelector('[data-act="toggle-source"]');
      if (sourceMode) {
        textarea.value = getHtml();    // serialise current visual edits
        refreshPreview();
        visual.hidden = true; altPanel.hidden = true; sourceWrap.hidden = false;
        if (btn) { btn.classList.add("is-active"); btn.textContent = "✓ Visual editor"; }
      } else {
        visual.innerHTML = textarea.value;   // adopt raw edits back
        sourceWrap.hidden = true; visual.hidden = false;
        if (btn) { btn.classList.remove("is-active"); btn.innerHTML = "&lt;/&gt; HTML"; }
        if (altOpen) { buildAltPanel(); altPanel.hidden = false; }
      }
    }

    function toggleAlt() {
      // Alt editing is tied to the live visual DOM, so leave source mode first.
      if (sourceMode) setSourceMode(false);
      altOpen = !altOpen;
      var btn = card.querySelector('[data-act="images"]');
      if (altOpen) { buildAltPanel(); altPanel.hidden = false; if (btn) btn.classList.add("is-active"); }
      else { altPanel.hidden = true; if (btn) btn.classList.remove("is-active"); }
    }

    // Build the alt-text side panel from the images currently in the document.
    // Each row edits the matching <img>'s alt attribute live; empty alts are
    // flagged so faculty can see at a glance what still needs a description.
    function buildAltPanel() {
      var imgs = visual.querySelectorAll("img");
      if (!imgs.length) {
        altPanel.innerHTML = '<div class="reflow-pn-alt-head"><h3>Image descriptions</h3>' +
          '<p>This document has no images.</p></div>';
        return;
      }
      var parts = ['<div class="reflow-pn-alt-head"><h3>Image descriptions (alt text)</h3>' +
        '<p>Describe each image so screen-reader users get the same information. ' +
        'If an image is purely decorative, mark it decorative so screen readers skip it.</p></div>' +
        '<ul class="reflow-pn-alt-list">'];
      Array.prototype.forEach.call(imgs, function (img, i) {
        img.setAttribute("data-reflow-img", String(i));
        // Decorative is encoded as role="presentation" + empty alt — the
        // standard markup that tells assistive tech to ignore the image.
        var deco = img.getAttribute("role") === "presentation" ||
                   img.getAttribute("aria-hidden") === "true";
        var alt = img.getAttribute("alt") || "";
        var missing = !deco && !alt.trim();
        var src = img.getAttribute("src") || "";
        parts.push(
          '<li class="reflow-pn-alt-item' + (missing ? ' is-missing' : '') +
          (deco ? ' is-decorative' : '') + '">' +
          '<img class="reflow-pn-alt-thumb" src="' + esc(src) + '" alt="" loading="lazy" />' +
          '<div class="reflow-pn-alt-fields">' +
          '<label for="reflow-pn-alt-' + i + '">Image ' + (i + 1) +
          (missing ? ' <span class="reflow-pn-alt-flag">needs description</span>' : '') +
          (deco ? ' <span class="reflow-pn-alt-deco-tag">decorative</span>' : '') + '</label>' +
          '<textarea id="reflow-pn-alt-' + i + '" class="reflow-pn-alt-input" data-img="' + i +
          '" rows="2" placeholder="Describe this image…"' + (deco ? ' disabled' : '') + '>' +
          esc(alt) + '</textarea>' +
          '<label class="reflow-pn-alt-deco"><input type="checkbox" class="reflow-pn-alt-deco-box" data-img="' +
          i + '"' + (deco ? ' checked' : '') + '> This image is decorative (skip for screen readers)</label>' +
          '</div></li>'
        );
      });
      parts.push('</ul>');
      altPanel.innerHTML = parts.join("");

      function imgFor(idx) { return visual.querySelector('img[data-reflow-img="' + idx + '"]'); }

      altPanel.querySelectorAll(".reflow-pn-alt-input").forEach(function (inp) {
        inp.addEventListener("input", function () {
          var target = imgFor(inp.getAttribute("data-img"));
          if (!target) return;
          target.setAttribute("alt", inp.value);
          var li = inp.closest(".reflow-pn-alt-item");
          if (li && !li.classList.contains("is-decorative")) {
            li.classList.toggle("is-missing", !inp.value.trim());
            var flag = li.querySelector(".reflow-pn-alt-flag");
            if (!inp.value.trim() && !flag) {
              var lbl = li.querySelector("label[for]");
              if (lbl) lbl.insertAdjacentHTML("beforeend", ' <span class="reflow-pn-alt-flag">needs description</span>');
            } else if (inp.value.trim() && flag) {
              flag.remove();
            }
          }
          setStatus("Unsaved changes — click Save or Publish.");
        });
      });

      altPanel.querySelectorAll(".reflow-pn-alt-deco-box").forEach(function (box) {
        box.addEventListener("change", function () {
          var target = imgFor(box.getAttribute("data-img"));
          var li = box.closest(".reflow-pn-alt-item");
          var inp = li ? li.querySelector(".reflow-pn-alt-input") : null;
          var lbl = li ? li.querySelector("label[for]") : null;
          if (!target || !li) return;
          if (box.checked) {
            // Mark decorative: empty alt + presentation role, skip warnings.
            target.setAttribute("alt", "");
            target.setAttribute("role", "presentation");
            if (inp) { inp.value = ""; inp.disabled = true; }
            li.classList.remove("is-missing");
            li.classList.add("is-decorative");
            var f = li.querySelector(".reflow-pn-alt-flag");
            if (f) f.remove();
            if (lbl && !li.querySelector(".reflow-pn-alt-deco-tag")) {
              lbl.insertAdjacentHTML("beforeend", ' <span class="reflow-pn-alt-deco-tag">decorative</span>');
            }
          } else {
            // No longer decorative: it needs a real description again.
            target.removeAttribute("role");
            if (inp) { inp.disabled = false; inp.focus(); }
            li.classList.remove("is-decorative");
            var t = li.querySelector(".reflow-pn-alt-deco-tag");
            if (t) t.remove();
            li.classList.add("is-missing");
            if (lbl && !li.querySelector(".reflow-pn-alt-flag")) {
              lbl.insertAdjacentHTML("beforeend", ' <span class="reflow-pn-alt-flag">needs description</span>');
            }
          }
          setStatus("Unsaved changes — click Save or Publish.");
        });
      });
    }

    // Toolbar. data-cmd → execCommand inline; data-block → formatBlock;
    // data-act → editor actions.
    var debouncePreview = null;
    textarea.addEventListener("input", function () {
      clearTimeout(debouncePreview);
      debouncePreview = setTimeout(refreshPreview, 300);
    });
    visual.addEventListener("input", function () {
      setStatus("Unsaved changes — click Save or Publish.");
    });

    card.querySelector(".reflow-pn-editor-toolbar").addEventListener("click", function (e) {
      var b = e.target.closest("button");
      if (!b) return;
      var cmd = b.getAttribute("data-cmd");
      var block = b.getAttribute("data-block");
      var act = b.getAttribute("data-act");
      if (cmd) {
        visual.focus();
        try { document.execCommand(cmd, false, null); } catch (_) {}
        return;
      }
      if (block) {
        visual.focus();
        try { document.execCommand("formatBlock", false, block); } catch (_) {}
        return;
      }
      if (act === "link") {
        var url = window.prompt("Link address (URL):", "https://");
        if (url) { visual.focus(); try { document.execCommand("createLink", false, url); } catch (_) {} }
        return;
      }
      if (act === "images") return toggleAlt();
      if (act === "toggle-source") return setSourceMode(!sourceMode);
      if (act === "save") return save();
      if (act === "revert") return revert();
      if (act === "publish") return publishFromEditor();
    });

    async function save() {
      setStatus("Saving…");
      try {
        var r = await fetch(ORIGIN + "/canvas/panorama/edit/" + encodeURIComponent(jobId), {
          method: "PUT",
          credentials: "include",
          headers: csrfHeaders(),
          body: JSON.stringify({ html: getHtml() })
        });
        if (!r.ok) throw new Error("HTTP " + r.status);
        var data = await r.json();
        setStatus("Saved. Edited HTML is now the source of truth for every format.");
        stateEl.textContent = data.edited ? " (edited)" : "";
      } catch (e) { setStatus("Save failed: " + e.message, true); }
    }
    async function revert() {
      if (!confirm("Discard your edits and revert to the auto-generated HTML?")) return;
      setStatus("Reverting…");
      try {
        await fetch(ORIGIN + "/canvas/panorama/edit/" + encodeURIComponent(jobId), {
          method: "DELETE", credentials: "include", headers: csrfHeaders()
        });
        await loadInitial();
        setStatus("Reverted.");
        stateEl.textContent = "";
      } catch (e) { setStatus("Revert failed: " + e.message, true); }
    }
    async function publishFromEditor() {
      setStatus("Saving and publishing…");
      try {
        // Save the current edits first, so the published version includes them.
        var sv = await fetch(ORIGIN + "/canvas/panorama/edit/" + encodeURIComponent(jobId), {
          method: "PUT", credentials: "include", headers: csrfHeaders(),
          body: JSON.stringify({ html: getHtml() })
        });
        if (!sv.ok) throw new Error("save HTTP " + sv.status);
        var sd = await sv.json();
        stateEl.textContent = sd.edited ? " (edited)" : "";
        // Then publish — makes the accessible version visible to students and
        // publishes the Canvas Page.
        var pb = await fetch(ORIGIN + "/canvas/panorama/approve/" + encodeURIComponent(jobId), {
          method: "POST", credentials: "include", headers: csrfHeaders(),
          body: JSON.stringify({ comment: null })
        });
        if (!pb.ok) {
          var e = await pb.json().catch(function () { return {}; });
          var detail = e && e.detail;
          if (detail && typeof detail === "object") detail = detail.message || JSON.stringify(detail);
          throw new Error(detail || ("HTTP " + pb.status));
        }
        setStatus("Published — students can now see this accessible version.");
      } catch (e) { setStatus("Publish failed: " + (e && e.message ? e.message : e), true); }
    }

    async function loadInitial() {
      setStatus("Loading…");
      try {
        var r = await fetch(ORIGIN + "/canvas/panorama/edit/" + encodeURIComponent(jobId),
                            { credentials: "include", headers: BACKEND_HEADERS });
        var data = await r.json();
        setHtml(data.html || "");
        stateEl.textContent = data.edited ? " (edited)" : "";
        if (sourceMode) refreshPreview();
        if (altOpen) buildAltPanel();
        setStatus(data.edited ? "Loaded your saved edit." : "Loaded auto-generated HTML.");
      } catch (e) {
        setStatus("Could not load HTML: " + e.message, true);
      }
    }

    loadInitial();
  }

  function getSelection(ta) {
    return { start: ta.selectionStart, end: ta.selectionEnd, text: ta.value.slice(ta.selectionStart, ta.selectionEnd) };
  }
  function setSelection(ta, start, end, repl) {
    ta.value = ta.value.slice(0, start) + repl + ta.value.slice(end);
    ta.focus();
  }

  function esc(s) {
    return String(s || "").replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[c];
    });
  }

  function injectStyles() {
    if (document.getElementById("reflow-pn-styles")) return;
    var s = document.createElement("style");
    s.id = "reflow-pn-styles";
    s.textContent = [
      // Row dial
      ".reflow-pn-wrap{display:inline-flex;align-items:center;font-family:system-ui,-apple-system,Segoe UI,sans-serif;}",
      ".reflow-pn-wrap.reflow-pn-row{position:absolute;top:50%;right:0.75rem;transform:translateY(-50%);margin:0;z-index:5;pointer-events:none;}",
      ".reflow-pn-wrap.reflow-pn-row .reflow-pn-dial{pointer-events:auto;}",
      // Legacy Files page: dial anchored to the filename cell, floated to the
      // right edge of the cell so it sits in the cell's slack space without
      // pushing the next column.
      ".reflow-pn-wrap.reflow-pn-cell{position:absolute !important;top:50% !important;right:0.5rem !important;transform:translateY(-50%) !important;margin:0 !important;z-index:5 !important;pointer-events:none !important;display:inline-block !important;width:auto !important;}",
      ".reflow-pn-wrap.reflow-pn-cell .reflow-pn-dial{pointer-events:auto;}",
      // New Files UI: dial floats absolutely in the inter-column gutter
      // between Status and Actions so it doesn't displace the row's 3-dot
      // menu. Top:50% + translateY(-50%) keeps it on the same baseline as
      // every other icon in the row. Left is negative so the wrap escapes
      // the actions cell's left edge into the gap.
      ".reflow-pn-wrap.reflow-pn-actions{position:absolute !important;top:50% !important;left:0.5rem !important;transform:translate(-50%, -50%) !important;display:inline-flex !important;align-items:center !important;justify-content:center !important;margin:0 !important;z-index:5 !important;pointer-events:none !important;}",
      ".reflow-pn-wrap.reflow-pn-actions .reflow-pn-dial{pointer-events:auto;}",
      ".reflow-pn-dial{background:transparent;border:0;padding:0;margin:0;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;line-height:1;border-radius:50%;transition:transform 120ms,box-shadow 120ms;vertical-align:middle;}",
      ".reflow-pn-dial:hover{transform:scale(1.08);box-shadow:0 0 0 4px rgba(0,0,0,0.05);}",
      ".reflow-pn-dial:focus{outline:none;box-shadow:0 0 0 3px rgba(10,95,181,0.4);}",
      // ---- Alternative Formats Menu ------------------------------------
      // Canvas's own type (Lato) so the menu reads as part of the page; one
      // accent, an iris violet, for the actions and focus; status colors
      // only where they carry status. Every text color is at least 4.5:1
      // on its background, and the tones pair a word with the color.
      ".reflow-pn-modal{--pn-ink:#1c2733;--pn-slate:#4b5a6a;--pn-rule:#d9dfe5;--pn-wash:#f4f6f8;--pn-iris:#4c3fa0;--pn-iris-dark:#3b3082;--pn-iris-wash:#efedf9;--pn-good:#1b6e45;--pn-good-wash:#e6f2ec;--pn-caution:#8a5200;--pn-caution-wash:#fdf1dc;--pn-problem:#b42318;--pn-problem-wash:#fcebe9;" +
        "position:fixed;inset:0;background:rgba(28,39,51,0.55);display:flex;align-items:center;justify-content:center;padding:1rem;box-sizing:border-box;z-index:99999;" +
        "font:400 0.875rem/1.5 'Lato Extended',Lato,'Helvetica Neue',Helvetica,Arial,sans-serif;color:var(--pn-ink);}",
      ".reflow-pn-modal *,.reflow-pn-modal *::before,.reflow-pn-modal *::after{box-sizing:border-box;}",
      ".reflow-pn-modal-card{background:#fff;width:min(46rem,100%);max-height:calc(100vh - 2rem);border-radius:10px;box-shadow:0 20px 56px rgba(28,39,51,0.3);display:flex;flex-direction:column;overflow:hidden;}",
      // The header and footer set their whole box: they sit in Canvas's
      // page, whose own CSS can reach them.
      ".reflow-pn-modal-header{flex:0 0 auto;position:static;display:flex;align-items:flex-start;justify-content:space-between;gap:1rem;height:auto;min-height:0;margin:0;padding:1.25rem 1.5rem 1rem;background:#fff;border:0;border-bottom:1px solid var(--pn-rule);box-shadow:none;}",
      ".reflow-pn-modal-titles{min-width:0;}",
      ".reflow-pn-modal-titles h2{margin:0;font-size:1.125rem;line-height:1.35;font-weight:700;color:var(--pn-ink);overflow-wrap:anywhere;}",
      ".reflow-pn-modal-titles p{margin:0.15rem 0 0;color:var(--pn-slate);font-size:0.8125rem;}",
      ".reflow-pn-edited{display:inline-block;margin-left:0.5rem;padding:0 0.45rem;border-radius:4px;background:var(--pn-iris-wash);color:var(--pn-iris);font-size:0.75rem;font-weight:700;}",
      ".reflow-pn-modal-close{flex:0 0 auto;display:inline-flex;align-items:center;justify-content:center;width:2.25rem;height:2.25rem;margin:-0.35rem -0.5rem 0 0;border:0;border-radius:6px;background:transparent;color:var(--pn-slate);cursor:pointer;}",
      ".reflow-pn-modal-close:hover{background:var(--pn-wash);color:var(--pn-ink);}",
      ".reflow-pn-modal :focus-visible{outline:2px solid var(--pn-iris);outline-offset:2px;}",
      ".reflow-pn-modal-body{flex:1 1 auto;min-height:0;overflow:auto;}",
      ".reflow-pn-modal a.reflow-pn-format,.reflow-pn-modal a.reflow-pn-format:hover,.reflow-pn-modal a.reflow-pn-action:hover,.reflow-pn-modal a.reflow-pn-student-primary:hover{text-decoration:none;}",
      ".reflow-pn-section-title{margin:0 0 0.4rem;font-size:0.9375rem;line-height:1.3;font-weight:700;color:var(--pn-ink);}",
      // Notices (authorize, accept the disclaimer)
      ".reflow-pn-notice{display:flex;align-items:center;gap:1rem;padding:0.85rem 1.5rem;background:var(--pn-caution-wash);border-bottom:1px solid var(--pn-rule);}",
      ".reflow-pn-notice p{margin:0;flex:1;color:var(--pn-ink);}",
      // Status row
      ".reflow-pn-approval-bar{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:0.75rem 1rem;padding:0.85rem 1.5rem;background:var(--pn-wash);border-bottom:1px solid var(--pn-rule);}",
      ".reflow-pn-approval-left{display:flex;align-items:baseline;gap:0.6rem;flex:1 1 18rem;min-width:0;}",
      ".reflow-pn-approval-msg{color:var(--pn-slate);}",
      ".reflow-pn-approval-actions{display:flex;flex-wrap:wrap;gap:0.5rem;}",
      ".reflow-pn-status-badge{flex:0 0 auto;display:inline-flex;align-items:center;gap:0.4rem;font-weight:700;color:var(--pn-ink);}",
      ".reflow-pn-status-badge::before{content:'';width:0.6rem;height:0.6rem;border-radius:50%;background:currentColor;}",
      ".reflow-pn-tone--good{color:var(--pn-good);}",
      ".reflow-pn-tone--caution{color:var(--pn-caution);}",
      ".reflow-pn-tone--problem{color:var(--pn-problem);}",
      ".reflow-pn-tone--muted{color:var(--pn-slate);}",
      ".reflow-pn-pii-bar{flex-direction:column;align-items:stretch;}",
      ".reflow-pn-pii-controls{display:flex;flex-direction:column;gap:0.4rem;}",
      ".reflow-pn-pii-label{font-weight:700;font-size:0.8125rem;}",
      ".reflow-pn-pii-justification{width:100%;font:inherit;padding:0.45rem 0.55rem;border:1px solid #8a96a3;border-radius:6px;resize:vertical;}",
      // Buttons. Specific enough to stand apart from the editor's
      // !important button rules, which share the old class names.
      ".reflow-pn-modal .reflow-pn-action{display:inline-flex;align-items:center;justify-content:center;min-height:2.25rem;padding:0.4rem 0.9rem;border:1px solid #8a96a3;border-radius:6px;background:#fff;color:var(--pn-ink);font:inherit;font-weight:700;text-decoration:none;cursor:pointer;white-space:nowrap;}",
      ".reflow-pn-modal .reflow-pn-action:hover{background:var(--pn-wash);border-color:var(--pn-slate);}",
      ".reflow-pn-modal .reflow-pn-action--primary{background:var(--pn-iris);border-color:var(--pn-iris);color:#fff;}",
      ".reflow-pn-modal .reflow-pn-action--primary:hover{background:var(--pn-iris-dark);border-color:var(--pn-iris-dark);}",
      ".reflow-pn-modal .reflow-pn-action:disabled{opacity:0.6;cursor:wait;}",
      // The two checks, side by side: what students get, then what was
      // uploaded.
      ".reflow-pn-checks{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));border-bottom:1px solid var(--pn-rule);}",
      ".reflow-pn-check{padding:1.1rem 1.5rem 1.25rem;}",
      ".reflow-pn-check--original{border-left:1px solid var(--pn-rule);}",
      ".reflow-pn-check-verdict{margin:0 0 0.35rem;font-size:1rem;font-weight:700;line-height:1.35;}",
      ".reflow-pn-check-verdict.is-good{color:var(--pn-good);}",
      ".reflow-pn-check-verdict.is-caution{color:var(--pn-caution);}",
      ".reflow-pn-check-verdict.is-problem{color:var(--pn-problem);}",
      ".reflow-pn-check-verdict.is-muted{color:var(--pn-slate);}",
      ".reflow-pn-check-lead{margin:0 0 0.5rem;color:var(--pn-ink);}",
      ".reflow-pn-check-items{list-style:none;margin:0 0 0.6rem;padding:0;display:flex;flex-direction:column;gap:0.3rem;}",
      ".reflow-pn-check-items--plain li{position:relative;padding-left:0.9rem;}",
      ".reflow-pn-check-items--plain li::before{content:'';position:absolute;left:0;top:0.55em;width:0.35rem;height:0.35rem;border-radius:50%;background:var(--pn-problem);}",
      ".reflow-pn-sev{display:inline-block;min-width:3.6rem;margin-right:0.25rem;padding:0 0.35rem;border-radius:4px;font-size:0.75rem;font-weight:700;text-align:center;}",
      ".reflow-pn-sev--error{background:var(--pn-problem-wash);color:var(--pn-problem);}",
      ".reflow-pn-sev--warning{background:var(--pn-caution-wash);color:var(--pn-caution);}",
      ".reflow-pn-sev--info{background:var(--pn-iris-wash);color:var(--pn-iris);}",
      ".reflow-pn-check-note{margin:0 0 0.75rem;color:var(--pn-slate);font-size:0.8125rem;}",
      ".reflow-pn-check-note a{color:var(--pn-iris);}",
      ".reflow-pn-check-actions{display:flex;flex-wrap:wrap;gap:0.5rem;}",
      ".reflow-pn-vrules{margin-top:0.25rem;}",
      ".reflow-pn-vrules > summary{cursor:pointer;color:var(--pn-iris);font-weight:700;width:max-content;max-width:100%;}",
      ".reflow-pn-vrule-list{list-style:none;margin:0.6rem 0;padding:0;display:flex;flex-direction:column;gap:0.55rem;}",
      ".reflow-pn-vrule-label{display:block;font-weight:700;}",
      ".reflow-pn-vrule-desc{display:block;color:var(--pn-slate);font-size:0.8125rem;}",
      // Formats
      ".reflow-pn-formats{padding:1.1rem 1.5rem 1.25rem;}",
      ".reflow-pn-modal-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0.75rem 1.5rem;}",
      ".reflow-pn-fmt-group-label{margin:0 0 0.25rem;font-size:0.8125rem;font-weight:700;color:var(--pn-slate);}",
      ".reflow-pn-fmt-list{list-style:none;margin:0;padding:0;}",
      ".reflow-pn-format{display:grid;grid-template-columns:1fr auto;gap:0 0.75rem;margin:0 -0.6rem;padding:0.5rem 0.6rem;border-radius:6px;color:var(--pn-ink);text-decoration:none;}",
      ".reflow-pn-format:hover{background:var(--pn-iris-wash);}",
      ".reflow-pn-format-name{font-weight:700;color:var(--pn-iris);text-decoration:underline;text-decoration-thickness:1px;text-underline-offset:0.18em;}",
      ".reflow-pn-format-type{color:var(--pn-slate);font-size:0.75rem;align-self:center;white-space:nowrap;}",
      ".reflow-pn-format-desc{grid-column:1 / -1;color:var(--pn-slate);font-size:0.8125rem;}",
      ".reflow-pn-format.is-busy{cursor:progress;}",
      ".reflow-pn-fmt-later{grid-column:1 / -1;margin:0.25rem 0 0;color:var(--pn-slate);font-size:0.8125rem;}",
      ".reflow-pn-fmt-status{grid-column:1 / -1;margin:0;min-height:1.25em;font-weight:700;color:var(--pn-ink);}",
      ".reflow-pn-fmt-status:empty{display:none;}",
      // Students: the accessible version first
      ".reflow-pn-student-cta{padding:1.25rem 1.5rem 0.25rem;}",
      ".reflow-pn-student-primary{display:flex;align-items:baseline;justify-content:space-between;gap:1rem;padding:0.85rem 1.1rem;border-radius:8px;background:var(--pn-iris);color:#fff;text-decoration:none;}",
      ".reflow-pn-student-primary:hover{background:var(--pn-iris-dark);}",
      ".reflow-pn-student-primary-text{font-size:1rem;font-weight:700;}",
      ".reflow-pn-student-primary-hint{font-size:0.8125rem;opacity:0.9;}",
      ".reflow-pn-student-note{margin:0.5rem 0 0;color:var(--pn-slate);font-size:0.8125rem;}",
      // Footer
      ".reflow-pn-modal-footer{flex:0 0 auto;position:static;height:auto;margin:0;padding:0.75rem 1.5rem;border:0;border-top:1px solid var(--pn-rule);background:var(--pn-wash);}",
      ".reflow-pn-footer-text{margin:0;color:var(--pn-slate);font-size:0.8125rem;}",
      ".reflow-pn-footer-text a{color:var(--pn-iris);}",
      "@media (max-width:40rem){.reflow-pn-checks,.reflow-pn-modal-grid{grid-template-columns:1fr;}.reflow-pn-check--original{border-left:0;border-top:1px solid var(--pn-rule);}.reflow-pn-modal{padding:0.5rem;}.reflow-pn-modal-card{max-height:calc(100vh - 1rem);}}",
      "@media (prefers-reduced-motion:reduce){.reflow-pn-modal *{transition:none !important;}}",
      // WCAG publication gate panel (only shown when the backend returns
      // a 409 with structured findings). Inline inside the approval bar
      // so faculty stays in flow.
      ".reflow-pn-wcag-gate{width:100%;padding:0.85rem 0 0;margin-top:0.6rem;border-top:1px solid #e7e9ec;display:flex;flex-direction:column;gap:0.6rem;}",
      ".reflow-pn-wcag-gate-title{margin:0;font-size:0.95rem;font-weight:700;color:#1d1d1d;}",
      ".reflow-pn-wcag-gate-subtitle{margin:0.25rem 0 0;font-size:0.85rem;font-weight:700;color:#1d1d1d;}",
      ".reflow-pn-wcag-gate-hint{margin:0;color:#5b6573;font-size:0.82rem;line-height:1.45;}",
      ".reflow-pn-wcag-checklist,.reflow-pn-wcag-findings{list-style:none;padding:0;margin:0;display:flex;flex-direction:column;gap:0.35rem;}",
      ".reflow-pn-wcag-checklist label,.reflow-pn-wcag-finding label{display:flex;align-items:flex-start;gap:0.55rem;padding:0.45rem 0.6rem;border:1px solid #e7e9ec;border-radius:6px;background:#fafbfc;cursor:pointer;font-size:0.86rem;line-height:1.35;}",
      ".reflow-pn-wcag-checklist label:hover,.reflow-pn-wcag-finding label:hover{border-color:#bfc4cb;background:#fff;}",
      ".reflow-pn-wcag-checklist input,.reflow-pn-wcag-finding input{margin-top:0.15rem;flex-shrink:0;}",
      ".reflow-pn-wcag-rule{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:0.78rem;font-weight:700;color:#b91c1c;background:#fee2e2;padding:0.05rem 0.4rem;border-radius:4px;flex-shrink:0;}",
      ".reflow-pn-wcag-msg{color:#1d1d1d;flex:1;}",
      ".reflow-pn-wcag-gate-actions{display:flex;gap:0.5rem;margin-top:0.3rem;}",
      ".reflow-pn-wcag-confirm,.reflow-pn-wcag-cancel{padding:0.5rem 0.95rem;border-radius:6px;border:1px solid transparent;cursor:pointer;font-weight:600;font-size:0.86rem;}",
      ".reflow-pn-wcag-confirm{background:#15803d;color:#fff;}",
      ".reflow-pn-wcag-confirm:hover{background:#166534;}",
      ".reflow-pn-wcag-cancel{background:#fff;border-color:#cdd3da;color:#1d1d1d;}",
      ".reflow-pn-wcag-cancel:hover{background:#f3f4f6;}",
      // Editor
      ".reflow-pn-editor{position:fixed;inset:0;background:rgba(15,20,30,0.6);display:flex;align-items:center;justify-content:center;z-index:100000;font:14px system-ui,sans-serif;}",
      ".reflow-pn-editor-card{background:#fff;width:min(72rem,95vw);height:min(86vh,52rem);border-radius:10px;display:flex;flex-direction:column;box-shadow:0 30px 80px rgba(0,0,0,0.3);overflow:hidden;}",
      ".reflow-pn-editor-card .reflow-pn-editor-head{display:flex;align-items:flex-start;justify-content:space-between;padding:1rem 1.25rem 0.5rem;border-bottom:1px solid #ececec;flex-wrap:wrap;}",
      ".reflow-pn-editor-card .reflow-pn-editor-head h2{margin:0;font-size:1.05rem;}",
      ".reflow-pn-editor-card .reflow-pn-editor-head p{margin:0.15rem 0 0;color:#666;font-size:0.85rem;}",
      // Stack the title + filename on the left so the filename doesn't float
      // in the middle of the header (flex space-between with 3 loose children).
      ".reflow-pn-editor-titles{display:flex;flex-direction:column;gap:0.1rem;min-width:0;flex:1;}",
      ".reflow-pn-editor-close{background:none;border:0;font-size:1.5rem;cursor:pointer;color:#666;line-height:1;padding:0;margin-left:1rem;}",
      ".reflow-pn-editor-toolbar{display:flex;align-items:center;gap:0.35rem;padding:0.55rem 1.25rem;border-bottom:1px solid #ececec;background:#fafafa;}",
      ".reflow-pn-editor-toolbar button{background:#fff;border:1px solid #d6d8db;padding:0.35rem 0.6rem;border-radius:5px;cursor:pointer;font-weight:600;color:#1d1d1d;}",
      ".reflow-pn-editor-toolbar button:hover{background:#f0f0f0;}",
      ".reflow-pn-editor-toolbar-sep{flex:0 0 auto;width:1px;height:1.4rem;background:#d6d8db;margin:0 0.5rem;}",
      ".reflow-pn-editor-spacer{flex:1;}",
      ".reflow-pn-btn-primary{background:#0a5fb5 !important;color:#fff !important;border-color:#0a5fb5 !important;}",
      ".reflow-pn-btn-primary:hover{background:#084a91 !important;}",
      ".reflow-pn-btn-secondary{color:#a00 !important;}",
      ".reflow-pn-btn-publish{background:#2e7d32 !important;color:#fff !important;border-color:#2e7d32 !important;}",
      ".reflow-pn-btn-publish:hover{background:#256527 !important;}",
      ".reflow-pn-editor-toggle.is-active{background:#e3edf7 !important;border-color:#0a5fb5 !important;color:#0a5fb5 !important;}",
      // Body is a flex row: the visual editor flexes to fill, the alt panel is
      // a fixed right rail when open, and the source/preview pane (a 2-col
      // grid) replaces the visual editor when "HTML" is toggled.
      ".reflow-pn-editor-body{display:flex;flex:1;min-height:0;}",
      ".reflow-pn-editor-visual{flex:1;min-width:0;overflow:auto;padding:1.5rem 1.75rem;background:#fff;color:#1d1d1d;font-family:Georgia,'Times New Roman',serif;font-size:1rem;line-height:1.65;outline:none;}",
      ".reflow-pn-editor-visual:focus{box-shadow:inset 0 0 0 2px rgba(10,95,181,0.18);}",
      ".reflow-pn-editor-visual h1,.reflow-pn-editor-visual h2,.reflow-pn-editor-visual h3{font-family:system-ui,-apple-system,Segoe UI,sans-serif;line-height:1.25;margin:1.1em 0 0.4em;}",
      ".reflow-pn-editor-visual p{margin:0 0 0.8em;}",
      ".reflow-pn-editor-visual img{max-width:100%;height:auto;}",
      ".reflow-pn-editor-visual table{border-collapse:collapse;width:100%;}",
      ".reflow-pn-editor-visual th,.reflow-pn-editor-visual td{border:1px solid #ccc;padding:0.4rem;}",
      // Flag images that still have no alt text right in the editing surface —
      // but not ones explicitly marked decorative (role=presentation).
      ".reflow-pn-editor-visual img:not([alt]):not([role='presentation']),.reflow-pn-editor-visual img[alt='']:not([role='presentation']){outline:2px dashed #cc7a00;outline-offset:2px;}",
      ".reflow-pn-editor-alt{flex:0 0 21rem;overflow:auto;border-left:1px solid #ececec;background:#fafafa;padding:1rem 1.1rem;}",
      ".reflow-pn-alt-head h3{margin:0 0 0.25rem;font-size:0.95rem;}",
      ".reflow-pn-alt-head p{margin:0 0 0.85rem;font-size:0.8rem;color:#5b6573;line-height:1.45;}",
      ".reflow-pn-alt-list{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:0.7rem;}",
      ".reflow-pn-alt-item{display:flex;gap:0.6rem;padding:0.55rem;border:1px solid #e6e6e6;border-radius:7px;background:#fff;}",
      ".reflow-pn-alt-item.is-missing{border-color:#cc7a00;background:#fff8ee;}",
      ".reflow-pn-alt-thumb{flex:0 0 3rem;width:3rem;height:3rem;object-fit:cover;border-radius:4px;background:#eee;border:1px solid #e0e0e0;}",
      ".reflow-pn-alt-fields{flex:1;min-width:0;display:flex;flex-direction:column;gap:0.3rem;}",
      ".reflow-pn-alt-fields label{font-size:0.78rem;font-weight:600;color:#1d1d1d;}",
      ".reflow-pn-alt-flag{color:#cc7a00;font-weight:700;text-transform:uppercase;font-size:0.66rem;letter-spacing:0.02em;}",
      ".reflow-pn-alt-input{width:100%;box-sizing:border-box;border:1px solid #d6d8db;border-radius:5px;padding:0.4rem;font:inherit;font-size:0.82rem;resize:vertical;}",
      ".reflow-pn-alt-input:focus{outline:none;border-color:#0a5fb5;box-shadow:0 0 0 2px rgba(10,95,181,0.18);}",
      ".reflow-pn-alt-input:disabled{background:#f0f0f0;color:#9aa0a6;cursor:not-allowed;}",
      ".reflow-pn-alt-item.is-decorative{border-color:#c9d3df;background:#f4f7fb;}",
      ".reflow-pn-alt-deco-tag{color:#5b6573;font-weight:700;text-transform:uppercase;font-size:0.66rem;letter-spacing:0.02em;}",
      ".reflow-pn-alt-deco{display:flex;align-items:center;gap:0.35rem;font-size:0.75rem;color:#5b6573;font-weight:500;cursor:pointer;}",
      ".reflow-pn-alt-deco input{margin:0;flex:0 0 auto;}",
      ".reflow-pn-editor-sourcewrap{flex:1;display:grid;grid-template-columns:1fr 1fr;min-height:0;}",
      // The class-level display rules above override the plain ``hidden``
      // attribute's display:none, which left all three panes visible at once.
      // This higher-specificity rule restores hiding for the toggled panes.
      ".reflow-pn-editor-visual[hidden],.reflow-pn-editor-alt[hidden],.reflow-pn-editor-sourcewrap[hidden]{display:none !important;}",
      ".reflow-pn-editor-source{box-sizing:border-box;width:100%;height:100%;border:0;padding:1rem 1.25rem;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:0.85rem;line-height:1.55;letter-spacing:0.01em;tab-size:2;white-space:pre-wrap;resize:none;background:#f8f9fa;color:#1d1d1d;outline:none;border-right:1px solid #ececec;}",
      ".reflow-pn-editor-preview{box-sizing:border-box;width:100%;height:100%;border:0;}",
      ".reflow-pn-editor-status{padding:0.55rem 1.25rem;border-top:1px solid #ececec;background:#fafafa;color:#444;font-size:0.83rem;}",
      ".reflow-pn-editor-status.is-error{color:#b00020;background:#fff0f0;}",
      // Language picker (openLanguagePicker). The overlay wrapper had no CSS,
      // so the picker rendered invisibly in normal flow behind the modal —
      // clicking Translate appeared to do nothing. These give it a centered,
      // on-top modal presentation matching the rest of the overlay.
      ".reflow-pn-lang-overlay{position:fixed;inset:0;background:rgba(15,20,30,0.55);display:flex;align-items:center;justify-content:center;z-index:100001;font:14px system-ui,sans-serif;color:#1d1d1d;}",
      ".reflow-pn-lang-card{width:min(36rem,92vw);max-height:80vh;overflow:auto;background:#fff;border-radius:10px;box-shadow:0 24px 64px rgba(0,0,0,0.3);display:flex;flex-direction:column;}",
      ".reflow-pn-lang-card .reflow-pn-lang-head{display:flex;align-items:center;justify-content:space-between;padding:1rem 1.25rem;border-bottom:1px solid #ececec;}",
      ".reflow-pn-lang-card .reflow-pn-lang-head h2{margin:0;font-size:1.05rem;}",
      ".reflow-pn-lang-close{background:none;border:0;font-size:1.5rem;line-height:1;cursor:pointer;color:#666;padding:0;}",
      ".reflow-pn-lang-list{list-style:none;margin:0;padding:0.75rem;display:grid;grid-template-columns:1fr 1fr;gap:0.4rem;}",
      ".reflow-pn-lang-list a{display:block;padding:0.55rem 0.75rem;border:1px solid #e6e6e6;border-radius:6px;text-decoration:none;color:#1d1d1d;background:#fff;}",
      ".reflow-pn-lang-list a:hover{background:#f3f6fb;border-color:#a4c3df;}",
      ".reflow-pn-lang-search{padding:0.75rem 1.5rem 0;}",
      ".reflow-pn-lang-search input{width:100%;padding:0.55rem 0.75rem;border:1px solid #d6d8db;border-radius:6px;font:inherit;}",
      ".reflow-pn-lang-grid{display:grid;grid-template-columns:1fr 1fr;gap:0.4rem;padding:0.85rem 1.5rem;max-height:22rem;overflow:auto;}",
      ".reflow-pn-lang-btn{display:flex;align-items:center;gap:0.6rem;padding:0.5rem 0.75rem;border:1px solid #e6e6e6;border-radius:6px;background:#fff;cursor:pointer;text-align:left;font:inherit;color:#1d1d1d;}",
      ".reflow-pn-lang-btn:hover{background:#f3f6fb;border-color:#a4c3df;}",
      ".reflow-pn-lang-code{font-weight:700;color:#0a5fb5;font-size:0.75rem;letter-spacing:0.04em;background:#eef4fc;padding:0.1rem 0.4rem;border-radius:4px;}",
      ".reflow-pn-lang-label{font-size:0.88rem;}",
      ".reflow-pn-lang-custom{display:flex;align-items:center;gap:0.6rem;padding:0.75rem 1.5rem;border-top:1px solid #ececec;background:#fafafa;}",
      ".reflow-pn-lang-custom label{flex:1;font-size:0.85rem;color:#444;display:flex;flex-direction:column;gap:0.25rem;}",
      ".reflow-pn-lang-custom input{padding:0.45rem 0.65rem;border:1px solid #d6d8db;border-radius:5px;font:inherit;}",
      ".reflow-pn-btn-primary{background:#2e7d32;color:#fff;border:none;padding:0.4rem 0.9rem;border-radius:5px;font-weight:600;cursor:pointer;font:inherit;}",
      ".reflow-pn-btn-primary:hover{background:#256527;}",
      ".reflow-pn-btn-primary:disabled{background:#9ec5a0;cursor:wait;}",
      ".reflow-pn-btn-secondary{background:#fff;color:#1a2233;border:1px solid #c8cfd9;padding:0.4rem 0.9rem;border-radius:5px;cursor:pointer;font:inherit;}",
      ".reflow-pn-btn-secondary:hover{background:#f4f7fb;border-color:#a4b0c1;}"
    ].join("\n");
    document.head.appendChild(s);
  }

  function _renderAuthBanner() {
    var o = STATE.oauth;
    if (!o || o.authorized) return "";
    if (!o.is_instructor && STATE.userRole !== "Instructor") return "";
    return '<section id="reflow-pn-auth-banner" class="reflow-pn-notice" role="status">' +
           '  <p>Connect Iris to your Canvas account so it can read your course files and publish accessible pages. New uploads aren’t converted until you do.</p>' +
           '  <button class="reflow-pn-action reflow-pn-action--primary reflow-pn-auth-btn" type="button">Authorize Iris</button>' +
           '</section>';
  }

  function _renderConsentBanner() {
    var c = STATE.consent || { agreed: false, reason: "unknown" };
    if (c.agreed) return "";
    // Don't nag students — only instructors trigger paid processing.
    if (STATE.userRole !== "Instructor") return "";
    var msg = c.reason === "version_changed"
      ? "The Iris disclaimer has changed. Accept it again to keep converting documents."
      : "Accept the Iris disclaimer before Iris converts documents in your courses.";
    return '<section id="reflow-pn-consent-banner" class="reflow-pn-notice" role="status">' +
           '  <p>' + esc(msg) + '</p>' +
           '  <a class="reflow-pn-action reflow-pn-action--primary" href="' + ORIGIN + '/canvas/consent" target="_blank" rel="noopener">Read and accept</a>' +
           '</section>';
  }

  function _consentAgreedText(c) {
    var when = fmtConsentDate(c.agreed_at);
    return 'You accepted the Iris disclaimer' + (when ? ' on ' + esc(when) : '') + '. ' +
           '<a href="' + ORIGIN + '/canvas/consent" target="_blank" rel="noopener">Read it again</a>';
  }

  // The footer line: instructors always know whether Iris may process
  // their documents. Informational, not nagging.
  function _renderConsentFooter() {
    var c = STATE.consent || { agreed: false, reason: "unknown" };
    var text = c.agreed
      ? _consentAgreedText(c)
      : 'Iris converts documents only after you accept its disclaimer. ' +
        '<a href="' + ORIGIN + '/canvas/consent" target="_blank" rel="noopener">Read the disclaimer</a>';
    return '<p class="reflow-pn-footer-text">' + text + '</p>';
  }

  // Resolve a job's Canvas Page reference into a full, openable URL.
  // Canvas's pages API returns ``url`` as a bare slug (e.g. "my-page"),
  // and older jobs stored exactly that. A bare slug rendered as an href
  // resolves relative to the current Canvas path -> ".../courses/50594/<slug>"
  // which 404s (the real path needs ".../pages/<slug>"). Accept either a
  // full URL (new jobs store html_url) or a slug and always produce a
  // correct absolute URL.
  function _canvasPageHref(payload) {
    var u = (payload.canvas_page_url || "").trim();
    if (!u) return "";
    if (/^https?:\/\//i.test(u)) return u;          // already a full URL
    var slug = u.replace(/^\/+/, "");
    if (slug.indexOf("pages/") === 0) slug = slug.slice(6);
    var course = encodeURIComponent(STATE.courseId || "");
    return location.origin + "/courses/" + course + "/pages/" + slug;
  }

  // Where the document stands, and what the instructor can do about it.
  // A status word with its tone, one sentence, and the actions. The
  // buttons carry ``data-action`` (see _wireApprovalActions).
  var _STATUS = {
    published:         { word: "Published",       tone: "good",    msg: "Students can open the accessible version and every format below." },
    awaiting_review:   { word: "Draft",           tone: "caution", msg: "Students can’t see it until you publish." },
    rejected:          { word: "Rejected",        tone: "problem", msg: "Hidden from students." },
    page_failed:       { word: "Draft",           tone: "caution", msg: "The Canvas page couldn’t be created. Authorize Iris again, then publish." },
    processing:        { word: "Converting",      tone: "muted",   msg: "Iris is converting this file. Check back in a few minutes." },
    processing_queued: { word: "Converting",      tone: "muted",   msg: "Waiting for Iris. Check back in a few minutes." },
    pii_scanning:      { word: "Converting",      tone: "muted",   msg: "Iris is converting this file. Check back in a few minutes." },
    failed:            { word: "Needs attention", tone: "problem", msg: "The conversion didn’t finish." },
    denied:            { word: "Needs attention", tone: "problem", msg: "Processing was denied." }
  };

  function _statusChip(word, tone) {
    return '<span class="reflow-pn-status-badge reflow-pn-tone--' + tone + '">' + esc(word) + '</span>';
  }

  function _renderApprovalBar(payload) {
    if (!payload.job_id) return "";
    var status = payload.job_status || "awaiting_review";
    var isInstructor = STATE.userRole === "Instructor";

    // Privacy (PII) review: Reflow paused the conversion pending a human
    // decision. Only documents Reflow converted reach this state.
    if (status === "awaiting_approval") {
      if (!isInstructor) {
        return '<section class="reflow-pn-approval-bar" data-status="awaiting_approval">' +
               '  <div class="reflow-pn-approval-left">' + _statusChip("Privacy review", "caution") +
               '    <span class="reflow-pn-approval-msg">Your instructor is reviewing this document.</span>' +
               '  </div>' +
               '</section>';
      }
      return '<section class="reflow-pn-approval-bar reflow-pn-pii-bar" data-status="awaiting_approval">' +
             '  <div class="reflow-pn-approval-left">' + _statusChip("Privacy review", "caution") +
             '    <span class="reflow-pn-approval-msg">Reflow flagged possible personal or sensitive information. Approve to finish the accessible formats, or deny to discard the document. Your decision is logged.</span>' +
             '  </div>' +
             '  <div class="reflow-pn-pii-controls">' +
             '    <label class="reflow-pn-pii-label" for="reflow-pn-pii-why">Reason for your decision</label>' +
             '    <textarea id="reflow-pn-pii-why" class="reflow-pn-pii-justification" rows="2" maxlength="1000">Reviewed by faculty in Canvas.</textarea>' +
             '    <div class="reflow-pn-approval-actions">' +
             '      <button class="reflow-pn-action reflow-pn-action--primary" type="button" data-action="pii_approve">Approve and continue</button>' +
             '      <button class="reflow-pn-action" type="button" data-action="pii_deny">Deny</button>' +
             '    </div>' +
             '  </div>' +
             '</section>';
    }

    var s = _STATUS[status] || _STATUS.awaiting_review;
    var msg = s.msg;
    if ((status === "failed" || status === "denied") && payload.error) msg = String(payload.error);
    var buttons = "";
    if (isInstructor) {
      if (status === "awaiting_review" || status === "rejected" || status === "page_failed") {
        buttons = '<button class="reflow-pn-action reflow-pn-action--primary reflow-pn-approve-btn" type="button" data-action="approve">Publish</button>' +
                  (status === "awaiting_review" ? '<button class="reflow-pn-action reflow-pn-reject-btn" type="button" data-action="reject">Reject</button>' : '');
      } else if (status === "published") {
        // Unpublish hides the formats and the Canvas page. ("Request edits"
        // hid only the formats and left the page up; Edit works on a
        // published document, so one way back to draft is enough.)
        buttons = '<button class="reflow-pn-action reflow-pn-unpublish-btn" type="button" data-action="unpublish">Unpublish</button>';
      }
    }
    return '<section class="reflow-pn-approval-bar" data-status="' + esc(status) + '">' +
           '  <div class="reflow-pn-approval-left">' + _statusChip(s.word, s.tone) +
           '    <span class="reflow-pn-approval-msg">' + esc(msg) + '</span>' +
           '  </div>' +
           '  <div class="reflow-pn-approval-actions">' + buttons + '</div>' +
           '</section>';
  }

  // Phase 13: apply focus management + keyboard handling so modals
  // meet WCAG 2.1 AA. Specifically:
  //  - 2.1.2 No Keyboard Trap (Escape always closes)
  //  - 2.4.3 Focus Order (Tab cycles through interactive elements
  //    inside the modal; Shift+Tab cycles backwards)
  //  - 2.4.7 Focus Visible (we don't suppress default focus rings)
  //  - 4.1.2 Name/Role/Value (modal has role+aria-modal+aria-label)
  function _wcagify_modal(overlay, modal, previouslyFocused) {
    if (modal.getAttribute("role") !== "dialog") {
      modal.setAttribute("role", "dialog");
    }
    modal.setAttribute("aria-modal", "true");
    if (!modal.getAttribute("aria-label") && !modal.getAttribute("aria-labelledby")) {
      modal.setAttribute("aria-label", "Alternative Formats Menu");
    }

    function focusableNodes() {
      return Array.prototype.slice.call(modal.querySelectorAll(
        'a[href], button:not([disabled]), input:not([disabled]), '
        + 'textarea:not([disabled]), select:not([disabled]), '
        + '[tabindex]:not([tabindex="-1"])'
      ));
    }
    var nodes = focusableNodes();
    if (nodes.length) {
      try { nodes[0].focus(); } catch (_) { /* defensive */ }
    }

    function onKey(e) {
      if (e.key === "Escape") {
        e.preventDefault();
        teardown();
        return;
      }
      if (e.key !== "Tab") return;
      var current = document.activeElement;
      var f = focusableNodes();
      if (!f.length) return;
      var first = f[0], last = f[f.length - 1];
      if (e.shiftKey && current === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && current === last) {
        e.preventDefault();
        first.focus();
      }
    }

    function teardown() {
      try { overlay.removeEventListener("keydown", onKey, true); } catch (_) {}
      try { if (overlay.parentNode) overlay.parentNode.removeChild(overlay); } catch (_) {}
      // Restore focus to whatever the user was on before opening.
      try {
        if (previouslyFocused && previouslyFocused.focus) {
          previouslyFocused.focus();
        }
      } catch (_) { /* element may have been removed */ }
    }

    overlay.addEventListener("keydown", onKey, true);

    // Wire the close button + backdrop click into the same teardown.
    var closer = modal.querySelector(".reflow-pn-modal-close");
    if (closer) {
      closer.addEventListener("click", function (e) { e.preventDefault(); teardown(); });
    }
    overlay.addEventListener("click", function (e) {
      if (e.target === overlay) teardown();
    });
  }

  // Replace the approval bar contents with the success state for the
  // given action. Pulled out so the multi-pass WCAG-gate flow can call
  // it after the resubmit succeeds.
  function _showApprovalSuccess(bar, action, payload) {
    _restatusDials(payload && payload.job_id, action);
    var done = {
      approve:       ["Published", "good", "Students can open the accessible version and every format now."],
      reject:        ["Rejected", "problem", "Hidden from students."],
      unpublish:     ["Draft", "caution", "Unpublished: hidden from students and from the course’s Pages. Publish again at any time."],
      request_edits: ["Draft", "caution", "Back to draft. Students can’t see the formats until you publish again."]
    }[action] || ["Draft", "caution", "Saved."];
    var pubActions = bar.querySelector(".reflow-pn-approval-actions");
    if (pubActions) pubActions.remove();
    var pubGate = bar.querySelector(".reflow-pn-wcag-gate");
    if (pubGate) pubGate.remove();
    var pubLeft = bar.querySelector(".reflow-pn-approval-left");
    if (pubLeft) {
      pubLeft.innerHTML = _statusChip(done[0], done[1]) +
        '<span class="reflow-pn-approval-msg">' + esc(done[2]) + '</span>';
      pubLeft.setAttribute("role", "status");
      // The button that was pressed is gone; the result takes focus, so a
      // screen reader reads it and the keyboard stays in the dialog.
      pubLeft.setAttribute("tabindex", "-1");
      try { pubLeft.focus(); } catch (_) { /* defensive */ }
    }
  }

  // Render the WCAG gate panel: a checklist of 4 manual review items
  // (always shown) + a list of automated WCAG error findings (each
  // waivable individually with a justification). Replaces the row of
  // approve/reject buttons with the panel. Calls ``onConfirm`` once
  // faculty has signed off and clicked Confirm and publish; the caller
  // resubmits the POST with ``gateState.waivers`` / ``gateState.checklist``
  // populated. The same gateState reference is mutated in place so a
  // multi-pass gate (e.g. unwaived findings AFTER unchecked items)
  // preserves the work the user already did.
  function _renderWcagGatePanel(bar, detail, gateState, onConfirm) {
    var actions = bar.querySelector(".reflow-pn-approval-actions");
    var existing = bar.querySelector(".reflow-pn-wcag-gate");
    if (existing) existing.remove();
    if (actions) actions.style.display = "none";

    var findings = (detail && detail.findings) || [];
    var checklistItems = [
      { id: "headings",      label: "Headings are correct (H1 down, no skipped levels)" },
      { id: "alt_text",      label: "Every figure has meaningful alt text" },
      { id: "tables",        label: "Tables have row + column headers where needed" },
      { id: "reading_order", label: "Reading order matches the visual order" }
    ];

    var html = '<div class="reflow-pn-wcag-gate" role="region" aria-label="Publication gate">' +
      '<h3 class="reflow-pn-wcag-gate-title">Before you publish</h3>' +
      '<p class="reflow-pn-wcag-gate-hint">Confirm you checked each of these. Anything the automated checks found that you can\u2019t fix in the editor can be waived with a reason.</p>' +
      '<ul class="reflow-pn-wcag-checklist">';
    checklistItems.forEach(function (item) {
      var checked = !!gateState.checklist[item.id];
      html += '<li><label><input type="checkbox" data-checklist="' + item.id + '"' +
              (checked ? " checked" : "") +
              '> ' + esc(item.label) + '</label></li>';
    });
    html += '</ul>';

    if (findings.length) {
      html += '<h4 class="reflow-pn-wcag-gate-subtitle">Automated WCAG checks found ' + findings.length + ' issue' + (findings.length === 1 ? "" : "s") + '</h4>' +
              '<p class="reflow-pn-wcag-gate-hint">Fix in the editor before publishing, OR check the box to waive (and tell us why — recorded for the audit trail).</p>' +
              '<ul class="reflow-pn-wcag-findings">';
      findings.forEach(function (f) {
        var alreadyWaived = gateState.waivers.indexOf(f.rule_id) >= 0;
        html += '<li class="reflow-pn-wcag-finding" data-rule="' + esc(f.rule_id) + '">' +
                '  <label>' +
                '    <input type="checkbox" data-waive="' + esc(f.rule_id) + '"' +
                       (alreadyWaived ? " checked" : "") + '>' +
                '    <span class="reflow-pn-wcag-rule">' + esc(f.rule_id) + '</span>' +
                '    <span class="reflow-pn-wcag-msg">' + esc(f.message || "") + '</span>' +
                '  </label>' +
                '</li>';
      });
      html += '</ul>';
    }

    html += '<div class="reflow-pn-wcag-gate-actions">' +
            '  <button class="reflow-pn-action reflow-pn-action--primary reflow-pn-wcag-confirm" type="button">Publish</button>' +
            '  <button class="reflow-pn-action reflow-pn-wcag-cancel" type="button">Cancel</button>' +
            '</div>' +
            '</div>';

    bar.insertAdjacentHTML("beforeend", html);
    var panel = bar.querySelector(".reflow-pn-wcag-gate");

    // Wire the checkboxes -> mutate gateState in place.
    panel.querySelectorAll('input[data-checklist]').forEach(function (cb) {
      cb.addEventListener("change", function () {
        gateState.checklist[cb.dataset.checklist] = !!cb.checked;
      });
    });
    panel.querySelectorAll('input[data-waive]').forEach(function (cb) {
      cb.addEventListener("change", function () {
        var rid = cb.dataset.waive;
        var i = gateState.waivers.indexOf(rid);
        if (cb.checked && i < 0) gateState.waivers.push(rid);
        if (!cb.checked && i >= 0) gateState.waivers.splice(i, 1);
      });
    });

    panel.querySelector(".reflow-pn-wcag-confirm").addEventListener("click", function () {
      // Guard against accidentally publishing without completing the
      // checklist — same guard the server enforces, but client-side so
      // faculty doesn't see a 409 they could have avoided.
      var unchecked = checklistItems.filter(function (it) { return !gateState.checklist[it.id]; });
      if (unchecked.length) {
        window.alert("Please confirm every item in the checklist before publishing.");
        return;
      }
      onConfirm();
    });
    panel.querySelector(".reflow-pn-wcag-cancel").addEventListener("click", function () {
      panel.remove();
      if (actions) actions.style.display = "";
      // Re-enable the approve button so faculty can try again or pivot.
      var ab = actions && actions.querySelector('button[data-action="approve"]');
      if (ab) { ab.disabled = false; ab.textContent = "Publish"; }
    });
  }

  // Wire up click handlers on the approval buttons. Called from inside
  // openFormatsModal after the modal is in the DOM.
  function _wireApprovalActions(modal, payload, refreshCallback) {
    var bar = modal.querySelector(".reflow-pn-approval-bar");
    if (!bar) return;
    bar.addEventListener("click", async function (e) {
      var btn = e.target.closest("button[data-action]");
      if (!btn) return;
      e.preventDefault();
      var action = btn.dataset.action;

      // PII privacy decision — distinct endpoint + payload (decision +
      // justification) from the publish approve/reject flow below.
      if (action === "pii_approve" || action === "pii_deny") {
        var decision = action === "pii_approve" ? "approved" : "denied";
        var ta = bar.querySelector(".reflow-pn-pii-justification");
        var justification = ((ta && ta.value) || "").trim();
        if (justification.length < 10) {
          if (ta) ta.focus();
          window.alert("Please enter a brief reason (at least 10 characters) for the privacy decision — it's recorded for the audit trail.");
          return;
        }
        if (decision === "denied" && !window.confirm(
              "Deny processing? Reflow will discard this document and generate no accessible formats.")) return;
        btn.disabled = true; btn.textContent = "Working…";
        try {
          var pr = await fetch(
            ORIGIN + "/canvas/panorama/pii-decision/" + encodeURIComponent(payload.job_id),
            {
              method: "POST",
              credentials: "include",
              headers: csrfHeaders(),
              body: JSON.stringify({ decision: decision, justification: justification })
            }
          );
          if (pr.status === 409) {
            // Not an error: the job already moved past the privacy gate (it
            // finished converting, or another tab/instructor acted). Show the
            // resolved state instead of a scary failure.
            var c409 = bar.querySelector(".reflow-pn-pii-controls");
            if (c409) c409.remove();
            var l409 = bar.querySelector(".reflow-pn-approval-left");
            if (l409) {
              l409.innerHTML = _statusChip("Done", "good") +
                '<span class="reflow-pn-approval-msg">This document already cleared the privacy review and finished converting. Close and reopen this menu to see the latest.</span>';
            }
            return;
          }
          if (!pr.ok) {
            var pt = await pr.text();
            throw new Error("HTTP " + pr.status + ": " + pt.slice(0, 200));
          }
          // Give clear in-place feedback. The canvas status only flips off
          // 'awaiting_approval' on the next bridge tick, so reopening the modal
          // with the (stale) payload would look like nothing happened.
          var controls = bar.querySelector(".reflow-pn-pii-controls");
          if (controls) controls.remove();
          var left = bar.querySelector(".reflow-pn-approval-left");
          if (left) {
            left.innerHTML =
              (decision === "approved" ? _statusChip("Approved", "good") : _statusChip("Denied", "problem")) +
              '<span class="reflow-pn-approval-msg">' +
              (decision === "approved"
                ? "Approved. Reflow is finishing the conversion — the accessible page will appear in a minute or two."
                : "Denied. Reflow will discard this document; no accessible formats will be generated.") +
              '</span>';
          }
        } catch (err) {
          btn.disabled = false;
          btn.textContent = decision === "approved" ? "Approve and continue" : "Deny";
          window.alert("Privacy decision failed: " + err.message);
        }
        return;
      }

      var endpoint = { approve: "approve", reject: "reject", request_edits: "request-edits", unpublish: "unpublish" }[action];
      if (!endpoint) return;
      var prompt = action === "reject" ? "Reject this accessible version? Students won\u2019t see it."
                 : action === "request_edits" ? "Move this back to draft? Students won\u2019t see the formats until you publish again."
                 : action === "unpublish" ? "Unpublish this accessible version? Students won\u2019t see it, and its Canvas page is unpublished. You can publish it again at any time."
                 : null;
      if (prompt && !window.confirm(prompt)) return;
      btn.disabled = true; btn.textContent = "Working…";

      // Persist gate state across the (potentially) multi-pass approve
      // flow. The server runs WCAG + checklist gates; we may need to
      // re-POST with waivers + checklist filled in. ``reject``,
      // ``request_edits`` and ``unpublish`` ignore both fields.
      var gateState = {
        waivers: [],
        checklist: { headings: false, alt_text: false, tables: false, reading_order: false }
      };

      async function _sendApprovalRequest() {
        return await fetch(
          ORIGIN + "/canvas/panorama/" + endpoint + "/" + encodeURIComponent(payload.job_id),
          {
            method: "POST",
            credentials: "include",
            headers: csrfHeaders(),
            body: JSON.stringify({
              comment: null,
              waivers: gateState.waivers,
              checklist: gateState.checklist
            })
          }
        );
      }

      try {
        var r = await _sendApprovalRequest();

        // Gate handling: when REQUIRE_WCAG_GATE=true the backend returns
        // structured 409s for the two gate failures (unwaived WCAG errors
        // or an incomplete reviewer checklist). Swap the approval bar
        // for an inline gate panel so faculty can act on the findings
        // and resubmit without leaving the modal.
        if (r.status === 409 && action === "approve") {
          var gatePayload = await r.json().catch(function () { return {}; });
          var detail = gatePayload && gatePayload.detail;
          if (detail && typeof detail === "object" &&
              (detail.error === "wcag_gate_blocked" || detail.error === "checklist_incomplete")) {
            _renderWcagGatePanel(bar, detail, gateState, async function () {
              // Resubmit with the populated gate state. Same handler;
              // success path below runs through the normal pubBadge code.
              var rr = await _sendApprovalRequest();
              if (rr.status === 409) {
                // Still gated (e.g. faculty waived some findings but
                // missed the checklist). Re-render with the new detail.
                var d2 = await rr.json().catch(function () { return {}; });
                _renderWcagGatePanel(bar, d2.detail || {}, gateState, arguments.callee);
                return;
              }
              if (!rr.ok) {
                var t2 = await rr.text();
                window.alert("Action failed: HTTP " + rr.status + ": " + t2.slice(0, 200));
                return;
              }
              _showApprovalSuccess(bar, "approve", payload);
            });
            return;
          }
        }

        if (!r.ok) {
          var txt = await r.text();
          throw new Error("HTTP " + r.status + ": " + txt.slice(0, 200));
        }
        _showApprovalSuccess(bar, action, payload);
      } catch (err) {
        btn.disabled = false;
        btn.textContent = action === "approve" ? "Publish"
                        : action === "reject"  ? "Reject"
                        : action === "unpublish" ? "Unpublish"
                                               : "Move back to draft";
        window.alert("Action failed: " + (err && err.message ? err.message : err));
      }
    });
  }

  // Language picker for the Translate format card. Presents a small
  // overlay with common languages; selecting one navigates to
  // ``/canvas/panorama/alt/{job_id}/translate?lang=<code>``.
  function openLanguagePicker(payload) {
    // Full language list (defined once at the top of the bundle). The AI
    // translator also accepts any language name, so a free-text field below
    // covers everything not in the list.
    var langs = TRANSLATE_LANGUAGES;
    function tUrl(code) {
      var preview = (payload.job_status !== "published" && STATE.userRole === "Instructor")
        ? "?preview=1" : "";
      // Backend matches the colon form ``/alt/{job}/translate:<lang>`` (see
      // ``fmt.startswith("translate:")``), not a ``?lang=`` query param.
      return ORIGIN + "/canvas/panorama/alt/" + encodeURIComponent(payload.job_id || "")
           + "/translate:" + encodeURIComponent(code) + preview;
    }
    var ov = document.createElement("div");
    ov.className = "reflow-pn-overlay reflow-pn-lang-overlay";
    ov.setAttribute("role", "dialog");
    ov.setAttribute("aria-label", "Choose a translation language");
    ov.innerHTML =
      '<div class="reflow-pn-lang-card">' +
      '  <div class="reflow-pn-lang-head"><h2>Translate to…</h2>' +
      '    <button class="reflow-pn-lang-close" type="button" aria-label="Close">&times;</button>' +
      '  </div>' +
      '  <ul class="reflow-pn-lang-list"></ul>' +
      '  <div class="reflow-pn-lang-custom">' +
      '    <label>Other language' +
      '      <input type="text" class="reflow-pn-lang-other" placeholder="e.g. Swahili, Hmong, Punjabi…" />' +
      '    </label>' +
      '    <button type="button" class="reflow-pn-lang-go reflow-pn-btn-primary">Translate</button>' +
      '  </div>' +
      '</div>';
    var ul = ov.querySelector(".reflow-pn-lang-list");
    langs.forEach(function (lang) {
      var li = document.createElement("li");
      var a = document.createElement("a");
      a.href = tUrl(lang.code);
      a.target = "_blank";
      a.rel = "noopener";
      a.textContent = lang.label;
      li.appendChild(a);
      ul.appendChild(li);
    });
    // Free-text fallback for any language not listed.
    var other = ov.querySelector(".reflow-pn-lang-other");
    var go = ov.querySelector(".reflow-pn-lang-go");
    function goOther() {
      var v = (other && other.value || "").trim();
      if (!v) { if (other) other.focus(); return; }
      window.open(tUrl(v), "_blank", "noopener");
    }
    if (go) go.addEventListener("click", goOther);
    if (other) other.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); goOther(); }
    });
    ov.querySelector(".reflow-pn-lang-close").addEventListener("click", function () {
      if (ov.parentNode) ov.parentNode.removeChild(ov);
    });
    ov.addEventListener("click", function (e) {
      if (e.target === ov && ov.parentNode) ov.parentNode.removeChild(ov);
    });
    document.body.appendChild(ov);
  }

  // -------------------------------------------------------------------
  // Bootstrap
  // -------------------------------------------------------------------
  //
  // Canvas is a single-page app at runtime: clicking a course nav swaps
  // the main panel without a full reload. We scan once at
  // DOMContentLoaded and again on every coalesced DOM mutation.

  async function decorateAllPdfs() {
    if (!STATE.courseId) return;

    // Three Canvas surfaces, three lookup keys.
    // - Files page: scan ``data-testid="table-row"`` rows; matched by filename
    //   text inside the row spans. Score is keyed by filename via
    //   ``loadScoresByFilename`` (cached in STATE.scoresByFilename).
    // - Module items: similar, filename-based.
    // - Anchor links (Pages, Discussions, embedded etc.): the href carries
    //   ``/files/<id>`` which is the numeric Canvas file_id. Score is keyed
    //   by file_id via ``fetchScoresByFileId``.
    //
    // We fire both lookups in parallel to keep paint snappy. Per-surface
    // results are independent: even if one lookup fails, the others
    // still decorate.
    var rows = findFileRows();
    var moduleItems = findModuleFiles();
    var anchors = findFileLinkAnchors();
    // Accessible-version links (the tool endpoint) inside a published Canvas
    // Page. These are keyed by job id and show the OUTPUT (WCAG) score.
    var accLinks = findAccessibleLinks();

    // Claim everything found before waiting on the network. A scan used to
    // mark an element only once its scores arrived, so a second scan
    // started meanwhile (any change to the page schedules one) found the
    // same link again and both added a dial: three dials on one link.
    // What ends up without a dial is released at the end, so a later scan
    // can try it again, as before.
    var claimed = []
      .concat(rows.map(function (e) { return e.row; }))
      .concat(moduleItems.map(function (e) { return e.item; }))
      .concat(anchors.map(function (e) { return e.anchor; }))
      .concat(accLinks.map(function (e) { return e.anchor; }));
    claimed.forEach(function (el) { el.dataset.reflowDecorated = "pending"; });
    try {
      await _decorateFound(rows, moduleItems, anchors, accLinks);
    } finally {
      claimed.forEach(function (el) {
        if (el.dataset.reflowDecorated === "pending") delete el.dataset.reflowDecorated;
      });
    }
  }

  async function _decorateFound(rows, moduleItems, anchors, accLinks) {

    var byNamePromise = (rows.length || moduleItems.length)
      ? loadScoresByFilename()
      : Promise.resolve({});
    var byIdPromise = anchors.length
      ? fetchScoresByFileId(anchors.map(function (a) { return a.fileId; }))
      : Promise.resolve({});
    var byJobPromise = accLinks.length
      ? fetchScoresByJobId(accLinks.map(function (a) { return a.jobId; }))
      : Promise.resolve({});

    var byName = await byNamePromise;
    var byId = await byIdPromise;
    var byJob = await byJobPromise;

    // Walk up from ``node`` looking for an ancestor that can host an
    // absolute-positioned child. <tr> is unreliable across browsers
    // (most engines treat ``position:relative`` on it as static under the
    // default ``border-collapse:collapse``), so we prefer real cell
    // elements: <td>, <th>, ARIA ``role="cell"`` divs, and Canvas's own
    // ``[data-testid*="cell"]`` divs. Stops at ``stopAt`` (typically the
    // row element) so we never escape upwards into the whole table.
    function findPositioningCell(node, stopAt) {
      var cur = node;
      while (cur && cur !== document.body) {
        if (stopAt && cur === stopAt) break;
        var tag = cur.tagName;
        if (tag === "TD" || tag === "TH") return cur;
        var role = cur.getAttribute && cur.getAttribute("role");
        if (role === "cell" || role === "gridcell") return cur;
        var testid = cur.dataset && cur.dataset.testid;
        if (testid && /cell/i.test(testid)) return cur;
        cur = cur.parentElement;
      }
      return stopAt || null;
    }

    function attachAfter(target, payload, filename, opts) {
      if (!opts && !shouldDecorate(payload)) return;
      var btn = makeDial(payload, filename, opts);
      if (target.parentNode) {
        target.parentNode.insertBefore(btn, target.nextSibling);
      }
    }

    // Small dial-shaped marker for files the watcher hasn't seen yet.
    // Reflow's watcher polls every ~60s; this surfaces that wait so
    // faculty doesn't refresh repeatedly thinking we missed the file.
    // Same actions-cell placement as the real dial for visual
    // consistency.
    function makePendingMarker(filename) {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "reflow-pn-dial reflow-pn-pending";
      btn.setAttribute("aria-label",
        "Awaiting accessibility scan for " + filename +
        ". Iris checks new uploads about once a minute."
      );
      btn.title =
        "Awaiting accessibility scan — Iris processes new uploads " +
        "automatically (about once a minute). Refresh in a few minutes " +
        "to see results.";
      btn.style.cssText =
        "background:#fff7e6;border:1px solid #cc7a00;color:#6b3a00;" +
        "border-radius:999px;width:52px;height:52px;padding:0;" +
        "display:inline-flex;align-items:center;justify-content:center;" +
        "font-size:18px;cursor:default;";
      btn.innerHTML = '<span aria-hidden="true">⧗</span>';  // hourglass
      // The marker is informational, not interactive — keep clicks
      // from bubbling to the row's row-click handler (which would
      // open the file preview).
      btn.addEventListener("click", function (e) { e.stopPropagation(); e.preventDefault(); });
      return btn;
    }

    function attachPendingMarker(entry) {
      var marker = makePendingMarker(entry.filename);
      var wrap = document.createElement("span");
      wrap.appendChild(marker);
      var actionsCell = entry.row.querySelector('[data-testid="table-cell-actions"]');
      if (actionsCell) {
        wrap.className = "reflow-pn-wrap reflow-pn-actions";
        try {
          if (getComputedStyle(actionsCell).position === "static") {
            actionsCell.style.position = "relative";
          }
          actionsCell.style.overflow = "visible";
        } catch (e) { /* defensive */ }
        actionsCell.appendChild(wrap);
        return;
      }
      var cell = findPositioningCell(entry.filenameSpan || entry.row, entry.row);
      if (cell) {
        wrap.className = "reflow-pn-wrap reflow-pn-cell";
        try {
          if (getComputedStyle(cell).position === "static") {
            cell.style.position = "relative";
          }
        } catch (e) { /* defensive */ }
        cell.appendChild(wrap);
      }
    }

    rows.forEach(function (entry) {
      var payload = byName[entry.filename];
      // No payload AND the user is an instructor: this is a new file
      // the watcher hasn't picked up yet, or one not yet submitted to
      // Reflow. Mark it as "Pending scan" so faculty doesn't think
      // we're ignoring it. Students see nothing in this state — there
      // are no formats for them to act on yet.
      if (!payload && STATE.userRole === "Instructor") {
        entry.row.dataset.reflowDecorated = "1";
        attachPendingMarker(entry);
        return;
      }
      if (!payload) return;
      entry.row.dataset.reflowDecorated = "1";
      if (!shouldDecorate(payload)) return;
      var btn = makeDial(payload, entry.filename);
      var wrap = document.createElement("span");
      wrap.appendChild(btn);
      // Prefer the dedicated actions column so the dial gets its own real
      // estate next to the row's 3-dot menu — keeps it away from the
      // UDOIT "Manage Alternates" dropdown that lives in the name cell.
      // Falls back to the name-cell positioning for surfaces without an
      // actions column (legacy Files page, anchor-only contexts).
      var actionsCell = entry.row.querySelector('[data-testid="table-cell-actions"]');
      if (actionsCell) {
        wrap.className = "reflow-pn-wrap reflow-pn-actions";
        try {
          // Anchor the dial inside the actions cell with absolute positioning
          // so we DON'T disturb the cell's native flex layout for the 3-dot
          // menu — pushing flex into the cell forced the menu off-center on
          // file rows but not folder rows. With absolute positioning the
          // dial floats into the inter-column gutter between Status and
          // Actions and the menu stays right where Canvas put it.
          if (getComputedStyle(actionsCell).position === "static") {
            actionsCell.style.position = "relative";
          }
          actionsCell.style.overflow = "visible";
        } catch (e) { /* defensive */ }
        actionsCell.appendChild(wrap);
        return;
      }
      var cell = findPositioningCell(entry.filenameSpan || entry.row, entry.row);
      if (!cell) return;
      wrap.className = "reflow-pn-wrap reflow-pn-cell";
      try {
        if (getComputedStyle(cell).position === "static") {
          cell.style.position = "relative";
        }
      } catch (e) { /* defensive */ }
      cell.appendChild(wrap);
    });

    moduleItems.forEach(function (entry) {
      var payload = byName[entry.filename];
      if (!payload) return;
      entry.item.dataset.reflowDecorated = "1";
      var anchor = entry.item.querySelector("a") || entry.item.querySelector("span") || entry.item;
      attachAfter(anchor, payload, entry.filename);
    });

    anchors.forEach(function (entry) {
      var payload = byId[entry.fileId];
      if (!shouldDecorate(payload)) return;
      entry.anchor.dataset.reflowDecorated = "1";
      var filename = cleanFilename(entry.anchor.textContent || "Document");
      var cell = findPositioningCell(entry.anchor);
      if (cell) {
        var btn = makeDial(payload, filename);
        var wrap = document.createElement("span");
        wrap.className = "reflow-pn-wrap reflow-pn-cell";
        wrap.appendChild(btn);
        try {
          if (getComputedStyle(cell).position === "static") {
            cell.style.position = "relative";
          }
        } catch (e) { /* defensive */ }
        cell.appendChild(wrap);
        return;
      }
      // Pages / Discussions / arbitrary anchor contexts: no table column to
      // anchor against, fall back to inline-after.
      attachAfter(entry.anchor, payload, filename);
    });

    // Accessible-version links: always decorate (the link IS the accessible
    // output) with the WCAG "after" dial, for faculty and students alike.
    accLinks.forEach(function (entry) {
      var payload = byJob[entry.jobId];
      if (!payload) return;
      entry.anchor.dataset.reflowDecorated = "1";
      var filename = cleanFilename(entry.anchor.textContent || "Document");
      attachAfter(entry.anchor, payload, filename, { accessible: true });
    });
  }

  function scheduleScan() {
    if (scheduleScan._pending) return;
    scheduleScan._pending = true;
    // Coalesce burst mutations into a single scan ~250ms later, and never
    // run two at once: a change during a scan asks for one more after it.
    setTimeout(function () {
      scheduleScan._pending = false;
      if (scheduleScan._running) { scheduleScan._again = true; return; }
      scheduleScan._running = true;
      decorateAllPdfs().catch(function (e) {
        console.warn("Iris overlay scan failed:", e);
      }).then(function () {
        scheduleScan._running = false;
        if (scheduleScan._again) { scheduleScan._again = false; scheduleScan(); }
      });
    }, 250);
  }

  function init() {
    if (init._started) return;
    init._started = true;
    try { injectStyles(); } catch (e) { console.warn("Iris overlay: injectStyles failed", e); }
    try { getEnv(); } catch (e) { console.warn("Iris overlay: getEnv failed", e); }
    // Fetch the CSRF token in the background. The first scheduled
    // scan can fire before the token arrives -- that's fine because
    // scans are read-only. State-changing actions check STATE.csrfToken
    // at submit time.
    loadCsrfToken().catch(function (e) {
      console.warn("Iris overlay: CSRF fetch failed", e);
    });
    // Check Canvas-OAuth authorization in the background so the modal can
    // surface an "Authorize Reflow" prompt to instructors who haven't yet
    // granted access. Read-only; safe to fire alongside the first scan.
    _installOAuthMessageListener();
    loadOAuthStatus().catch(function (e) {
      console.warn("Iris overlay: OAuth status fetch failed", e);
    });
    // Load the AI-API consent state too, so the modal can hide the
    // "Authorization required…" banner for instructors who already accepted
    // (and show "✓ Authorized" in the footer). Without this STATE.consent
    // stayed undefined and the banner showed unconditionally.
    loadConsentStatus().catch(function (e) {
      console.warn("Iris overlay: consent status fetch failed", e);
    });
    scheduleScan();
    try {
      var obs = new MutationObserver(scheduleScan);
      obs.observe(document.body, { childList: true, subtree: true });
    } catch (e) {
      console.warn("Iris overlay: MutationObserver unavailable", e);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();

"""Alternative-format renderers.

Accessible HTML is the canonical accessible representation of a converted
document (Iris's HTML, or for legacy documents the HTML rendered from
Reflow's markdown). Every other format — plain text, ePub, audio,
translations, tagged PDF, etc. — derives from that same RenderedPage, so
all surfaces stay in sync. When the HTML gets better (better alt text,
better tables, better lang attributes) every downstream format inherits
the improvement.

Iris writes math as MathML (with the LaTeX in ``alttext``) and marks print
pages with ``<hr role="doc-pagebreak">``. Browsers render both natively;
the renderers below that cannot (plain text, audio, translation, the
WeasyPrint PDF, braille) convert them first, via
:mod:`connector.canvas.iris_output`, into the LaTeX-in-text and
``[Page N]`` forms they were built around.

Renderers are deliberately independent of FastAPI: they take a
RenderedPage in, return bytes (or a string) out, and never import the
server. That makes them trivial to unit test and to call from workers.
"""

from __future__ import annotations

import asyncio
import html as _html
import io
import logging
import os
import re
import threading
import time
import uuid

from .iris_output import mathml_to_latex, strip_pagebreaks
from .markdown_to_html import RenderedPage, render

logger = logging.getLogger(__name__)

_MATHML_RE = re.compile(r"<math[\s>]", re.IGNORECASE)
_H1_RE = re.compile(r"<h1[\s>][\s\S]*?</h1>", re.IGNORECASE)
_EPUB_PATCH_LOCK = threading.Lock()
_TAGS_RE = re.compile(r"<[^>]+>")


def document_heading(rendered: RenderedPage) -> tuple[str, str]:
    """``(page title, heading to add above the body)``.

    A document that has its own ``<h1>`` keeps it as the only one, and its
    text becomes the page title ("Week 1 Reading: The Water Cycle" rather
    than the file name). One without gets the title as its ``<h1>``.
    """
    match = _H1_RE.search(rendered.html or "")
    if match:
        text = _html.unescape(_TAGS_RE.sub(" ", match.group(0)))
        text = re.sub(r"\s+", " ", text).strip()
        return (text or rendered.title or "Document"), ""
    return rendered.title, f"<h1>{_escape(rendered.title)}</h1>"


# ---- formats that need an outside service ----------------------------------

def translation_configured() -> bool:
    """The key :func:`render_translation` needs is set."""
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


_AUDIO_CHECK_SECONDS = 600.0
_audio_check: tuple[float, bool] | None = None


def _aws_credentials_found() -> bool:
    try:
        import boto3
    except ImportError:
        return False
    try:
        return boto3.session.Session().get_credentials() is not None
    except Exception:  # noqa: BLE001 - a broken AWS config is "no credentials"
        return False


async def audio_configured() -> bool:
    """boto3 finds AWS credentials for Amazon Polly (:func:`render_audio_mp3`).

    Cached for ten minutes: looking means asking the EC2 metadata endpoint
    when nothing else has credentials, which takes a second where there is
    none.
    """
    global _audio_check
    now = time.monotonic()
    if _audio_check is not None and now - _audio_check[0] < _AUDIO_CHECK_SECONDS:
        return _audio_check[1]
    found = await asyncio.to_thread(_aws_credentials_found)
    _audio_check = (now, found)
    return found


# Anything between these markers (or these stand-alone forms) is math.
# Used to (a) auto-enable MathJax in the HTML wrapper and (b) decide
# whether Braille goes through the Nemeth code rather than en-us-g2.
# Conservative — false positives only cost a MathJax script load.
_MATH_RE = re.compile(
    # ``$$...$$`` and ``\[...\]`` (display) plus ``$...$`` and ``\(...\)``
    # (inline). ``mhchem`` chemistry markup ``\ce{...}`` and ``\pu{...}``.
    # The inline ``$...$`` form is tightened to require non-whitespace
    # adjacent to both delimiters — otherwise prose with money values
    # ("a price of $5 and another $7…") matches and we erroneously
    # load MathJax + route to Nemeth Braille for non-math documents.
    r"(\$\$[\s\S]+?\$\$)"
    r"|(\\\[[\s\S]+?\\\])"
    r"|(\$(?!\s)[^\$\n]*?[^\s\$]\$)"
    r"|(\\\([\s\S]+?\\\))"
    r"|(\\ce\{[^{}]+\})"
    r"|(\\pu\{[^{}]+\})"
)


def has_math_content(rendered_or_text: RenderedPage | str) -> bool:
    """True when the source carries LaTeX math or mhchem chemistry markup.

    Callers use this to flip on MathJax in the HTML wrapper and to route
    Braille rendering through a Nemeth-aware table. Cheap regex scan; on
    a 200KB textbook chapter it runs in microseconds.
    """
    text = (
        rendered_or_text.html
        if isinstance(rendered_or_text, RenderedPage)
        else str(rendered_or_text or "")
    )
    return bool(_MATH_RE.search(text) or _MATHML_RE.search(text))


def _lang(rendered: RenderedPage) -> str:
    lang = str(getattr(rendered, "lang", "") or "en").strip()
    return lang if re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{1,8})*", lang) else "en"


def _text_pipeline_html(rendered: RenderedPage) -> str:
    """The body as the text-only renderers want it: math as LaTeX, no
    page-break rules."""
    return strip_pagebreaks(mathml_to_latex(rendered.html or ""))


def canonical_html(
    markdown: str,
    *,
    title: str,
    original_pdf_url: str | None = None,
    image_base_url: str | None = None,
) -> RenderedPage:
    """The single source of truth. All other formats derive from this."""
    return render(
        markdown,
        title=title,
        original_pdf_url=original_pdf_url,
        image_base_url=image_base_url,
    )


def html_full_document(rendered: RenderedPage, *, mathjax: bool | None = None) -> str:
    """Wrap the rendered body fragment in a full standalone HTML doc.

    ``mathjax=None`` (the default) auto-detects: any LaTeX delimiters or
    ``\\ce{}`` / ``\\pu{}`` chemistry markup in the rendered body turns
    MathJax on. Pass ``True`` or ``False`` to override (mostly for
    surfaces that already know — e.g., the ``html-math`` alt-format
    endpoint forces True for backwards compat).

    When MathJax is on, the loader also pulls the ``mhchem`` extension
    so chemistry markup like ``\\ce{H2O}`` renders correctly. Screen
    readers consume MathJax's MathML output, so the math is accessible
    to AT, not just visible.
    """
    if mathjax is None:
        mathjax = has_math_content(rendered)
    head_extras = ""
    if mathjax:
        head_extras = (
            "<script>"
            "window.MathJax = {"
            "loader: {load: ['[tex]/mhchem']},"
            "tex: {"
            "packages: {'[+]': ['mhchem']},"
            "inlineMath: [['$','$'], ['\\\\(','\\\\)']],"
            "displayMath: [['$$','$$'], ['\\\\[','\\\\]']]"
            "}"
            "};"
            "</script>"
            '<script async="true" '
            'src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js"></script>'
        )
    title, heading = document_heading(rendered)
    return (
        f"<!doctype html><html lang=\"{_lang(rendered)}\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{_escape(title)}</title>"
        "<style>body{font-family:Georgia,serif;max-width:48rem;margin:2rem auto;"
        "padding:0 1rem;line-height:1.65;color:#1d1d1d;}"
        "img{max-width:100%;height:auto;}"
        "table{border-collapse:collapse;width:100%;margin:1rem 0;}"
        "th,td{border:1px solid #ccc;padding:0.5rem;text-align:left;}"
        "h1,h2,h3,h4{font-family:system-ui,sans-serif;}</style>"
        f"{head_extras}"
        f"</head><body>{heading}{rendered.html}</body></html>"
    )


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_ENTITIES = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"',
             "&apos;": "'", "&#39;": "'", "&nbsp;": " "}


def html_to_plain_text(rendered: RenderedPage) -> str:
    """Strip tags from the rendered HTML to produce a clean text version.

    MathML is read out as its LaTeX first; stripping its tags would leave
    the tokens of ``x²`` as "x 2".
    """
    text = _TAG_RE.sub(" ", _text_pipeline_html(rendered))
    for ent, repl in _ENTITIES.items():
        text = text.replace(ent, repl)
    text = _WS_RE.sub(" ", text).strip()
    return text


def render_epub(rendered: RenderedPage) -> bytes:
    """Convert the canonical HTML into an EPUB3 file."""
    try:
        from ebooklib import epub
    except ImportError as exc:
        raise RuntimeError(
            "ebooklib is not installed; add 'ebooklib' to dependencies"
        ) from exc

    # ebooklib >=0.18 (with lxml >=5) crashes on write: ``_get_nav`` runs
    # ``get_pages()`` over EVERY document item to build the EPUB3 page-list,
    # including the Nav/NCX items whose body is still empty at that point.
    # ``lxml.html.document_fromstring("")`` then raises "Document is empty",
    # surfacing as a 500 on the alt-format endpoint. Wrap ebooklib's
    # ``parse_html_string`` so an empty/whitespace body parses to an empty
    # document instead of throwing. ``ebooklib.epub`` imported the function
    # by name, so that is the name to patch (patching ``ebooklib.utils``
    # alone changes nothing). Restored in ``finally`` so the patch is
    # scoped to this call and never leaks to other ebooklib users; the lock
    # keeps two renders in worker threads from saving each other's patch as
    # the original.
    with _EPUB_PATCH_LOCK:
        return _render_epub_patched(epub, rendered)


def _render_epub_patched(epub, rendered: RenderedPage) -> bytes:
    _orig_parse = epub.parse_html_string

    def _safe_parse(s):
        is_empty = (
            s is None
            or (isinstance(s, (bytes, bytearray)) and not s.strip())
            or (isinstance(s, str) and not s.strip())
        )
        if is_empty:
            from lxml import html as _lh
            return _lh.fromstring("<html><body></body></html>")
        return _orig_parse(s)

    epub.parse_html_string = _safe_parse
    try:
        return _build_epub(epub, rendered)
    finally:
        epub.parse_html_string = _orig_parse


def _build_epub(epub, rendered: RenderedPage) -> bytes:
    title, heading = document_heading(rendered)
    book = epub.EpubBook()
    book.set_identifier(f"accessible-document-{uuid.uuid4()}")
    book.set_title(title or "Accessible Document")
    book.set_language(_lang(rendered))
    # Both converters (Iris now, Reflow for older documents) are Equalify's.
    book.add_author("Equalify")

    chapter = epub.EpubHtml(
        title=title or "Document",
        file_name="content.xhtml",
        lang=_lang(rendered),
    )
    # No XML declaration: ebooklib parses this string with lxml, which
    # refuses a str that declares an encoding, and ebooklib answers that
    # refusal by writing the chapter as an empty file. ebooklib writes its
    # own declaration and namespaces around the body.
    chapter.content = (
        "<html><head>"
        f"<title>{_escape(title)}</title></head>"
        f"<body>{heading}{rendered.html}</body></html>"
    )
    book.add_item(chapter)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.toc = (epub.Link("content.xhtml", title or "Document", "main"),)
    book.spine = ["nav", chapter]

    buf = io.BytesIO()
    epub.write_epub(buf, book)
    return buf.getvalue()


def _chunks(text: str, size: int) -> list[str]:
    """Split text on sentence boundaries first, then by size cap."""
    out: list[str] = []
    cur = ""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    for s in sentences:
        if len(cur) + len(s) + 1 <= size:
            cur = (cur + " " + s).strip() if cur else s
        else:
            if cur:
                out.append(cur)
            if len(s) <= size:
                cur = s
            else:
                # Hard split if a single sentence exceeds size
                for i in range(0, len(s), size):
                    out.append(s[i:i+size])
                cur = ""
    if cur:
        out.append(cur)
    return out


def render_audio_mp3(rendered: RenderedPage, voice: str = "Joanna") -> bytes:
    """Synthesize MP3 audio via Amazon Polly.

    Requires AWS credentials with ``polly:SynthesizeSpeech`` and a
    region (e.g., ``AWS_DEFAULT_REGION=us-east-1``). Raises
    ``RuntimeError`` with a clear, actionable message when either is
    missing — the endpoint surfaces this as a 503, NOT a stack trace.
    """
    try:
        import boto3
        from botocore.exceptions import (
            BotoCoreError,
            ClientError,
            NoCredentialsError,
            NoRegionError,
            PartialCredentialsError,
        )
    except ImportError as exc:
        raise RuntimeError("boto3 is not installed") from exc

    text = html_to_plain_text(rendered)
    if not text:
        raise RuntimeError("No text to synthesize")

    parts = _chunks(text, 2800)
    audio_chunks: list[bytes] = []
    try:
        polly = boto3.client("polly")
    except NoRegionError as exc:
        raise RuntimeError(
            "Audio MP3 requires AWS configuration. Set AWS_DEFAULT_REGION "
            "(e.g., us-east-1) and AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY "
            "for an IAM user with polly:SynthesizeSpeech permission."
        ) from exc
    for chunk in parts:
        try:
            resp = polly.synthesize_speech(
                Text=chunk,
                OutputFormat="mp3",
                VoiceId=voice,
                Engine="neural",
            )
        except (NoCredentialsError, PartialCredentialsError) as exc:
            raise RuntimeError(
                "Audio MP3 requires AWS credentials. Set AWS_ACCESS_KEY_ID + "
                "AWS_SECRET_ACCESS_KEY in .env for an IAM user with "
                "polly:SynthesizeSpeech permission."
            ) from exc
        except (BotoCoreError, ClientError) as exc:
            raise RuntimeError(f"Polly synthesis failed: {exc}") from exc
        audio_chunks.append(resp["AudioStream"].read())
    return b"".join(audio_chunks)


async def render_translation(
    rendered: RenderedPage,
    target_lang: str,
) -> RenderedPage:
    """Translate the accessible HTML into ``target_lang`` via Anthropic Claude.

    Uses the Anthropic Messages API directly — the pydantic-ai + tier
    factory the source fork relied on lives in core Reflow and isn't
    present in the connector. Requires ``ANTHROPIC_API_KEY`` in the
    environment; raises ``RuntimeError`` with a clear message when it's
    missing so the endpoint surfaces 503 rather than a stack trace.

    Math/chemistry safety: the prompt explicitly tells the model to
    leave LaTeX delimiters (``$...$``, ``$$...$$``) and the strings
    they wrap untouched. We don't want to translate ``E = mc^2`` into
    Spanish.
    """
    import os

    try:
        from anthropic import AsyncAnthropic
    except ImportError as exc:
        raise RuntimeError(
            "Translation requires the 'anthropic' package; add it to "
            "dependencies and rebuild the image."
        ) from exc

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "Translation requires ANTHROPIC_API_KEY in the environment. "
            "Set it in .env and restart the connector."
        )

    text = html_to_plain_text(rendered)
    if not text:
        raise RuntimeError("Nothing to translate")

    client = AsyncAnthropic(api_key=api_key)
    system_prompt = (
        "You are a translator preserving formatting and accessibility. "
        "Translate the provided text into the target language. Preserve "
        "headings, lists, emphasis, and reading order. "
        "CRITICAL: Do NOT translate text inside LaTeX math delimiters "
        "(`$...$`, `$$...$$`, `\\(...\\)`, `\\[...\\]`); pass those "
        "spans through verbatim. Do NOT translate chemical formulas "
        "(e.g. H2O, CO2) or chemistry MathJax (`\\ce{...}`). "
        "Return ONLY the translated text — no preamble, no code fences."
    )

    try:
        message = await client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=4096,
            system=system_prompt,
            messages=[{
                "role": "user",
                "content": f"Target language: {target_lang}\n\nText:\n{text}",
            }],
        )
    except Exception as exc:  # noqa: BLE001 — surface upstream errors clearly
        raise RuntimeError(f"Anthropic API error: {exc}") from exc

    translated = ""
    for block in message.content:
        if getattr(block, "type", None) == "text":
            translated += getattr(block, "text", "")
    translated = translated.strip()
    if not translated:
        raise RuntimeError("Claude returned empty translation")

    paragraphs = "".join(
        f"<p>{_escape(p)}</p>" for p in translated.split("\n") if p.strip()
    )
    return RenderedPage(
        title=f"{rendered.title} ({target_lang})",
        html=paragraphs,
    )


def _escape(s: str) -> str:
    if not s:
        return ""
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace("\"", "&quot;")
    )


def pdf_has_text_layer(pdf_bytes: bytes) -> bool:
    """True when at least one page in the source PDF carries selectable text.

    Used to decide whether ``Searchable PDF`` should run OCR (image-only
    scan) or render a Tagged PDF from the canonical HTML (born-digital
    PDF that already has text but no structure). The distinction matters:
    running ocrmypdf on a born-digital PDF is a no-op (the output is
    essentially the input back), and the Tagged PDF path produces a
    properly structured document instead.

    Cheap: ``page.get_text()`` returns the existing text layer without
    re-extracting it.
    """
    try:
        import fitz
    except ImportError:
        # PyMuPDF missing → can't classify; assume image-only and let
        # the OCR path try its best.
        return False
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        for i in range(doc.page_count):
            if doc[i].get_text().strip():
                return True
        return False
    finally:
        doc.close()


def render_tagged_pdf(rendered: RenderedPage, *, base_url: str | None = None) -> bytes:
    """Render the canonical accessible HTML into a tagged PDF.

    Why this and not just OCR: when the source PDF already has a text
    layer (born-digital from Word/InDesign/LaTeX), running ocrmypdf
    against it adds nothing — the output is effectively the input back.
    What faculty actually want for accessibility is a PDF with a real
    structure tree (heading levels, reading order, alt text on
    figures, language markers). That's what WeasyPrint produces from
    the canonical HTML.

    ``base_url`` is the URL the embedded ``<img>`` tags resolve
    against. Pass the route serving the rendered HTML so figure refs
    like ``figures/figure-3.png`` reach the connector's figure proxy.

    Raises ``RuntimeError`` with an actionable message when WeasyPrint
    or its native deps aren't installed, so the endpoint can 503
    cleanly instead of crashing.
    """
    try:
        from weasyprint import HTML
    except ImportError as exc:
        raise RuntimeError(
            "weasyprint is not installed; add 'weasyprint' to "
            "dependencies and rebuild the image."
        ) from exc
    except OSError as exc:  # native deps (cairo/pango/etc.) missing
        raise RuntimeError(
            f"WeasyPrint native dependency missing: {exc}. "
            "The Dockerfile must install libpango / libcairo / "
            "libgdk-pixbuf system libraries."
        ) from exc

    # WeasyPrint doesn't execute JavaScript, so MathJax can't render
    # the LaTeX math the canonical HTML carries. Pre-render each math
    # span to inline SVG server-side via matplotlib's mathtext, then
    # let WeasyPrint embed the SVG into the tagged PDF. The original
    # LaTeX source rides along as the image ``alt`` so screen readers
    # consuming the PDF still get the math content verbatim.
    from .math_render import mathify_html

    # MathML has to become LaTeX first: WeasyPrint does not lay out MathML
    # (it would print the bare tokens), and mathify_html renders LaTeX.
    text_ready = RenderedPage(
        title=rendered.title, html=_text_pipeline_html(rendered), lang=_lang(rendered),
    )
    html_doc = html_full_document(text_ready, mathjax=False)
    html_doc = mathify_html(html_doc)
    # ``pdf_variant='pdf/ua-1'`` is what actually emits the structure
    # tree (StructTreeRoot, StructElem for H1/H2/P/Figure/Table, etc.)
    # plus the /MarkInfo and /DisplayDocTitle catalog entries Acrobat
    # checks for in its Accessibility report. WITHOUT this flag,
    # WeasyPrint produces a regular PDF — no tags — and Acrobat shows
    # "No tags available" in the Accessibility panel. The flag must be
    # passed as a direct kwarg, not via an ``options=`` dict, since
    # the latter is silently dropped as "Unknown rendering option".
    return HTML(string=html_doc, base_url=base_url).write_pdf(
        pdf_variant="pdf/ua-1",
    )


def render_ocr_pdf(original_pdf_bytes: bytes, archival: bool = True) -> bytes:
    """OCR the original source PDF, returning a SEARCHABLE PDF.

    Uses ``ocrmypdf`` which wraps Tesseract + Ghostscript. When
    ``archival`` is True we ask for PDF/A output, which adds archival
    metadata (font embedding, color profiles) -- this is NOT the same
    as PDF/UA tagged structure. PDF/A != accessible. Real PDF/UA
    tagging (proper structure tree, alt text on figures, language
    markers, reading order metadata) requires a separate tagging pass
    that we have not yet implemented.

    The legacy ``tagged`` parameter name was misleading and has been
    renamed to ``archival`` so callers stop assuming this output meets
    PDF/UA accessibility requirements. If your code needs to assert
    accessible PDF output, do not call this function -- the proper
    accessible delivery is the converted Canvas HTML page.

    Raises ``RuntimeError`` with a friendly message if Tesseract or
    Ghostscript aren't installed in the container, so the endpoint can
    return 503.
    """
    # Keep ``tagged`` aliased for backward compatibility one cycle.
    # No call sites pass it positionally; remove after one release.
    import io
    try:
        import ocrmypdf
    except ImportError as exc:
        raise RuntimeError(
            "ocrmypdf not installed; add 'ocrmypdf' to dependencies and "
            "ensure tesseract-ocr + ghostscript are installed in the Dockerfile"
        ) from exc

    out_type = "pdfa" if archival else "pdf"
    in_buf = io.BytesIO(original_pdf_bytes)
    out_buf = io.BytesIO()
    # ``skip_text=True`` is the universal "make this searchable" mode: it OCRs
    # image-only pages and PASSES THROUGH pages that already have a text layer.
    # Critically, it also stops ocrmypdf from erroring on born-digital / tagged
    # PDFs (which already have selectable text) — the default mode refuses
    # those with "PriorOcrFound"/"Tagged PDF" and demands an override flag.
    # ocrmypdf.ocr's first two parameters are POSITIONAL (the first is named
    # ``input_file_or_options`` in current releases), so they must not be
    # passed as keywords.
    try:
        ocrmypdf.ocr(
            in_buf,
            out_buf,
            language="eng",
            output_type=out_type,
            skip_text=True,
            deskew=True,
            optimize=1,
            progress_bar=False,
        )
    except Exception as exc:
        raise RuntimeError(f"OCR failed: {exc}") from exc
    return out_buf.getvalue()


def render_braille_brf(
    rendered: RenderedPage, grade: int = 2, lang_table: str | None = None
) -> bytes:
    """Convert the canonical HTML to BRF (Braille Ready File).

    Delegates to :mod:`connector.canvas.braille`, which formats the
    *document* — headings, lists, tables, figure descriptions, print page
    numbers — through liblouisutdml, rather than translating a flattened
    string. See that module for why the distinction matters and how the
    UEB/Nemeth split works.

    ``lang_table`` is accepted for backwards compatibility and ignored;
    table selection is now per-run (UEB for prose, Nemeth for maths)
    rather than per-document, and a single whole-document table is the
    behaviour this replaced.
    """
    from .braille import render_brf

    if lang_table is not None:
        logger.info(
            "render_braille_brf: lang_table=%r ignored — tables are now "
            "selected per run (UEB prose, Nemeth maths).", lang_table,
        )
    return render_brf(rendered, grade=grade)


def _strip_latex_delimiters(text: str) -> str:
    """Drop ``$...$`` / ``$$...$$`` / ``\\(...\\)`` / ``\\[...\\]`` fences.

    Used before sending math-bearing text to Nemeth Braille — the table
    transcribes math symbols natively, but the LaTeX fence characters
    themselves would come out as literal dollar signs or backslashes
    in the Braille output.
    """
    out = re.sub(r"\$\$([\s\S]+?)\$\$", r" \1 ", text)
    out = re.sub(r"\\\[([\s\S]+?)\\\]", r" \1 ", out)
    out = re.sub(r"\$([^\$\n]+?)\$", r" \1 ", out)
    out = re.sub(r"\\\(([\s\S]+?)\\\)", r" \1 ", out)
    # mhchem chemistry: \ce{H2O} → " H2O ", \pu{} → strip
    out = re.sub(r"\\ce\{([^{}]+)\}", r" \1 ", out)
    out = re.sub(r"\\pu\{([^{}]+)\}", r" \1 ", out)
    return out


READER_TEMPLATE = """<!doctype html>
<html lang=\"__RFLANG__\">
<head>
<meta charset=\"utf-8\">
<title>__RFTITLE__ — Reader</title>
<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
<style>
:root {{ --bg: #fdf6e3; --fg: #1d1d1d; --accent: #0a5fb5; --focus: #ffeb99; }}
body {{ background: var(--bg); color: var(--fg); font-family: Georgia, serif;
       max-width: 42rem; margin: 0 auto; padding: 4rem 1.5rem 8rem;
       line-height: 1.8; font-size: 1.15rem; }}
body.dyslexia {{ font-family: 'OpenDyslexic','Comic Sans MS',sans-serif; letter-spacing: 0.04em; }}
body.dark {{ --bg: #1d1d1d; --fg: #f8f9fa; }}
h1, h2, h3 {{ font-family: system-ui, sans-serif; color: var(--accent); }}
img {{ max-width: 100%; height: auto; }}
.line-focus p, .line-focus li {{ opacity: 0.45; transition: opacity 200ms; }}
.line-focus p.is-current, .line-focus li.is-current {{ opacity: 1; background: var(--focus); padding: 0 0.25rem; }}
.toolbar {{ position: fixed; top: 0; left: 0; right: 0; background: rgba(255,255,255,0.96);
            border-bottom: 1px solid #ccc; padding: 0.5rem 1rem; display: flex; gap: 0.4rem;
            align-items: center; flex-wrap: wrap; z-index: 1000; }}
.toolbar button, .toolbar select {{ background: #fff; border: 1px solid #ccc; padding: 0.35rem 0.7rem;
            border-radius: 4px; cursor: pointer; font: inherit; }}
.toolbar button:hover {{ background: #f0f0f0; }}
.toolbar button[aria-pressed=\"true\"] {{ background: var(--accent); color: #fff; border-color: var(--accent); }}
.toolbar .label {{ font-size: 0.8rem; color: #555; margin-right: 0.25rem; }}
.spacer {{ flex: 1; }}
body.dark .toolbar {{ background: rgba(40,40,40,0.96); color: #f8f9fa; }}
body.dark .toolbar button, body.dark .toolbar select {{ background: #2a2a2a; color: #f8f9fa; border-color: #555; }}
.dictionary-popup {{ position: absolute; background: #fff; border: 1px solid #888; border-radius: 6px;
            box-shadow: 0 4px 12px rgba(0,0,0,0.2); padding: 0.5rem 0.75rem; max-width: 22rem;
            font-size: 0.9rem; z-index: 2000; }}
</style>
</head>
<body>
<div class=\"toolbar\" role=\"toolbar\" aria-label=\"Reader controls\">
  <span class=\"label\">Read aloud</span>
  <button id=\"play\" aria-pressed=\"false\"><span aria-hidden=\"true\">▶</span> Play</button>
  <button id=\"stop\"><span aria-hidden=\"true\">■</span> Stop</button>
  <select id=\"voice\" aria-label=\"Voice\"></select>
  <select id=\"rate\" aria-label=\"Speed\">
    <option value=\"0.8\">0.8×</option><option value=\"1\" selected>1×</option>
    <option value=\"1.2\">1.2×</option><option value=\"1.5\">1.5×</option>
  </select>
  <span class=\"spacer\"></span>
  <span class=\"label\">Font</span>
  <button id=\"font-smaller\" aria-label=\"Smaller\">A−</button>
  <button id=\"font-larger\" aria-label=\"Larger\">A+</button>
  <button id=\"dyslexia\" aria-pressed=\"false\" title=\"Dyslexia-friendly font\">Dyslexia</button>
  <button id=\"dark\" aria-pressed=\"false\" title=\"Dark theme\">Dark</button>
  <button id=\"focus\" aria-pressed=\"false\" title=\"Highlight current line\">Focus</button>
</div>
<article id=\"content\">
__RFHEADING__
__RFBODY__
</article>
<script>
(function () {{
  var article = document.getElementById('content');
  var playBtn = document.getElementById('play');
  var stopBtn = document.getElementById('stop');
  var voiceSel = document.getElementById('voice');
  var rateSel = document.getElementById('rate');
  var smaller = document.getElementById('font-smaller');
  var larger = document.getElementById('font-larger');
  var dys = document.getElementById('dyslexia');
  var dark = document.getElementById('dark');
  var focusBtn = document.getElementById('focus');
  var synth = window.speechSynthesis;
  if (!synth) {{
    playBtn.disabled = true; stopBtn.disabled = true; voiceSel.disabled = true;
  }}

  // Voice picker
  function populateVoices() {{
    if (!synth) return;
    var voices = synth.getVoices() || [];
    voiceSel.innerHTML = '';
    voices.forEach(function (v, i) {{
      var opt = document.createElement('option');
      opt.value = String(i);
      opt.textContent = v.name + ' (' + v.lang + ')';
      if (v.default) opt.selected = true;
      voiceSel.appendChild(opt);
    }});
  }}
  populateVoices();
  if (synth) synth.onvoiceschanged = populateVoices;

  // Build paragraph list for sequential read + focus
  var paragraphs = Array.prototype.slice.call(article.querySelectorAll('p, li, h1, h2, h3, h4'));
  var idx = -1;
  var playing = false;

  function readNext() {{
    idx++;
    if (idx >= paragraphs.length) {{ stop(); return; }}
    var el = paragraphs[idx];
    paragraphs.forEach(function (p) {{ p.classList.remove('is-current'); }});
    el.classList.add('is-current');
    el.scrollIntoView({{ block: 'center', behavior: 'smooth' }});
    var utter = new SpeechSynthesisUtterance(el.textContent.trim());
    var voices = synth.getVoices() || [];
    var sel = voices[parseInt(voiceSel.value)];
    if (sel) utter.voice = sel;
    utter.rate = parseFloat(rateSel.value);
    utter.onend = function () {{ if (playing) readNext(); }};
    synth.speak(utter);
  }}
  function play() {{ playing = true; playBtn.setAttribute('aria-pressed', 'true'); readNext(); }}
  function stop() {{ playing = false; idx = -1; playBtn.setAttribute('aria-pressed', 'false');
                     synth && synth.cancel(); paragraphs.forEach(function (p) {{ p.classList.remove('is-current'); }}); }}

  playBtn.addEventListener('click', function () {{ if (!playing) play(); else stop(); }});
  stopBtn.addEventListener('click', stop);

  // Font size
  var size = 1.15;
  function setSize(s) {{ size = Math.max(0.7, Math.min(2.5, s)); document.body.style.fontSize = size + 'rem'; }}
  smaller.addEventListener('click', function () {{ setSize(size - 0.1); }});
  larger.addEventListener('click', function () {{ setSize(size + 0.1); }});

  // Dyslexia font
  dys.addEventListener('click', function () {{
    var on = document.body.classList.toggle('dyslexia');
    dys.setAttribute('aria-pressed', on ? 'true' : 'false');
  }});

  // Dark mode
  dark.addEventListener('click', function () {{
    var on = document.body.classList.toggle('dark');
    dark.setAttribute('aria-pressed', on ? 'true' : 'false');
  }});

  // Line focus (uses .line-focus on body; CSS handles rest)
  focusBtn.addEventListener('click', function () {{
    var on = document.body.classList.toggle('line-focus');
    focusBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
  }});

  // Picture dictionary on click: highlight word, show definition popup via Free Dictionary API.
  article.addEventListener('click', function (e) {{
    var sel = window.getSelection();
    var word = sel.toString().trim();
    if (!word || word.indexOf(' ') >= 0) return;
    var existing = document.querySelector('.dictionary-popup');
    if (existing) existing.remove();
    var pop = document.createElement('div');
    pop.className = 'dictionary-popup';
    pop.style.left = (e.pageX + 10) + 'px';
    pop.style.top = (e.pageY + 10) + 'px';
    pop.textContent = 'Looking up "' + word + '"…';
    document.body.appendChild(pop);
    fetch('https://api.dictionaryapi.dev/api/v2/entries/en/' + encodeURIComponent(word))
      .then(function (r) {{ return r.json(); }})
      .then(function (j) {{
        if (Array.isArray(j) && j[0] && j[0].meanings && j[0].meanings[0]) {{
          var m = j[0].meanings[0];
          var def = (m.definitions && m.definitions[0] && m.definitions[0].definition) || '(no definition)';
          // Text only: the word is the reader's selection and the definition
          // comes from a third party, so neither may become markup here.
          var strong = document.createElement('strong');
          strong.textContent = word;
          var em = document.createElement('em');
          em.textContent = '(' + (m.partOfSpeech || '') + ')';
          pop.textContent = '';
          pop.appendChild(strong);
          pop.appendChild(document.createTextNode(' '));
          pop.appendChild(em);
          pop.appendChild(document.createElement('br'));
          pop.appendChild(document.createTextNode(def));
        }} else {{
          pop.textContent = 'No definition found for "' + word + '".';
        }}
      }})
      .catch(function () {{ pop.textContent = 'Dictionary lookup failed.'; }});
    setTimeout(function () {{ pop.remove(); }}, 10000);
  }});
}})();
</script>
</body>
</html>
"""


def render_reader_html(rendered: RenderedPage) -> str:
    """Render an Immersive-Reader-style standalone HTML page.

    Uses the browser's built-in Web Speech API (no Azure required).
    Features: read-aloud with voice + speed picker, line focus, font
    size, dyslexia font, dark mode, click-a-word picture dictionary.
    Works in any modern browser, free, no API costs.
    """
    # READER_TEMPLATE doubles all its CSS/JS braces ({{ }}) so it can pass
    # through str.format(). But the document body routinely contains literal
    # braces (LaTeX math like \frac{a}{b}, set notation, code, JSON) which
    # str.format() would try to interpret as replacement fields and crash on
    # ("Single '{' encountered" / KeyError) — 500-ing the reader. So title and
    # body are no-field sentinels injected via str.replace(); .format() with no
    # args is used only to un-double the template's own braces.
    title, heading = document_heading(rendered)
    page = READER_TEMPLATE.format()
    page = page.replace("__RFLANG__", _lang(rendered))
    page = page.replace("__RFTITLE__", _escape(title or "Document"))
    page = page.replace("__RFHEADING__", heading)
    page = page.replace("__RFBODY__", rendered.html or "")
    return page

"""Reading Iris's HTML into the connector's canonical document.

The shapes here are Iris's documented output (docs/API.md "Fetch the HTML
output" and "Partial documents"; src/pipeline/assembly.ts wrapDocument):
a full document whose content is the <main>, with what Iris knows about its
own output in comments after it.
"""

from __future__ import annotations

from connector.canvas.iris_output import (
    IrisPartInfo,
    IrisReport,
    apply_figures,
    figures_from_manifest,
    html_to_markdown,
    mathml_to_latex,
    pagebreaks_to_markers,
    placeholder_page,
    stitch,
    strip_pagebreaks,
)


def _doc(body: str, *, lang: str = "en", after: str = "") -> str:
    return (
        f'<!DOCTYPE html>\n<html lang="{lang}">\n<head>\n  <meta charset="utf-8">\n'
        f"  <title>Week 3 reading</title>\n</head>\n<body>\n<main>\n{body}\n</main>{after}\n"
        "</body>\n</html>\n"
    )


def test_single_part_keeps_main_and_drops_the_shell():
    doc = stitch([(IrisPartInfo(0, 1, 3), _doc("<h1>Reading</h1>\n<p>Text.</p>"))])
    assert doc.body_html == "<h1>Reading</h1>\n<p>Text.</p>"
    assert "<title>" not in doc.body_html and "<main>" not in doc.body_html
    assert doc.lang == "en"
    assert doc.report.parts == 1


def test_language_comes_from_the_root_element():
    doc = stitch([(IrisPartInfo(0, 1, 1), _doc("<p>Hola</p>", lang="es"))])
    assert doc.lang == "es"


def test_markers_become_a_report_and_leave_the_body():
    after = (
        "\n<!-- @page-failed 2\n  This document is incomplete: the source pages above could not be\n-->"
        "\n<!-- @page-uncorrected 3\n  The content of the source pages above IS in this document\n-->"
        "\n<!-- @editor-truncated\n  A correction round could not be completed\n-->"
        "\n<!-- @lint-unavailable\n  This document has NOT been checked\n-->"
        "\n<!-- @review-unread 1 of 2\n  Part of this document has NOT been reviewed.\n-->"
        "\n<!-- @unresolved\n  - Heading levels skip from h2 to h4 on page 1\n"
        "  - The table on page 3 has no column headers\n-->"
    )
    body = (
        "<p>Page one.</p>\n"
        "<!-- @page-failed 2: bedrock: response hit the 32000-token output ceiling -->\n"
        "<p>Page three.</p>"
    )
    doc = stitch([(IrisPartInfo(0, 1, 3), _doc(body, after=after))])
    assert "@page-failed" not in doc.body_html and "<!--" not in doc.body_html
    r = doc.report
    assert r.failed_pages == [2]
    assert "output ceiling" in r.failed_reasons["2"]
    assert r.uncorrected_pages == [3]
    assert r.unresolved == [
        "Heading levels skip from h2 to h4 on page 1",
        "The table on page 3 has no column headers",
    ]
    assert r.editor_truncated and r.lint_unavailable and r.review_unread


def test_part_pages_are_reported_as_source_pages():
    # Pages 26-50 of the PDF are session pages 1-25 of the second part.
    after = "\n<!-- @page-failed 3\n  incomplete\n-->\n<!-- @page-uncorrected 1, 25\n x\n-->"
    doc = stitch([
        (IrisPartInfo(0, 1, 25), _doc("<p>a</p>")),
        (IrisPartInfo(1, 26, 50), _doc("<p>b</p>", after=after)),
    ])
    assert doc.report.failed_pages == [28]
    assert doc.report.uncorrected_pages == [26, 50]
    assert doc.report.parts == 2


def test_open_issues_say_which_part_they_came_from():
    after = "\n<!-- @unresolved\n  - Reading order unclear on page 2\n-->"
    doc = stitch([
        (IrisPartInfo(0, 1, 25), _doc("<p>a</p>")),
        (IrisPartInfo(1, 26, 40), _doc("<p>b</p>", after=after)),
    ])
    assert doc.report.unresolved == ["Part 2 (pages 26-40): Reading order unclear on page 2"]


def test_parts_are_joined_in_page_order_whatever_order_they_arrive():
    doc = stitch([
        (IrisPartInfo(1, 26, 50), _doc("<p>second</p>")),
        (IrisPartInfo(0, 1, 25), _doc("<p>first</p>")),
    ])
    assert doc.body_html.index("first") < doc.body_html.index("second")


def test_only_clashing_ids_are_renamed_and_references_follow():
    part2 = (
        '<p>See note <a href="#fn-1">1</a> and <a href="#appendix-a">Appendix A</a>.</p>'
        '<table><tr><th id="h1" scope="col">Year</th></tr><tr><td headers="h1">2026</td></tr></table>'
        '<p id="fn-1" aria-describedby="h1 elsewhere">Note.</p>'
        '<hr role="doc-pagebreak" aria-label="Page 27" id="page-27">'
    )
    doc = stitch([
        (IrisPartInfo(0, 1, 25), _doc('<p id="fn-1">First part note</p><a href="#fn-1">1</a>')),
        (IrisPartInfo(1, 26, 50), _doc(part2)),
    ])
    body = doc.body_html
    # Part 1 untouched.
    assert '<p id="fn-1">First part note</p>' in body
    assert '<a href="#fn-1">1</a>' in body
    # Part 2's fn-1 clashes with part 1's: renamed, and its own link follows.
    assert 'id="part2-fn-1"' in body and 'href="#part2-fn-1"' in body
    # Ids nobody else uses keep their names, so links into them still work.
    assert 'headers="h1"' in body and 'id="h1"' in body
    assert 'aria-describedby="h1 elsewhere"' in body
    assert 'id="page-27"' in body
    # A link to an id no part defines is left as Iris wrote it.
    assert 'href="#appendix-a"' in body


def test_links_between_parts_reach_their_targets():
    """A contents list in part 1 and endnotes collected at the back of the
    book, in part 2: Iris wrote each link inside one session, so the target
    lives in another part after stitching."""
    part1 = (
        '<nav><a href="#chapter-3">Chapter 3</a></nav>'
        '<p>As shown<a href="#en-12" id="en-ref-12">12</a>.</p>'
        '<p id="p1-fn-1">Page one note</p>'
    )
    part2 = (
        '<h2 id="chapter-3">Chapter 3</h2>'
        '<p id="p1-fn-1">Another page one note</p><a href="#p1-fn-1">1</a>'
        '<aside><p id="en-12">Endnote 12 <a href="#en-ref-12">back</a></p></aside>'
    )
    body = stitch([
        (IrisPartInfo(0, 1, 25), _doc(part1)),
        (IrisPartInfo(1, 26, 50), _doc(part2)),
    ]).body_html
    assert 'href="#chapter-3"' in body and 'id="chapter-3"' in body
    assert 'href="#en-12"' in body and 'id="en-12"' in body
    assert 'href="#en-ref-12"' in body and 'id="en-ref-12"' in body
    # The clashing page-scoped footnote is renamed in part 2, and part 2's
    # own link goes to its own note, not part 1's.
    assert 'id="part2-p1-fn-1"' in body and 'href="#part2-p1-fn-1"' in body
    assert body.count('id="p1-fn-1"') == 1


def test_placeholder_page_is_the_session_position_mapped_to_the_pdf():
    part = IrisPartInfo(1, 26, 50)
    assert placeholder_page("page-3-chart.png", part) == 28
    assert placeholder_page("page-1-logo.png", part) == 26
    assert placeholder_page("page_25-cover.jpg", part) == 50
    # A printed folio is not a session position.
    assert placeholder_page("page-214-figure.png", part) is None
    assert placeholder_page("chart.png", part) is None
    assert placeholder_page("homepage-3-x.png", part) is None


def test_images_get_stable_refs_and_a_manifest():
    body = (
        '<img src="page-1-logo.png" alt="Acme Corp logo">'
        '<figure><img src="page-2-chart.png" alt="Bar chart of revenue"><figcaption>Fig 1</figcaption></figure>'
        '<img src="page-2-rule.png" alt="">'
        '<img src="chart.png" alt="Unplaced picture">'
    )
    doc = stitch([(IrisPartInfo(0, 10, 12), _doc(body))])
    refs = [(f.ref, f.page, f.index_on_page, f.decorative) for f in doc.figures]
    assert refs == [
        ("figures/p10-1.png", 10, 1, False),
        ("figures/p11-1.png", 11, 1, False),
        ("figures/p11-2.png", 11, 2, True),
        ("figures/x1.png", None, 1, False),
    ]
    assert 'src="figures/p11-1.png"' in doc.body_html
    assert "page-2-chart.png" not in doc.body_html


def test_mathml_gets_its_namespace():
    doc = stitch([(IrisPartInfo(0, 1, 1), _doc('<p><math alttext="x^2"><msup><mi>x</mi><mn>2</mn></msup></math></p>'))])
    assert 'xmlns="http://www.w3.org/1998/Math/MathML"' in doc.body_html


def test_apply_figures_points_at_canvas_or_describes():
    body = (
        '<figure><img src="figures/p4-1.png" alt="Map of campus"><figcaption>Campus</figcaption></figure>'
        '<p>Inline <img src="figures/p5-1.png" alt="Signature of the dean"> here.</p>'
        '<figure><img src="figures/p6-1.png" alt=""></figure>'
        '<img src="figures/x1.png" alt="A photo">'
    )
    manifest = figures_from_manifest([
        {"ref": "figures/p4-1.png", "page": 4, "index_on_page": 1, "alt": "Map of campus"},
        {"ref": "figures/p5-1.png", "page": 5, "index_on_page": 1, "alt": "Signature of the dean"},
        {"ref": "figures/p6-1.png", "page": 6, "index_on_page": 1, "alt": ""},
        {"ref": "figures/x1.png", "page": None, "index_on_page": 1, "alt": "A photo"},
    ])
    out = apply_figures(body, {"figures/p4-1.png": "https://canvas/files/9/preview"}, manifest=manifest)
    assert 'src="https://canvas/files/9/preview"' in out
    assert "Image (page 5 of the original PDF): Signature of the dean" in out
    assert "<p>Inline <span" in out  # inline-safe replacement
    assert "Image: A photo" in out
    # Decorative and unresolvable: dropped, and its empty <figure> with it.
    assert "p6-1" not in out and out.count("<figure>") == 1
    assert "figures/" not in out


def test_mathml_to_latex_for_text_pipelines():
    html = (
        '<p>Area <math alttext="\\pi r^2"><mi>π</mi><msup><mi>r</mi><mn>2</mn></msup></math>.</p>'
        '<math display="block" alttext="\\frac{a}{b}"><mfrac><mi>a</mi><mi>b</mi></mfrac></math>'
        '<math><mi>x</mi><mo>+</mo><mn>1</mn></math>'
    )
    out = mathml_to_latex(html)
    assert "\\(\\pi r^2\\)" in out
    assert "\\[\\frac{a}{b}\\]" in out
    # No alttext: the symbols survive in order, even if structure does not.
    assert "\\(x + 1\\)" in out
    assert "<math" not in out


def test_page_breaks_become_print_page_markers_or_vanish():
    html = '<hr role="doc-pagebreak" aria-label="Page 214" id="page-214"><p>Text</p><hr role="doc-pagebreak">'
    assert "<p>[Page 214]</p>" in pagebreaks_to_markers(html)
    assert "<hr" not in pagebreaks_to_markers(html)
    assert "doc-pagebreak" not in strip_pagebreaks(html)


def test_markdown_export_keeps_math_tables_and_pages():
    html = (
        '<hr role="doc-pagebreak" aria-label="Page 3" id="page-3">'
        "<h2>Results</h2><p>Mean <math alttext=\"\\bar{x}_i\"><mi>x</mi></math> rose.</p>"
        '<math display="block" alttext="E=mc^2"><mi>E</mi></math>'
        '<table><thead><tr><th scope="col">Year</th><th scope="col">Total</th></tr></thead>'
        "<tbody><tr><td>2026</td><td>12</td></tr></tbody></table>"
    )
    md = html_to_markdown(html)
    assert "[Page 3]" in md
    assert "## Results" in md
    assert "$\\bar{x}_i$" in md  # underscores inside math are not escaped
    assert "$$E=mc^2$$" in md
    assert "| Year | Total |" in md


def test_report_round_trips_and_tolerates_unknown_keys():
    r = IrisReport(failed_pages=[2], unresolved=["x"], parts=3)
    again = IrisReport.from_dict({**r.to_dict(), "something_new": True})
    assert again == r
    assert IrisReport.from_dict(None) == IrisReport()

"""The format menu's two checks, in words.

The original PDF's veraPDF audit used to reach faculty as a percentage of
PDF/UA rules passed, so an untagged PDF (70 untagged pieces of content, no
language, fonts not embedded) read 93%. The menu now says whether the file
is tagged at all and what is wrong with how much of it.
"""

from __future__ import annotations

import pytest
from connector.canvas.accessibility_summary import accessible_version_summary, original_pdf_summary
from connector.canvas.wcag_checks import WcagFinding

# What veraPDF reported for the course's test document: a born-digital PDF
# with no tags at all.
UNTAGGED = [
    {"rule_id": "7.1-8", "clause": "7.1", "occurrence_count": 1,
     "description": "The Catalog dictionary of a conforming file shall contain the Metadata key. More."},
    {"rule_id": "6.2-1", "clause": "6.2", "occurrence_count": 1,
     "description": "The document catalog dictionary shall include a MarkInfo dictionary containing an entry, "
                    "Marked, whose value shall be true"},
    {"rule_id": "7.1-11", "clause": "7.1", "occurrence_count": 1,
     "description": "The logical structure of the conforming file shall be described by a structure hierarchy"},
    {"rule_id": "7.1-3", "clause": "7.1", "occurrence_count": 70,
     "description": "Content shall be marked as Artifact or tagged as real content"},
    {"rule_id": "7.2-34", "clause": "7.2", "occurrence_count": 56,
     "description": "Natural language for text in page content shall be determined"},
    {"rule_id": "7.21.4.1-1", "clause": "7.21.4.1", "occurrence_count": 4,
     "description": "The font programs for all fonts used for rendering within a conforming file shall be embedded"},
    {"rule_id": "7.1-10", "clause": "7.1", "occurrence_count": 1,
     "description": "The document catalog dictionary shall include a ViewerPreferences dictionary"},
]


@pytest.mark.unit
def test_an_untagged_pdf_says_so_first_and_counts_what_is_wrong() -> None:
    summary = original_pdf_summary(UNTAGGED, False)
    assert summary["verdict"] == "untagged"
    assert summary["headline"] == "Not tagged"
    labels = [p["label"] for p in summary["problems"]]
    # The two "no structure" rules are one problem, and it comes first.
    assert labels[0] == "The PDF has no tags, so screen readers get no headings, lists or reading order"
    assert summary["problems"][0]["rules"] == ["6.2-1", "7.1-11"]
    # Then the most frequent.
    assert labels[1:4] == [
        "70 pieces of content aren’t tagged",
        "56 pieces of text have no language set",
        "4 fonts aren’t embedded",
    ]
    assert "No document metadata" in labels
    assert "Viewers show the file name instead of the title" in labels
    assert len(labels) == 6


@pytest.mark.unit
def test_each_problem_keeps_the_rule_and_its_wording() -> None:
    problems = {p["label"]: p for p in original_pdf_summary(UNTAGGED, False)["problems"]}
    content = problems["70 pieces of content aren’t tagged"]
    assert content["rules"] == ["7.1-3"]
    assert content["detail"] == "Content shall be marked as Artifact or tagged as real content."
    # Two rules' wording reads as two sentences, and only the first sentence
    # of each.
    untagged = problems["The PDF has no tags, so screen readers get no headings, lists or reading order"]
    assert untagged["detail"].startswith("The document catalog dictionary shall include a MarkInfo")
    assert "value shall be true. The logical structure" in untagged["detail"]
    assert problems["No document metadata"]["detail"] == (
        "The Catalog dictionary of a conforming file shall contain the Metadata key."
    )


@pytest.mark.unit
def test_a_tagged_pdf_that_fails_is_not_called_untagged() -> None:
    summary = original_pdf_summary([
        {"rule_id": "7.3-1", "clause": "7.3", "occurrence_count": 3, "description": "Figure tags need alt text"},
        {"rule_id": "7.1-3", "clause": "7.1", "occurrence_count": 1, "description": "Content shall be tagged"},
    ], False)
    assert summary["verdict"] == "fails"
    assert summary["headline"] == "Doesn’t meet PDF/UA-1"
    assert [p["label"] for p in summary["problems"]] == [
        "3 images have no alternative text",
        "1 piece of content isn’t tagged",
    ]
    assert summary["occurrences"] == 4


@pytest.mark.unit
def test_a_compliant_pdf_passes() -> None:
    summary = original_pdf_summary([], True)
    assert summary == {"verdict": "passes", "headline": "Meets PDF/UA-1", "problems": [], "occurrences": 0}


@pytest.mark.unit
def test_a_rule_without_plain_words_uses_verapdfs_own() -> None:
    summary = original_pdf_summary([
        {"rule_id": "9.9-9", "clause": "9.9", "occurrence_count": 2,
         "description": "Something the menu has no words for. A second sentence."},
        "not a violation",
        None,
    ], False)
    assert [p["label"] for p in summary["problems"]] == ["Something the menu has no words for (2 times)"]


@pytest.mark.unit
def test_the_accessible_version_groups_findings_worst_first() -> None:
    findings = [
        WcagFinding("img-decorative-needs-confirm", "info", "1.1.1", "Decorative image"),
        WcagFinding("heading-level-skip", "warning", "1.3.1", "h2 to h4"),
        WcagFinding("img-alt-missing", "error", "1.1.1", "No alt"),
        WcagFinding("img-alt-missing", "error", "1.1.1", "No alt"),
        WcagFinding("title-too-short", "warning", "2.4.2", "Short title"),
    ]
    summary = accessible_version_summary(findings)
    assert (summary["errors"], summary["warnings"], summary["confirm"]) == (2, 2, 1)
    assert summary["items"] == [
        {"label": "2 images have no description", "severity": "error", "rule": "img-alt-missing"},
        {"label": "1 heading skips a level", "severity": "warning", "rule": "heading-level-skip"},
        {"label": "The page title is very short", "severity": "warning", "rule": "title-too-short"},
        {"label": "1 image is marked decorative: check that it carries no information",
         "severity": "info", "rule": "img-decorative-needs-confirm"},
    ]


@pytest.mark.unit
def test_findings_as_dicts_and_unknown_rules() -> None:
    summary = accessible_version_summary([
        {"rule_id": "new-rule", "severity": "warning", "message": "A new check said this. And more."},
    ])
    assert summary["items"] == [
        {"label": "A new check said this.", "severity": "warning", "rule": "new-rule"},
    ]
    assert accessible_version_summary(None) == {"errors": 0, "warnings": 0, "confirm": 0, "items": []}

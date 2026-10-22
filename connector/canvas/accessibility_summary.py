"""The two accessibility checks the format menu reports, in plain words.

* The **original PDF**: veraPDF's PDF/UA-1 audit, run when the file is
  submitted (``verapdf_audit``).
* The **accessible version**: the connector's automated WCAG checks on the
  HTML students get (``wcag_checks``).

The menu used to show the original as a percentage of PDF/UA rules passed.
That number flatters an untagged file: a PDF with no tags at all fails a
handful of rules, each of them many times over, and "passes" the rest
because there is nothing for them to check, so a file a screen reader can
make nothing of scored 93%. What faculty need is what is wrong, how much of
it, and whether the file is tagged at all, so that is what this reports.
Each problem keeps veraPDF's rule id and its own description for anyone
who wants the standard's wording.
"""

from __future__ import annotations

from typing import Any

# veraPDF PDF/UA-1 rules, matched on the rule id first and then on the
# clause (longest prefix wins). (singular, plural): "{n}" is the number of
# times veraPDF found the problem; a rule that can only fail once per file
# has no plural.
_ORIGINAL: dict[str, tuple[str, str | None]] = {
    "7.1-11": ("The PDF has no tags, so screen readers get no headings, lists or reading order", None),
    "6.2-1": ("The PDF has no tags, so screen readers get no headings, lists or reading order", None),
    "7.1-3": ("{n} piece of content isn’t tagged", "{n} pieces of content aren’t tagged"),
    "7.1-1": (
        "{n} piece of decoration sits inside tagged content",
        "{n} pieces of decoration sit inside tagged content",
    ),
    "7.1-2": (
        "{n} piece of tagged content sits inside decoration",
        "{n} pieces of tagged content sit inside decoration",
    ),
    "7.1-8": ("No document metadata", None),
    "7.1-9": ("No title in the document metadata", None),
    "7.1-10": ("Viewers show the file name instead of the title", None),
    "5-1": ("Not identified as PDF/UA", None),
    "7.2-34": ("{n} piece of text has no language set", "{n} pieces of text have no language set"),
    "7.2": ("{n} text problem", "{n} text problems"),
    "7.3": ("{n} image has no alternative text", "{n} images have no alternative text"),
    "7.4": ("{n} heading is out of order", "{n} headings are out of order"),
    "7.5": ("{n} table problem", "{n} table problems"),
    "7.6": ("{n} list problem", "{n} list problems"),
    "7.18.5": ("{n} link isn’t tagged", "{n} links aren’t tagged"),
    "7.18": ("{n} annotation problem", "{n} annotation problems"),
    "7.21.4": ("{n} font isn’t embedded", "{n} fonts aren’t embedded"),
    "7.21.7": ("{n} font can’t be read as text", "{n} fonts can’t be read as text"),
    "7.21": ("{n} font problem", "{n} font problems"),
}
# Rules that mean the file is not tagged at all.
_UNTAGGED = frozenset({"7.1-11", "6.2-1"})

# The connector's own checks on the accessible HTML. Errors and warnings
# lower the score; "confirm" items (info) are judgment calls the checks
# cannot make, such as whether an image marked decorative really is.
_ACCESSIBLE: dict[str, tuple[str, str | None]] = {
    "img-decorative-needs-confirm": (
        "{n} image is marked decorative: check that it carries no information",
        "{n} images are marked decorative: check that they carry no information",
    ),
    "img-alt-missing": ("{n} image has no description", "{n} images have no description"),
    "img-alt-too-short": ("{n} image description is very short", "{n} image descriptions are very short"),
    "multiple-h1": ("More than one top-level heading", None),
    "no-h1": ("No top-level heading", None),
    "no-headings": ("No headings", None),
    "heading-level-skip": ("{n} heading skips a level", "{n} headings skip a level"),
    "table-missing-th": ("{n} table has no header cells", "{n} tables have no header cells"),
    "table-th-needs-scope": (
        "{n} table’s header cells aren’t tied to their data",
        "{n} tables’ header cells aren’t tied to their data",
    ),
    "table-no-caption": ("{n} table has no caption", "{n} tables have no caption"),
    "link-name-missing": ("{n} link has no text", "{n} links have no text"),
    "link-generic-text": ("{n} link says only “click here” or similar", "{n} links say only “click here” or similar"),
    "control-name-missing": ("{n} control has no name", "{n} controls have no name"),
    "lang-attribute-missing": ("The language isn’t set", None),
    "title-missing": ("No page title", None),
    "title-too-short": ("The page title is very short", None),
    "font-element-present": ("Old <font> styling is present", None),
    "empty-document": ("The accessible version is empty", None),
    "malformed-html": ("The accessible version couldn’t be read", None),
    "check-runner-unavailable": ("The checks couldn’t run", None),
}
_SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


def _say(table: dict[str, tuple[str, str | None]], key: str, n: int) -> str | None:
    words = table.get(key)
    if words is None:
        return None
    singular, plural = words
    text = singular if (n == 1 or plural is None) else plural
    return text.replace("{n}", str(n))


def _original_words(rule_id: str, clause: str, n: int) -> str | None:
    said = _say(_ORIGINAL, rule_id, n)
    if said is not None:
        return said
    parts = clause.split(".")
    while parts:
        said = _say(_ORIGINAL, ".".join(parts), n)
        if said is not None:
            return said
        parts.pop()
    return None


def _first_sentence(text: str, limit: int = 160) -> str:
    text = " ".join(str(text or "").split())
    cut = text.find(". ")
    if 0 < cut < limit:
        return text[: cut + 1]
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _fallback_label(description: str, count: int) -> str:
    """veraPDF's own words for a rule the menu has no plain words for."""
    sentence = _first_sentence(description).rstrip(".") or "A PDF/UA rule failed"
    return f"{sentence} ({count} times)" if count > 1 else sentence


def original_pdf_summary(violations: list[Any] | None, compliant: bool | None) -> dict[str, Any]:
    """The original PDF's audit as the menu shows it.

    ``verdict`` is ``passes``, ``untagged`` or ``fails``; ``problems`` are in
    the order to read them (no tags first, then the most frequent).
    """
    problems: dict[str, dict[str, Any]] = {}
    untagged = False
    for raw in violations or []:
        if not isinstance(raw, dict):
            continue
        rule_id = str(raw.get("rule_id") or "")
        clause = str(raw.get("clause") or rule_id.split("-")[0])
        count = max(1, int(raw.get("occurrence_count") or 1))
        if rule_id in _UNTAGGED:
            untagged = True
        description = str(raw.get("description") or "")
        label = _original_words(rule_id, clause, count) or _fallback_label(description, count)
        entry = problems.setdefault(label, {
            "label": label, "count": 0, "rules": [], "details": [],
            "structural": rule_id in _UNTAGGED,
        })
        entry["count"] = max(entry["count"], count) if rule_id in _UNTAGGED else entry["count"] + count
        if rule_id and rule_id not in entry["rules"]:
            entry["rules"].append(rule_id)
        detail = _first_sentence(description, 240)
        if detail and not detail.endswith((".", "\u2026")):
            detail += "."
        if detail and detail not in entry["details"]:
            entry["details"].append(detail)
    ordered = sorted(problems.values(), key=lambda p: (not p["structural"], -p["count"], p["label"]))
    if compliant and not ordered:
        verdict, headline = "passes", "Meets PDF/UA-1"
    elif untagged:
        verdict, headline = "untagged", "Not tagged"
    else:
        verdict, headline = "fails", "Doesn’t meet PDF/UA-1"
    return {
        "verdict": verdict,
        "headline": headline,
        "problems": [
            {"label": p["label"], "rules": p["rules"], "detail": " ".join(p["details"])}
            for p in ordered
        ],
        "occurrences": sum(p["count"] for p in ordered),
    }


def accessible_version_summary(findings: list[Any] | None) -> dict[str, Any]:
    """The automated checks on the accessible HTML, grouped by rule."""
    groups: dict[str, dict[str, Any]] = {}
    for f in findings or []:
        rule_id = str(getattr(f, "rule_id", "") or (f.get("rule_id") if isinstance(f, dict) else ""))
        severity = str(getattr(f, "severity", "") or (f.get("severity") if isinstance(f, dict) else "") or "info")
        message = str(getattr(f, "message", "") or (f.get("message") if isinstance(f, dict) else ""))
        group = groups.setdefault(rule_id or message, {
            "rule": rule_id, "severity": severity, "count": 0, "message": message,
        })
        group["count"] += 1
        if _SEVERITY_ORDER.get(severity, 3) < _SEVERITY_ORDER.get(group["severity"], 3):
            group["severity"] = severity
    items = []
    for g in sorted(groups.values(), key=lambda g: (_SEVERITY_ORDER.get(g["severity"], 3), -g["count"])):
        label = _say(_ACCESSIBLE, g["rule"], g["count"]) or _first_sentence(g["message"])
        items.append({"label": label, "severity": g["severity"], "rule": g["rule"]})
    count = {s: sum(g["count"] for g in groups.values() if g["severity"] == s) for s in _SEVERITY_ORDER}
    return {"errors": count["error"], "warnings": count["warning"], "confirm": count["info"], "items": items}

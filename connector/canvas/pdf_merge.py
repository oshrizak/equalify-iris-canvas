"""Join tagged PDFs into one, keeping every part's structure tree.

A document longer than Iris's page cap is converted in parts, one Iris
session each, and Iris tags each part's PDF on its own. A student wants one
file, so the parts are joined here. Concatenating pages is not enough for a
tagged PDF: each part brings its own structure tree, and the pieces have to
be renumbered to live in one tree:

* each part's ``ParentTree`` keys (the numbers in every page's
  ``/StructParents`` and every annotation's ``/StructParent``) are shifted
  past the keys already used, and the pages and annotations with them;
* each part's top-level elements move under the first part's ``Document``
  element, so a screen reader meets one document, not several;
* element ``/ID`` strings that collide across parts (iris-pdf names them
  per page, ``p1-th1``, so every part has its own ``p1-``) are renamed, and
  the table cells whose ``/Headers`` name them follow;
* the title is the document's, and the PDF/UA-1 claim stays only when every
  part made it.

Pages are copied before the structure tree that points at them, so its
``/Pg`` references land on the copies (qpdf keeps one record per source
file of what it has copied). Anything this module cannot join cleanly
raises :class:`PdfMergeError`, and the caller serves something else rather
than a PDF with a broken tree.
"""

from __future__ import annotations

import io
import logging
import re
from collections.abc import Iterator, Sequence
from typing import Any

import pikepdf
from pikepdf import Array, Dictionary, Name, Pdf, String

logger = logging.getLogger(__name__)


class PdfMergeError(Exception):
    """The parts could not be joined into one tagged PDF."""


def merge_tagged_pdfs(parts: Sequence[bytes], *, title: str | None = None) -> bytes:
    """One tagged PDF from ``parts``, in order. A single part is returned as is."""
    if not parts:
        raise PdfMergeError("there are no parts to join")
    if len(parts) == 1:
        return parts[0]
    try:
        return _merge(parts, title)
    except PdfMergeError:
        raise
    except Exception as exc:  # noqa: BLE001 - any pikepdf/qpdf failure means "cannot join"
        raise PdfMergeError(f"{exc.__class__.__name__}: {exc}") from exc


def _merge(parts: Sequence[bytes], title: str | None) -> bytes:
    out = Pdf.open(io.BytesIO(parts[0]))
    sources: list[Pdf] = []  # kept open until saved: copied streams read from them
    try:
        root = _struct_root(out, 1)
        parent = _top_parent(root)
        nums = _number_tree(root.get("/ParentTree"))
        next_key = max(_int(root.get("/ParentTreeNextKey")), max(nums, default=-1) + 1)
        ids = _element_ids(root)
        role_map = _dict_items(root.get("/RoleMap"))
        class_map = _dict_items(root.get("/ClassMap"))
        claims_ua = _claims_pdfua(out)
        expected_pages = len(out.pages)

        for number, data in enumerate(parts[1:], start=2):
            src = Pdf.open(io.BytesIO(data))
            sources.append(src)
            src_root = _struct_root(src, number)
            claims_ua = claims_ua and _claims_pdfua(src)
            expected_pages += len(src.pages)

            first_new = len(out.pages)
            for page in src.pages:
                out.pages.append(page)
            copied = _copy(out, src, src_root)

            offset = next_key
            seen: set[tuple[int, int]] = set()
            for page in out.pages[first_new:]:
                _shift_struct_parents(page.obj, offset, seen)
            part_nums = _number_tree(copied.get("/ParentTree"))
            for key, value in part_nums.items():
                nums[key + offset] = value
            next_key = max(
                offset + _int(copied.get("/ParentTreeNextKey")),
                max(nums, default=-1) + 1,
            )

            renames: dict[bytes, bytes] = {}
            for id_key, elem in _element_ids(copied).items():
                new_id = id_key
                if new_id in ids:
                    new_id = f"part{number}-".encode() + id_key
                    while new_id in ids:
                        new_id = b"x" + new_id
                    renames[id_key] = new_id
                    elem.ID = String(new_id)
                ids[new_id] = elem
            if renames:
                _rename_headers(copied, renames)

            for role, mapped in _dict_items(copied.get("/RoleMap")).items():
                role_map.setdefault(role, mapped)
            for cls, attrs in _dict_items(copied.get("/ClassMap")).items():
                class_map.setdefault(cls, attrs)

            _adopt_top_elements(parent, copied)
            _merge_acroform(out, src)
            _merge_optional_content(out, src)

        root.ParentTree = out.make_indirect(Dictionary(Nums=_flat_nums(nums)))
        root.ParentTreeNextKey = next_key
        if ids:
            root.IDTree = out.make_indirect(Dictionary(Names=_flat_names(ids)))
        if role_map:
            root.RoleMap = Dictionary(role_map)
        if class_map:
            root.ClassMap = Dictionary(class_map)
        _set_title_and_claim(out, title, claims_ua)
        _check(out, expected_pages)

        buf = io.BytesIO()
        out.save(buf)
        return buf.getvalue()
    finally:
        for src in sources:
            src.close()
        out.close()


# ---- reading --------------------------------------------------------------

def _copy(out: Pdf, src: Pdf, obj: Any) -> Any:
    """``obj`` from ``src``, copied into ``out``. qpdf copies only indirect
    objects, and a catalog often holds its dictionaries directly."""
    if not obj.is_indirect:
        obj = src.make_indirect(obj)
    return out.copy_foreign(obj)


def _struct_root(pdf: Pdf, number: int) -> Dictionary:
    root = pdf.Root.get("/StructTreeRoot")
    if not isinstance(root, Dictionary):
        raise PdfMergeError(f"part {number} is not tagged")
    return root


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _kids(value: Any) -> list[Any]:
    """An element's ``/K`` (one kid, an array of them, or nothing) as a list."""
    if value is None:
        return []
    if isinstance(value, Array):
        return list(value)
    return [value]


def _is_struct_elem(obj: Any) -> bool:
    """A structure element, as opposed to an MCID, an MCR or an OBJR."""
    if not isinstance(obj, Dictionary):
        return False
    kind = obj.get("/Type")
    if kind is not None and kind != Name.StructElem:
        return False
    return "/S" in obj


def _top_parent(root: Dictionary) -> Dictionary:
    """Where later parts' elements go: the ``Document`` element when the
    tree has exactly one top-level element (as iris-pdf writes it), else the
    root itself."""
    top = [k for k in _kids(root.get("/K")) if _is_struct_elem(k)]
    if len(top) == 1 and len(_kids(root.get("/K"))) == 1:
        return top[0]
    return root


def _number_tree(node: Any, depth: int = 0) -> dict[int, Any]:
    items: dict[int, Any] = {}
    if not isinstance(node, Dictionary) or depth > 32:
        return items
    nums = node.get("/Nums")
    if isinstance(nums, Array):
        values = list(nums)
        for i in range(0, len(values) - 1, 2):
            items[_int(values[i])] = values[i + 1]
    for kid in _kids(node.get("/Kids")):
        items.update(_number_tree(kid, depth + 1))
    return items


def _element_ids(root: Any) -> dict[bytes, Any]:
    """Every element ``/ID`` in the tree, read off the elements themselves.

    Not from the ``IDTree`` alone: some producers (WeasyPrint among them)
    give table headers ids and write no ``IDTree``. The joined file gets one
    listing them all.
    """
    ids: dict[bytes, Any] = {}
    for elem in _struct_elements(root):
        value = elem.get("/ID")
        if isinstance(value, String):
            ids.setdefault(bytes(value), elem)
    return ids


def _dict_items(value: Any) -> dict[str, Any]:
    if not isinstance(value, Dictionary):
        return {}
    return {str(key): value[key] for key in value.keys()}


def _struct_elements(start: Any) -> Iterator[Dictionary]:
    """Every structure element below ``start``, each once."""
    stack = list(_kids(start.get("/K")))
    seen: set[tuple[int, int]] = set()
    while stack:
        obj = stack.pop()
        if not _is_struct_elem(obj):
            continue
        if obj.is_indirect:
            if obj.objgen in seen:
                continue
            seen.add(obj.objgen)
        yield obj
        stack.extend(_kids(obj.get("/K")))


# ---- renumbering ------------------------------------------------------------

def _shift_struct_parents(page: Dictionary, offset: int, seen: set[tuple[int, int]]) -> None:
    """Move a copied page's ParentTree keys up by ``offset``: the page's
    own, its annotations', and those of the form XObjects it draws."""
    if "/StructParents" in page:
        page.StructParents = _int(page.StructParents) + offset
    for annot in _kids(page.get("/Annots")):
        if not isinstance(annot, Dictionary):
            continue
        if annot.is_indirect:
            if annot.objgen in seen:
                continue
            seen.add(annot.objgen)
        if "/StructParent" in annot:
            annot.StructParent = _int(annot.StructParent) + offset
    _shift_xobjects(page.get("/Resources"), offset, seen, 0)


def _shift_xobjects(resources: Any, offset: int, seen: set[tuple[int, int]], depth: int) -> None:
    if not isinstance(resources, Dictionary) or depth > 16:
        return
    xobjects = resources.get("/XObject")
    if not isinstance(xobjects, Dictionary):
        return
    for key in list(xobjects.keys()):
        xobj = xobjects[key]
        if not isinstance(xobj, pikepdf.Stream) or xobj.get("/Subtype") != Name.Form:
            continue
        if xobj.objgen in seen:
            continue
        seen.add(xobj.objgen)
        for name in ("/StructParents", "/StructParent"):
            if name in xobj:
                xobj[name] = _int(xobj[name]) + offset
        _shift_xobjects(xobj.get("/Resources"), offset, seen, depth + 1)


def _rename_headers(copied_root: Dictionary, renames: dict[bytes, bytes]) -> None:
    """Point table cells' ``/Headers`` at the renamed header ids."""
    for elem in _struct_elements(copied_root):
        attrs = elem.get("/A")
        for attr in _kids(attrs):
            if not isinstance(attr, Dictionary):
                continue
            headers = attr.get("/Headers")
            if not isinstance(headers, Array):
                continue
            attr.Headers = Array(
                String(renames.get(bytes(h), bytes(h))) if isinstance(h, String) else h
                for h in headers
            )


def _adopt_top_elements(parent: Dictionary, copied_root: Dictionary) -> None:
    """Move a later part's top-level content under ``parent``.

    A part's ``Document`` element is unwrapped, its children moving up,
    unless it owns marked content itself; then it is kept, as a ``Part``.
    """
    adopted: list[Any] = []
    for top in _kids(copied_root.get("/K")):
        if not _is_struct_elem(top):
            raise PdfMergeError("a part's structure tree has content outside any element")
        children = _kids(top.get("/K"))
        if top.get("/S") == Name.Document and all(_is_struct_elem(c) for c in children):
            adopted.extend(children)
        else:
            if top.get("/S") == Name.Document:
                top.S = Name.Part
            adopted.append(top)
    for elem in adopted:
        elem.P = parent
    kids = _kids(parent.get("/K"))
    parent.K = Array(kids + adopted)


# ---- document-level ------------------------------------------------------

def _merge_acroform(out: Pdf, src: Pdf) -> None:
    form = src.Root.get("/AcroForm")
    if not isinstance(form, Dictionary):
        return
    fields = form.get("/Fields")
    if not isinstance(fields, Array) or len(fields) == 0:
        return
    target = out.Root.get("/AcroForm")
    if not isinstance(target, Dictionary):
        out.Root.AcroForm = _copy(out, src, form)
        return
    copied_fields = [_copy(out, src, field) for field in fields]
    target.Fields = Array(_kids(target.get("/Fields")) + copied_fields)


def _merge_optional_content(out: Pdf, src: Pdf) -> None:
    props = src.Root.get("/OCProperties")
    if not isinstance(props, Dictionary):
        return
    copied = _copy(out, src, props)
    target = out.Root.get("/OCProperties")
    if not isinstance(target, Dictionary):
        out.Root.OCProperties = copied
        return

    def union(a: Any, b: Any) -> Array:
        merged = _kids(a)
        known = {o.objgen for o in merged if getattr(o, "is_indirect", False)}
        for obj in _kids(b):
            if getattr(obj, "is_indirect", False) and obj.objgen in known:
                continue
            merged.append(obj)
        return Array(merged)

    target.OCGs = union(target.get("/OCGs"), copied.get("/OCGs"))
    default = target.get("/D")
    src_default = copied.get("/D")
    if isinstance(default, Dictionary) and isinstance(src_default, Dictionary):
        for key in ("/OFF", "/ON"):
            if key in src_default:
                default[key] = union(default.get(key), src_default.get(key))


_UA_RE = re.compile(rb"<pdfuaid:part\b[^>]*>\s*1\s*</pdfuaid:part>|pdfuaid:part\s*=\s*[\"']1[\"']")


def _xmp(pdf: Pdf) -> bytes:
    meta = pdf.Root.get("/Metadata")
    if isinstance(meta, pikepdf.Stream):
        try:
            return bytes(meta.read_bytes())
        except Exception:  # noqa: BLE001 - unreadable metadata claims nothing
            return b""
    return b""


def _claims_pdfua(pdf: Pdf) -> bool:
    return bool(_UA_RE.search(_xmp(pdf)))


def _xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _set_title_and_claim(out: Pdf, title: str | None, claims_ua: bool) -> None:
    """The document's title in Info and XMP, and the PDF/UA-1 claim only
    when every part made it (iris-pdf withholds it from a part with an
    untagged page or an unembedded font)."""
    if title:
        out.docinfo[Name.Title] = String(title)
    xmp = _xmp(out)
    if not xmp:
        return
    new = xmp
    if not claims_ua:
        new = re.sub(rb"<pdfuaid:part\b[^>]*>[\s\S]*?</pdfuaid:part>", b"", new)
        new = re.sub(rb"\spdfuaid:part\s*=\s*[\"'][^\"']*[\"']", b"", new)
    if title:
        li = (
            b'<rdf:li xml:lang="x-default">' + _xml_escape(title).encode("utf-8") + b"</rdf:li>"
        )
        new, count = re.subn(
            rb"(<dc:title\b[^>]*>\s*<rdf:Alt\b[^>]*>)[\s\S]*?(</rdf:Alt>)",
            lambda m: m.group(1) + li + m.group(2),
            new,
            count=1,
        )
    if new != xmp:
        meta = out.Root.Metadata
        meta.write(new)
        meta.Type = Name.Metadata
        meta.Subtype = Name.XML


# ---- writing ---------------------------------------------------------------

def _flat_nums(nums: dict[int, Any]) -> Array:
    flat: list[Any] = []
    for key in sorted(nums):
        flat.extend([key, nums[key]])
    return Array(flat)


def _flat_names(ids: dict[bytes, Any]) -> Array:
    flat: list[Any] = []
    for key in sorted(ids):
        flat.extend([String(key), ids[key]])
    return Array(flat)


def _check(out: Pdf, expected_pages: int) -> None:
    """Cheap self-checks before anyone gets the file."""
    if len(out.pages) != expected_pages:
        raise PdfMergeError(f"joined {len(out.pages)} pages, expected {expected_pages}")
    root = out.Root.StructTreeRoot
    keys = set(_number_tree(root.get("/ParentTree")))
    page_ids = {p.obj.objgen for p in out.pages}
    for index, page in enumerate(out.pages, start=1):
        if "/StructParents" in page.obj and _int(page.obj.StructParents) not in keys:
            raise PdfMergeError(f"page {index} has no entry in the joined ParentTree")
    for elem in _struct_elements(root):
        pg = elem.get("/Pg")
        if pg is not None and (not pg.is_indirect or pg.objgen not in page_ids):
            raise PdfMergeError("an element points at a page that is not in the joined file")

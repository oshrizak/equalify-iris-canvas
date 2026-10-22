"""Small tagged PDFs shaped like equalify-iris-pdf's output, and a checker.

``tagged_pdf`` builds what iris-pdf hands back for one Iris session: a
``Document`` element holding one element per marked-content run, a flat
``ParentTree`` keyed by ``/StructParents`` (pages, form XObjects) and
``/StructParent`` (link annotations), table header ids named per page
(``p1-th1``) and listed in an ``IDTree``, ``/Headers`` on the data cells, and
an XMP packet with the title and, optionally, the PDF/UA-1 claim.

``check_structure`` walks a tagged PDF the way a screen reader's PDF layer
does, from the content back to the tree, and fails on anything that does
not line up.
"""

from __future__ import annotations

import io
from typing import Any

import pikepdf
from pikepdf import Array, Dictionary, Name, String


def _xmp(title: str, ua: bool) -> bytes:
    claim = "<pdfuaid:part>1</pdfuaid:part>" if ua else ""
    return (
        '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:pdfuaid="http://www.aiim.org/pdfua/ns/id/">'
        f'<dc:title><rdf:Alt><rdf:li xml:lang="x-default">{title}</rdf:li></rdf:Alt></dc:title>'
        f"{claim}</rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end=\"w\"?>"
    ).encode()


def tagged_pdf(
    pages: list[list[tuple[str, str]]],
    *,
    title: str = "Doc",
    ua: bool = True,
    table_on: int | None = None,
    link_on: int | None = None,
    footer_xobject: bool = False,
    tagged: bool = True,
) -> bytes:
    pdf = pikepdf.new()
    font = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica))
    footer = None
    if footer_xobject:
        footer = pdf.make_stream(
            b"/P <</MCID 0>> BDC BT /F1 9 Tf 72 40 Td (Footer text) Tj ET EMC",
            Type=Name.XObject, Subtype=Name.Form, BBox=[0, 0, 612, 792],
            Resources=Dictionary(Font=Dictionary(F1=font)),
        )

    root = pdf.make_indirect(Dictionary(Type=Name.StructTreeRoot))
    doc = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name.Document, P=root, K=Array()))
    root.K = doc
    nums: list[Any] = []
    ids: list[Any] = []
    key = 0

    def elem(kind: str, parent: Any, page: Any, kids: Any, **extra: Any) -> Any:
        e = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name("/" + kind), P=parent, Pg=page, K=kids, **extra))
        parent.K.append(e)
        return e

    for index, items in enumerate(pages):
        ops: list[bytes] = []
        y = 700
        for mcid, (kind, text) in enumerate(items):
            ops.append(f"/{kind} <</MCID {mcid}>> BDC BT /F1 12 Tf 72 {y} Td ({text}) Tj ET EMC".encode())
            y -= 20
        extra_mcids = len(items)
        if table_on == index:
            ops.append(f"/TH <</MCID {extra_mcids}>> BDC BT /F1 12 Tf 72 {y} Td (Month) Tj ET EMC".encode())
            ops.append(f"/TD <</MCID {extra_mcids + 1}>> BDC BT /F1 12 Tf 72 {y - 20} Td (January) Tj ET EMC".encode())
            y -= 40
        if link_on == index:
            n = extra_mcids + (2 if table_on == index else 0)
            ops.append(f"/Link <</MCID {n}>> BDC BT /F1 12 Tf 72 {y} Td (Read more) Tj ET EMC".encode())
        resources = Dictionary(Font=Dictionary(F1=font))
        if footer is not None:
            resources.XObject = Dictionary(Fx=footer)
            ops.append(b"/Fx Do")
        page_obj = pdf.make_indirect(Dictionary(
            Type=Name.Page, MediaBox=[0, 0, 612, 792], Resources=resources,
            Contents=pdf.make_stream(b"\n".join(ops)),
        ))
        pdf.pages.append(pikepdf.Page(page_obj))
        page = pdf.pages[-1].obj
        if not tagged:
            continue

        page.StructParents = key
        owners: list[Any] = []
        for mcid, (kind, _text) in enumerate(items):
            owners.append(elem(kind, doc, page, mcid))
        if table_on == index:
            table = elem("Table", doc, page, Array())
            row = elem("TR", table, page, Array())
            th_id = f"p{index + 1}-th1"
            th = elem("TH", row, page, extra_mcids, ID=String(th_id),
                      A=Dictionary(O=Name.Table, Scope=Name.Column))
            row2 = elem("TR", table, page, Array())
            td = elem("TD", row2, page, extra_mcids + 1,
                      A=Dictionary(O=Name.Table, Headers=Array([String(th_id)])))
            owners.extend([th, td])
            ids.append((th_id, th))
        link_elem = None
        if link_on == index:
            link_elem = elem("Link", doc, page, Array())
            link_elem.K.append(len(owners))
            owners.append(link_elem)
        nums.extend([key, Array(owners)])
        key += 1
        if link_elem is not None:
            annot = pdf.make_indirect(Dictionary(
                Type=Name.Annot, Subtype=Name.Link, Rect=[72, 100, 200, 120],
                A=Dictionary(S=Name.URI, URI=String("https://example.org/")),
                StructParent=key, Contents=String("Read more"),
            ))
            page.Annots = Array([annot])
            link_elem.K.append(Dictionary(Type=Name.OBJR, Obj=annot, Pg=page))
            nums.extend([key, link_elem])
            key += 1

    if tagged and footer is not None:
        first = pdf.pages[0].obj
        foot = pdf.make_indirect(Dictionary(
            Type=Name.StructElem, S=Name.P, P=doc, Pg=first,
            K=Dictionary(Type=Name.MCR, Stm=footer, MCID=0, Pg=first),
        ))
        doc.K.append(foot)
        footer.StructParents = key
        nums.extend([key, Array([foot])])
        key += 1

    if tagged:
        root.ParentTree = pdf.make_indirect(Dictionary(Nums=Array(nums)))
        root.ParentTreeNextKey = key
        if ids:
            names: list[Any] = []
            for name, e in sorted(ids):
                names.extend([String(name), e])
            root.IDTree = pdf.make_indirect(Dictionary(Names=Array(names)))
        pdf.Root.StructTreeRoot = root
        pdf.Root.MarkInfo = Dictionary(Marked=True)
    pdf.Root.Lang = String("en")
    pdf.Root.Metadata = pdf.make_stream(_xmp(title, ua), Type=Name.Metadata, Subtype=Name.XML)
    pdf.docinfo[Name.Title] = String(title)
    buf = io.BytesIO()
    pdf.save(buf)
    return buf.getvalue()


# ---- checking ---------------------------------------------------------------

def _kids(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, Array) else [value]


def _number_tree(node: Any) -> dict[int, Any]:
    out: dict[int, Any] = {}
    if not isinstance(node, Dictionary):
        return out
    nums = _kids(node.get("/Nums"))
    for i in range(0, len(nums) - 1, 2):
        out[int(nums[i])] = nums[i + 1]
    for kid in _kids(node.get("/Kids")):
        out.update(_number_tree(kid))
    return out


def _name_tree(node: Any) -> dict[bytes, Any]:
    out: dict[bytes, Any] = {}
    if not isinstance(node, Dictionary):
        return out
    names = _kids(node.get("/Names"))
    for i in range(0, len(names) - 1, 2):
        out[bytes(names[i])] = names[i + 1]
    for kid in _kids(node.get("/Kids")):
        out.update(_name_tree(kid))
    return out


def _same(a: Any, b: Any) -> bool:
    return a is not None and b is not None and a.is_indirect and b.is_indirect and a.objgen == b.objgen


def _owns(elem: Any, page: Any, mcid: int, stream: Any = None) -> bool:
    for kid in _kids(elem.get("/K")):
        if isinstance(kid, int) or (hasattr(kid, "__int__") and not isinstance(kid, Dictionary)):
            try:
                if int(kid) == mcid and stream is None and _same(elem.get("/Pg"), page):
                    return True
            except (TypeError, ValueError):
                continue
        elif isinstance(kid, Dictionary) and kid.get("/Type") == Name.MCR:
            if int(kid.get("/MCID", -1)) != mcid:
                continue
            if stream is not None:
                if _same(kid.get("/Stm"), stream):
                    return True
            elif _same(kid.get("/Pg") or elem.get("/Pg"), page):
                return True
    return False


def _mcids(content_owner: Any) -> list[int]:
    found: list[int] = []
    for operands, operator in pikepdf.parse_content_stream(content_owner):
        if str(operator) == "BDC" and len(operands) == 2 and isinstance(operands[1], Dictionary):
            if "/MCID" in operands[1]:
                found.append(int(operands[1].MCID))
    return found


def check_structure(data: bytes) -> dict[str, Any]:
    """Raise AssertionError on any inconsistency; return a few facts."""
    with pikepdf.open(io.BytesIO(data)) as pdf:
        check = getattr(pdf, "check_pdf_syntax", None) or pdf.check  # pikepdf 10 renamed it
        problems = [str(p) for p in check()]
        assert not [p for p in problems if "error" in p.lower()], problems
        root = pdf.Root.StructTreeRoot
        nums = _number_tree(root.get("/ParentTree"))
        assert int(root.get("/ParentTreeNextKey", 0)) > max(nums, default=-1)
        page_ids = {p.obj.objgen for p in pdf.pages}

        elems: list[Any] = []
        stack = [(k, root) for k in _kids(root.get("/K"))]
        seen: set[tuple[int, int]] = set()
        while stack:
            e, parent = stack.pop()
            if not isinstance(e, Dictionary) or "/S" not in e:
                continue
            assert e.objgen not in seen, "an element is in the tree twice"
            seen.add(e.objgen)
            assert _same(e.get("/P"), parent), f"{e.get('/S')} does not point at its parent"
            pg = e.get("/Pg")
            if pg is not None:
                assert pg.objgen in page_ids, "an element points at a page outside the file"
            elems.append(e)
            stack.extend((k, e) for k in _kids(e.get("/K")))

        marked = 0
        xobjects_seen: set[tuple[int, int]] = set()
        for number, page in enumerate(pdf.pages, start=1):
            mcids = _mcids(page)
            if mcids:
                owners = nums[int(page.obj.StructParents)]
                for mcid in mcids:
                    owner = owners[mcid]
                    assert _owns(owner, page.obj, mcid), f"page {number} MCID {mcid} is not owned"
                    marked += 1
            for annot in _kids(page.obj.get("/Annots")):
                if "/StructParent" in annot:
                    owner = nums[int(annot.StructParent)]
                    assert any(
                        isinstance(k, Dictionary) and k.get("/Type") == Name.OBJR and _same(k.Obj, annot)
                        for k in _kids(owner.get("/K"))
                    ), f"page {number} annotation is not owned"
            xobjects = page.obj.get("/Resources", Dictionary()).get("/XObject", Dictionary())
            for xname in list(xobjects.keys()):
                xobj = xobjects[xname]
                if xobj.objgen in xobjects_seen or "/StructParents" not in xobj:
                    continue
                xobjects_seen.add(xobj.objgen)
                owners = nums[int(xobj.StructParents)]
                for mcid in _mcids(xobj):
                    assert _owns(owners[mcid], None, mcid, stream=xobj), "XObject MCID is not owned"

        ids = _name_tree(root.get("/IDTree"))
        for name, e in ids.items():
            assert bytes(e.ID) == name, "IDTree entry names the wrong element"
        all_ids = [bytes(e.ID) for e in elems if "/ID" in e]
        assert len(all_ids) == len(set(all_ids)), "duplicate element ids"
        for e in elems:
            for attr in _kids(e.get("/A")):
                if isinstance(attr, Dictionary) and "/Headers" in attr:
                    for h in attr.Headers:
                        assert bytes(h) in ids, f"Headers names an unknown id {bytes(h)!r}"

        xmp = pdf.Root.Metadata.read_bytes() if "/Metadata" in pdf.Root else b""
        top = _kids(root.get("/K"))
        return {
            "pages": len(pdf.pages),
            "marked": marked,
            "elements": len(elems),
            "ids": sorted(ids),
            "title": str(pdf.docinfo.get("/Title", "")),
            "xmp": xmp,
            "top": [str(t.get("/S")) for t in top],
            "document_kids": [str(k.get("/S")) for k in _kids(top[0].get("/K")) if isinstance(k, Dictionary)]
            if len(top) == 1 else [],
        }

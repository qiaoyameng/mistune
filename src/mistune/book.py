"""Compose several independent Markdown documents into one book.

Each input document carries a **stable document identifier** and its own
Markdown text.  Ordinary references (``[^key]`` footnotes and ``[label]``
shortcut reference links) are resolved inside the document where they appear
first; a name used in another document can never satisfy a local reference by
accident.

References that intentionally cross document boundaries use dedicated syntax:

``[^@doc-id/key]``
    Cross-document footnote reference: points at the footnote definition
    ``[^key]`` in document ``doc-id``.

``[text][@doc-id/label]`` (also ``[@doc-id/label]``)
    Cross-document reference link: points at the reference link definition
    ``[label]:`` in document ``doc-id``.

Footnotes are numbered by the order of their first reference across the
whole book.  When the same footnote is referenced several times, every
reference gets its own back-link.  Definitions that are never referenced are
omitted from the output.  Reordering documents therefore changes the visible
numbers, while every generated HTML ``id`` embeds the stable document/key
identity so it remains traceable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from .block_parser import BlockParser
from .core import BlockState, InlineState
from .helpers import parse_link_href, parse_link_title
from .inline_parser import InlineParser
from .markdown import Markdown
from .plugins.footnotes import footnotes as footnotes_plugin
from .plugins.footnotes import (
    REF_FOOTNOTE,
    _FOOTNOTE_LABEL,
    parse_footnote_item,
)
from .renderers.html import HTMLRenderer
from .util import safe_entity, unikey

if TYPE_CHECKING:
    from .plugins import Plugin

__all__ = ["BookComposer", "Book", "Diagnostic", "compose_book"]

# Identifier character classes: identifiers stay inside a conservative set so
# the cross-reference syntax never collides with ordinary footnote labels.
_ID_CHARS = r"A-Za-z0-9._\-"
_CROSS_PART = r"[" + _ID_CHARS + r"]{1,128}"

_BOOK_FOOTNOTE_INLINE = (
    r"\[\^(?:"
    r"@(?P<xfoot_doc>" + _CROSS_PART + r")/(?P<xfoot_key>" + _FOOTNOTE_LABEL + r")"
    r"|(?P<footnote_key>" + _FOOTNOTE_LABEL + r")"
    r")\]"
)

_CROSS_LINK_INLINE = (
    r"(?:"
    r"\[(?P<xlink_text>[^\[\]\n]*(?:\n[^\[\]\n]*)*?)\]"
    r"\[@(?P<xlink_doc>" + _CROSS_PART + r")/(?P<xlink_label>[^\[\]\s\\]{1,500})\]"
    r"|\[@(?P<xlink_doc2>" + _CROSS_PART + r")/(?P<xlink_label2>[^\[\]\s\\]{1,500})\]"
    r")"
)

_CODE_FENCE_OPEN = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})[^\n]*$", re.M)
_INLINE_CODE = re.compile(r"`+[^`\n]*`+")
_FULL_REF_LINK = re.compile(
    r"\[[^\[\]\n]{0,500}\]\[[^\[\]\n]{0,500}\]"
)
_SHORTCUT_LINK = re.compile(r"\[(?P<label>[^\[\]\n\\]{1,500})\]")
_UNRESOLVED_FOOTNOTE = re.compile(r"\[\^(?P<key>" + _FOOTNOTE_LABEL + r")\]")


@dataclass
class Diagnostic:
    """A problem reported while composing a book.

    :attr code:   stable machine readable code
    :attr message: human readable description
    :attr document: stable document identifier, when the problem belongs to one
    :attr line: 1-based line number inside the source document, when known
    """

    code: str
    message: str
    document: Optional[str] = None
    line: Optional[int] = None

    def __str__(self) -> str:  # pragma: no cover - trivial
        location = ""
        if self.document is not None:
            location = self.document
            if self.line is not None:
                location += ":" + str(self.line)
            location += ": "
        return location + self.code + ": " + self.message


@dataclass
class Book:
    """Result of composing documents."""

    html: str
    diagnostics: List[Diagnostic] = field(default_factory=list)

    def __str__(self) -> str:
        return self.html

    def raise_for_errors(self) -> None:
        """Raise :class:`BookError` if any diagnostic was collected."""
        if self.diagnostics:
            raise BookError(self.diagnostics)


class BookError(Exception):
    """Raised by :meth:`Book.raise_for_errors` when diagnostics exist."""

    def __init__(self, diagnostics: Sequence[Diagnostic]) -> None:
        self.diagnostics = list(diagnostics)
        super().__init__("; ".join(str(d) for d in self.diagnostics))


# ---------------------------------------------------------------------------
# stable id allocation
# ---------------------------------------------------------------------------

_SLUG_STRIP = re.compile(r"[^A-Za-z0-9]+")


def _slug(value: str) -> str:
    slug = _SLUG_STRIP.sub("-", value).strip("-").lower()
    return slug or "x"


class _IdAllocator:
    """Assigns stable, collision free HTML ids inside one book."""

    def __init__(self) -> None:
        self._used: Dict[str, str] = {}

    def allocate(self, prefix: str, identity: str) -> str:
        base = prefix + "-" + _slug(identity)
        candidate = base
        suffix = 2
        while candidate in self._used and self._used[candidate] != identity:
            candidate = base + "-" + str(suffix)
            suffix += 1
        self._used[candidate] = identity
        return candidate

    def reserve(self, candidate: str, identity: Any) -> str:
        """Reserve an externally shaped id; disambiguate on collision."""
        base = candidate
        suffix = 2
        while candidate in self._used and self._used[candidate] != identity:
            candidate = base + "-" + str(suffix)
            suffix += 1
        self._used[candidate] = identity
        return candidate


# ---------------------------------------------------------------------------
# per document bookkeeping
# ---------------------------------------------------------------------------


@dataclass
class _Doc:
    id: str
    text: str
    state: BlockState
    env: Dict[str, Any]
    tokens: List[Dict[str, Any]]
    diagnostics: List[Diagnostic] = field(default_factory=list)


# ---------------------------------------------------------------------------
# parsing plugin (scoped references + explicit cross references)
# ---------------------------------------------------------------------------


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _normalize_def(raw: str) -> str:
    return " ".join(raw.split())


def _make_book_plugin() -> "Plugin":
    def plugin(md: Markdown) -> None:
        footnotes_plugin(md)
        # The composer builds the book-wide footnote section itself.
        md.after_render_hooks[:] = [
            hook for hook in md.after_render_hooks if hook.__name__ != "md_footnotes_hook"
        ]

        md.inline.register(
            "footnote",
            _BOOK_FOOTNOTE_INLINE,
            _parse_book_footnote,
            before="link",
        )
        md.inline.register(
            "cross_link",
            _CROSS_LINK_INLINE,
            _parse_cross_link,
            before="link",
        )
        md.block.register(
            "ref_footnote",
            REF_FOOTNOTE,
            _parse_book_ref_footnote,
            before="ref_link",
        )
        md.block.register(
            "ref_link",
            r"^ {0,3}\[(?P<reflink_1>(?:[^\[\]\\]|\\.){0,500})\]:",
            _parse_book_ref_link,
        )

        if md.renderer and md.renderer.NAME == "html":
            md.renderer.register("footnote_ref", _render_book_footnote_ref)
            md.renderer.register("footnote_item", _render_book_footnote_item)
            md.renderer.register("footnotes", _render_book_footnotes)
            md.renderer.register("book_doc", _render_book_doc)

    return plugin


def _parse_book_ref_footnote(block: BlockParser, m: "re.Match[str]", state: BlockState) -> int:
    ref = state.env.get("ref_footnotes")
    if ref is None:
        ref = {}
    key = unikey(m.group("footnote_key"))
    raw = m.group("footnote_text")
    ctx = state.env.get("book_doc")
    if key in ref:
        code_line = _line_of(state.src, m.start())
        if _normalize_def(ref[key]) == _normalize_def(raw):
            diag = Diagnostic(
                "duplicate-footnote-definition",
                "Duplicate footnote definition '[" + m.group("footnote_key") + "]'.",
                getattr(ctx, "id", None),
                code_line,
            )
        else:
            diag = Diagnostic(
                "conflicting-footnote-definition",
                "Conflicting footnote definition '[" + m.group("footnote_key") + "]': "
                "a definition with different text already exists; keeping the first one.",
                getattr(ctx, "id", None),
                code_line,
            )
        if ctx is not None:
            ctx.diagnostics.append(diag)
        return m.end()
    ref[key] = raw
    state.env["ref_footnotes"] = ref
    return m.end()


def _parse_book_ref_link(block: BlockParser, m: "re.Match[str]", state: BlockState) -> Optional[int]:
    label = m.group("reflink_1")
    key = unikey(label)
    href, href_pos = parse_link_href(state.src, m.end(), block=True)
    if href is None or href_pos is None:
        return None
    blank_pos = block.BLANK_LINE.search(state.src, href_pos)
    max_pos = blank_pos.start() if blank_pos else len(state.src)
    title, title_pos = parse_link_title(state.src, href_pos, max_pos)
    end_pos = title_pos or href_pos

    ctx = state.env.get("book_doc")
    existing = state.env["ref_links"].get(key)
    if existing is not None:
        if title_pos and title:
            same_title = existing.get("title") == title
        else:
            same_title = "title" not in existing
        same_url = existing.get("url") == href
        code_line = _line_of(state.src, m.start())
        if same_url and same_title:
            diag = Diagnostic(
                "duplicate-link-definition",
                "Duplicate link definition '[" + label + "]'.",
                getattr(ctx, "id", None),
                code_line,
            )
        else:
            diag = Diagnostic(
                "conflicting-link-definition",
                "Conflicting link definition '[" + label + "]': "
                "a definition with a different destination already exists; keeping the first one.",
                getattr(ctx, "id", None),
                code_line,
            )
        if ctx is not None:
            ctx.diagnostics.append(diag)
        # first definition wins; still consume the block so it is not a paragraph
        return end_pos
    # Delegate the actual token consumption/data storage to the stock logic.
    return _stock_parse_ref_link(block, m, state)


def _stock_parse_ref_link(block: BlockParser, m: "re.Match[str]", state: BlockState) -> Optional[int]:
    from .block_parser import _BLANK_TO_LINE, _find_next_blank_line  # local import: private helpers
    from .helpers import unescape_char
    from .util import escape_url

    label = m.group("reflink_1")
    key = unikey(label)
    if not key:
        return None
    href, href_pos = parse_link_href(state.src, m.end(), block=True)
    if href is None:
        return None
    assert href_pos is not None
    blank_pos = _find_next_blank_line(state, href_pos, block.BLANK_LINE)
    max_pos = blank_pos.start() if blank_pos is not None else state.cursor_max
    title, title_pos = parse_link_title(state.src, href_pos, max_pos)
    if title_pos:
        m2 = _BLANK_TO_LINE.match(state.src, title_pos)
        if m2:
            title_pos = m2.end()
        else:
            title_pos = None
            title = None
    if title_pos is None:
        m3 = _BLANK_TO_LINE.match(state.src, href_pos)
        if m3:
            href_pos = m3.end()
        else:
            href_pos = None
            href = None
    end_pos = title_pos or href_pos
    if not end_pos:
        return None
    assert href is not None
    data = {"url": escape_url(unescape_char(href)), "label": label}
    if title:
        data["title"] = title
    state.env["ref_links"][key] = data
    return end_pos


def _parse_book_footnote(
    inline: InlineParser, m: "re.Match[str]", state: InlineState
) -> int:
    ctx = state.env.get("book_doc")
    if m.group("xfoot_doc") is not None:
        state.append_token(
            {
                "type": "cross_footnote_ref",
                "raw": m.group(0),
                "attrs": {
                    "doc": m.group("xfoot_doc"),
                    "key": m.group("xfoot_key"),
                },
            }
        )
        return m.end()

    key = unikey(m.group("footnote_key"))
    ref = state.env.get("ref_footnotes")
    if ref and key in ref:
        refs = state.env.setdefault("book_local_refs", [])
        refs.append(key)
        # global numbering is assigned during composition; placeholder index 0
        state.append_token(
            {
                "type": "footnote_ref",
                "raw": key,
                "attrs": {"index": 0, "doc": ctx.id if ctx else None, "key": key},
            }
        )
    else:
        if ctx is not None:
            ctx.diagnostics.append(
                Diagnostic(
                    "unknown-footnote-reference",
                    "Unknown footnote reference '[" + m.group("footnote_key") + "]'.",
                    ctx.id,
                    _line_of(state.src, m.start()),
                )
            )
        state.append_token({"type": "text", "raw": m.group(0)})
    return m.end()


def _parse_cross_link(inline: InlineParser, m: "re.Match[str]", state: InlineState) -> int:
    doc_id = m.group("xlink_doc") or m.group("xlink_doc2")
    label = m.group("xlink_label") or m.group("xlink_label2")
    text = m.group("xlink_text")
    if not text:
        text = label
    child = state.copy()
    child.src = text.strip(" \r\n\t\f")
    children = inline.render(child)
    token: Dict[str, Any] = {
        "type": "cross_link",
        "children": children,
        "attrs": {"doc": doc_id, "label": label},
    }
    token["_inline_done"] = True
    state.append_token(token)
    return m.end()


# ---------------------------------------------------------------------------
# custom renderers (stable ids)
# ---------------------------------------------------------------------------


def _render_book_doc(renderer: HTMLRenderer, text: str, doc_id: str, id: str) -> str:
    return (
        '<section class="book-doc" data-doc-id="'
        + safe_entity(doc_id)
        + '" id="' + id + '">\n' + text + "</section>\n"
    )


def _render_book_footnote_ref(renderer: HTMLRenderer, *args: Any, **kwargs: Any) -> str:
    # HTMLRenderer passes the token raw text positionally when it exists
    if "index" in kwargs:
        index = kwargs["index"]
    else:
        index = args[1]
    return (
        '<sup class="footnote-ref" id="' + kwargs["ref_id"] + '">'
        '<a href="#' + kwargs["item_id"] + '">' + str(index) + "</a></sup>"
    )


def _render_book_footnote_item(
    renderer: HTMLRenderer, text: str, key: str, index: int, doc: str, id: str, backs: str
) -> str:
    text = text.rstrip()
    if text.endswith("</p>"):
        text = text[: -len("</p>")] + backs + "</p>"
    else:
        text = text + "\n" + backs
    return '<li id="' + id + '">' + text + "</li>\n"


def _render_book_footnotes(renderer: HTMLRenderer, text: str) -> str:
    return '<section class="footnotes">\n<ol>\n' + text + "</ol>\n</section>\n"



# ---------------------------------------------------------------------------
# composer
# ---------------------------------------------------------------------------

DocumentInput = Union[str, Tuple[str, str], Mapping[str, Any]]


class BookComposer:
    """Compose several independent Markdown documents into a single book.

    :param escape: escape raw HTML in the generated HTML (default ``False``)
    :param plugins: extra mistune plugins enabled while parsing documents
    """

    def __init__(
        self,
        escape: bool = False,
        plugins: Optional[Sequence["Plugin"]] = None,
    ) -> None:
        renderer = HTMLRenderer(escape=escape)
        plugin_list: List["Plugin"] = []
        if plugins:
            plugin_list.extend(plugins)
        plugin_list.append(_make_book_plugin())
        self.md = Markdown(
            renderer=renderer,
            block=BlockParser(),
            inline=InlineParser(),
            plugins=plugin_list,
        )

    # -- public API ---------------------------------------------------------

    def compose(self, documents: Sequence[DocumentInput]) -> Book:
        """Parse and compose ``documents`` into one :class:`Book`."""
        diagnostics: List[Diagnostic] = []
        docs = self._parse_documents(documents, diagnostics)

        defs: Dict[str, Dict[str, str]] = {}
        link_defs: Dict[str, Dict[str, Any]] = {}
        for doc in docs:
            for key, raw in doc.env.get("ref_footnotes", {}).items():
                defs[(doc.id, key)] = raw
            for key, data in doc.env.get("ref_links", {}).items():
                link_defs[(doc.id, key)] = data

        doc_allocator = _IdAllocator()
        fn_allocator = _IdAllocator()
        ref_allocator = _IdAllocator()
        doc_slug: Dict[str, str] = {}
        for doc in docs:
            doc_slug[doc.id] = doc_allocator.allocate("doc", doc.id)

        items = _GlobalItems(defs, fn_allocator, ref_allocator)

        # expand inline text first: footnote_ref/cross_link tokens are produced
        # during inline parsing, so numbering must happen afterwards.
        for doc in docs:
            doc.tokens = list(self.md._iter_render(doc.tokens, doc.state))

        # walk every document in book order, assigning global numbers
        for doc in docs:
            self._walk(doc, doc.tokens, items, link_defs, diagnostics)
        # expand footnote definition bodies (may add more references)
        while items.pointer < len(items.ordered):
            entry = items.ordered[items.pointer]
            items.pointer += 1
            doc_id, key = entry.identity
            owner = docs_by_id_map(docs, doc_id)
            if owner is None:
                continue
            body_tokens = self._parse_footnote_body(owner, key, defs[entry.identity])
            body_state = BlockState()
            body_state.env = owner.env
            entry.body_tokens = list(self.md._iter_render(body_tokens, body_state))
            self._walk(owner, entry.body_tokens, items, link_defs, diagnostics)

        # build the footnote section HTML
        section_html = self._render_footnotes(items, docs)

        chunks: List[str] = []
        render_state = BlockState()
        render_state.env = docs[0].env if docs else {"ref_links": {}}
        for doc in docs:
            render_state.env = doc.env
            section_token = {
                "type": "book_doc",
                "children": doc.tokens,
                "attrs": {"doc_id": doc.id, "id": doc_slug[doc.id]},
            }
            chunks.append(self.md.renderer.render_token(section_token, render_state))
        html_out = "".join(chunks) + section_html
        return Book(html=html_out, diagnostics=diagnostics)

    # -- parsing ------------------------------------------------------------

    def _parse_documents(
        self, documents: Sequence[DocumentInput], diagnostics: List[Diagnostic]
    ) -> List[_Doc]:
        seen: Dict[str, int] = {}
        result: List[_Doc] = []
        for position, entry in enumerate(documents):
            doc_id, text = self._coerce_entry(entry, position, diagnostics)
            if doc_id is None or text is None:
                continue
            if doc_id in seen:
                diagnostics.append(
                    Diagnostic(
                        "duplicate-document-id",
                        "Duplicate document identifier '" + doc_id + "'; skipping later occurrence.",
                        doc_id,
                    )
                )
                continue
            seen[doc_id] = position
            doc = self._parse_one(doc_id, text)
            diagnostics.extend(doc.diagnostics)
            result.append(doc)
        return result

    @staticmethod
    def _coerce_entry(
        entry: DocumentInput, position: int, diagnostics: List[Diagnostic]
    ) -> Tuple[Optional[str], Optional[str]]:
        if isinstance(entry, str):
            doc_id = "doc-" + str(position + 1)
            text: Any = entry
        elif isinstance(entry, tuple) and len(entry) == 2:
            doc_id, text = entry
        elif isinstance(entry, Mapping):
            doc_id = entry.get("id") or entry.get("doc_id") or entry.get("name")
            text = entry.get("text")
        else:
            diagnostics.append(
                Diagnostic(
                    "invalid-document-entry",
                    "Document entry at position " + str(position) + " is not a string, "
                    "(id, text) tuple, or mapping.",
                )
            )
            return None, None
        if not isinstance(doc_id, str) or not doc_id.strip():
            diagnostics.append(
                Diagnostic(
                    "invalid-document-id",
                    "Document entry at position " + str(position) + " has no valid identifier.",
                )
            )
            return None, None
        if not isinstance(text, str):
            diagnostics.append(
                Diagnostic(
                    "invalid-document-text",
                    "Document '" + doc_id + "' has non-string text.",
                    doc_id,
                )
            )
            return None, None
        return doc_id, text

    def _parse_one(self, doc_id: str, text: str) -> _Doc:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        if not normalized.endswith("\n"):
            normalized += "\n"
        state = BlockState()
        state.env = {"ref_links": {}, "ref_footnotes": {}}
        doc = _Doc(id=doc_id, text=normalized, state=state, env=state.env, tokens=[])
        state.env["book_doc"] = doc
        state.process(normalized)
        self.md.block.parse(state)
        doc.tokens = state.tokens
        self._scan_unknown_links(doc)
        return doc

    # -- unknown reference scanning ----------------------------------------

    @staticmethod
    def _strip_code(text: str) -> str:
        # remove paired fenced code blocks, preserving line numbers
        out: List[str] = []
        pos = 0
        for m in _CODE_FENCE_OPEN.finditer(text):
            start = m.start()
            if start < pos:
                continue
            fence = m.group("fence")
            close = re.compile(
                r"^ {0,3}" + re.escape(fence[0]) * len(fence) + r"[ \t]*$",
                re.M,
            )
            cm = close.search(text, m.end())
            end = cm.end() if cm else len(text)
            out.append(text[pos:start])
            out.append("\n" * text[start:end].count("\n"))
            pos = end
        out.append(text[pos:])
        text = "".join(out)
        # remove inline code spans
        text = _INLINE_CODE.sub(lambda m: "\n" * m.group(0).count("\n"), text)
        return text

    def _scan_unknown_links(self, doc: _Doc) -> None:
        stripped = self._strip_code(doc.text)
        # remove explicit cross-document constructs; they are resolved separately
        cross_full = re.compile(
            r"\[[^\[\]\n]{0,500}\]\[@[" + _ID_CHARS + r"]{1,128}/[^\[\]\s\\]{1,500}\]"
        )
        stripped = cross_full.sub("", stripped)
        stripped = re.sub(r"\[\^@[" + _ID_CHARS + r"]{1,128}/[^\[\]\s]{1,500}\]", "", stripped)
        ref_links = doc.env.get("ref_links", {})
        ref_notes = set(doc.env.get("ref_footnotes", {}))

        def is_known(label: str) -> bool:
            key = unikey(label)
            return key in ref_links or key in ref_notes

        def report(label: str, pos: int) -> None:
            doc.diagnostics.append(
                Diagnostic(
                    "unknown-link-reference",
                    "Unknown link reference '[" + label + "]'.",
                    doc.id,
                    _line_of(stripped, pos),
                )
            )

        # full/collapsed reference links: [text][label] or [label][]
        consumed_spans: List[Tuple[int, int]] = []
        for m in _FULL_REF_LINK.finditer(stripped):
            consumed_spans.append(m.span())
            left, right = m.group(0)[1:-1].split("][", 1)
            label = right or left
            if not label or label.startswith("@") or label.startswith("^"):
                continue
            if not is_known(label):
                report(label, m.start())

        def consumed(pos: int) -> bool:
            return any(start <= pos < end for start, end in consumed_spans)

        for m in _SHORTCUT_LINK.finditer(stripped):
            if consumed(m.start()):
                continue
            label = m.group("label")
            if not label or label.startswith("^") or label.startswith("@"):
                continue
            if not is_known(label):
                doc.diagnostics.append(
                    Diagnostic(
                        "unknown-link-reference",
                        "Unknown link reference '[" + label + "]'.",
                        doc.id,
                        _line_of(stripped, m.start()),
                    )
                )

    # -- global numbering ---------------------------------------------------

    def _parse_footnote_body(self, owner: _Doc, key: str, raw: str) -> List[Dict[str, Any]]:
        return parse_footnote_item(self.md.block, key, 0, owner.state)["children"]

    def _walk(
        self,
        doc: _Doc,
        tokens: List[Dict[str, Any]],
        items: "_GlobalItems",
        link_defs: Dict[Tuple[str, str], Dict[str, Any]],
        diagnostics: List[Diagnostic],
    ) -> None:
        for token in tokens:
            token_type = token.get("type")
            if token_type == "text" and isinstance(token.get("raw"), str):
                # an unresolved local footnote reference was rendered back to text
                for um in _UNRESOLVED_FOOTNOTE.finditer(token["raw"]):
                    diagnostics.append(
                        Diagnostic(
                            "unknown-footnote-reference",
                            "Unknown footnote reference '[^" + um.group("key") + "]'.",
                            doc.id,
                        )
                    )
            if token_type == "footnote_ref":
                attrs = token["attrs"]
                entry = items.reference((doc.id, unikey(attrs["key"])))
                token["attrs"] = {
                    "index": entry.number,
                    "item_id": entry.item_id,
                    "ref_id": entry.next_ref_id(),
                }
            elif token_type == "cross_footnote_ref":
                attrs = token["attrs"]
                target_doc = attrs["doc"]
                identity = (target_doc, unikey(attrs["key"]))
                if identity not in items.defs:
                    diagnostics.append(
                        Diagnostic(
                            "unknown-cross-reference",
                            "Cross-document footnote '" + target_doc + "/"
                            + attrs["key"] + "' is not defined.",
                            doc.id,
                        )
                    )
                    token["type"] = "text"
                    token.pop("attrs", None)
                    continue
                entry = items.reference(identity)
                token["type"] = "footnote_ref"
                token.pop("raw", None)
                token["attrs"] = {
                    "index": entry.number,
                    "item_id": entry.item_id,
                    "ref_id": entry.next_ref_id(),
                }
            elif token_type == "cross_link":
                attrs = token["attrs"]
                identity = (attrs["doc"], unikey(attrs["label"]))
                target = link_defs.get(identity)
                if target is None:
                    diagnostics.append(
                        Diagnostic(
                            "unknown-cross-reference",
                            "Cross-document link '" + attrs["doc"] + "/"
                            + attrs["label"] + "' is not defined.",
                            doc.id,
                        )
                    )
                    token["type"] = "text"
                    token.pop("attrs", None)
                else:
                    token["type"] = "link"
                    token["attrs"] = {"url": target["url"], "title": target.get("title")}
            children = token.get("children")
            if isinstance(children, list):
                self._walk(doc, children, items, link_defs, diagnostics)

    def _render_footnotes(self, items: "_GlobalItems", docs: List[_Doc]) -> str:
        if not items.ordered:
            return ""
        render_state = BlockState()
        render_state.env = {"ref_links": {}}
        item_html_parts: List[str] = []
        for entry in items.ordered:
            doc_id, key = entry.identity
            owner = docs_by_id_map(docs, doc_id)
            assert owner is not None
            render_state.env = owner.env
            body_html = self.md.renderer.render_tokens(entry.body_tokens or [], render_state)
            backs = "".join(
                '<a href="#' + ref_id + '" class="footnote" title="back to reference '
                + str(i) + '">&#8617;'
                + ('<sup>' + str(i) + '</sup>' if len(entry.ref_ids) > 1 else "")
                + "</a>"
                for i, ref_id in enumerate(entry.ref_ids, 1)
            )
            token = {
                "type": "footnote_item",
                "attrs": {
                    "key": key,
                    "index": entry.number,
                    "doc": doc_id,
                    "id": entry.item_id,
                    "backs": backs,
                },
            }
            item_html_parts.append(self.md.renderer.render_token(
                {**token, "raw": body_html}, render_state
            ))
        return _render_book_footnotes(self.md.renderer, "".join(item_html_parts))


def docs_by_id_map(docs: List[_Doc], doc_id: str) -> Optional[_Doc]:
    for doc in docs:
        if doc.id == doc_id:
            return doc
    return None


class _GlobalEntry:
    def __init__(self, identity: Tuple[str, str], number: int, item_id: str,
                 ref_allocator: "_IdAllocator") -> None:
        self.identity = identity
        self.number = number
        self.item_id = item_id
        self.ref_allocator = ref_allocator
        self.ref_ids: List[str] = []
        self.body_tokens: Optional[List[Dict[str, Any]]] = None

    def next_ref_id(self) -> str:
        occurrence = len(self.ref_ids) + 1
        if occurrence == 1:
            ref_id = self.item_id.replace("fn-", "fnref-", 1)
            ref_id = self.ref_allocator.reserve(ref_id, self.identity)
        else:
            ref_id = self.ref_allocator.allocate(
                "fnref",
                self.identity[0] + "-" + self.identity[1] + "#" + str(occurrence),
            )
        self.ref_ids.append(ref_id)
        return ref_id


class _GlobalItems:
    def __init__(
        self,
        defs: Dict[Tuple[str, str], str],
        allocator: _IdAllocator,
        ref_allocator: _IdAllocator,
    ) -> None:
        self.defs = defs
        self.allocator = allocator
        self.ref_allocator = ref_allocator
        self.ordered: List[_GlobalEntry] = []
        self.pointer = 0
        self._by_identity: Dict[Tuple[str, str], _GlobalEntry] = {}

    def reference(self, identity: Tuple[str, str]) -> _GlobalEntry:
        entry = self._by_identity.get(identity)
        if entry is None:
            number = len(self.ordered) + 1
            item_id = self.allocator.allocate("fn", identity[0] + "-" + identity[1])
            entry = _GlobalEntry(identity, number, item_id, self.ref_allocator)
            self._by_identity[identity] = entry
            self.ordered.append(entry)
        return entry


def compose_book(
    documents: Sequence[DocumentInput],
    escape: bool = False,
    plugins: Optional[Sequence["Plugin"]] = None,
) -> Book:
    """Convenience wrapper around :class:`BookComposer`.

    ``documents`` is a list of Markdown strings (document ids are generated as
    ``doc-1``, ``doc-2`` ...), ``(id, text)`` tuples, or mappings with ``id``
    and ``text`` keys.
    """
    return BookComposer(escape=escape, plugins=plugins).compose(documents)

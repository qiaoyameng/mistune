"""Combine several independent Markdown documents into a single book.

Each document keeps its own local scope for footnote definitions (``[^1]``)
and reference link definitions (``[home]`` / ``[label]: ...``).  When
documents are combined into a book:

* footnotes are numbered globally by the order of their first citation in
  the whole book, while repeated citations of the same footnote generate
  separate back links;
* footnote definitions that are never cited are not rendered;
* references never resolve against another document by name coincidence --
  cross-document references require an explicit syntax::

      [^@doc-id:footnote-key]     (cross-document footnote)
      [@doc-id:link-label]        (cross-document shortcut link)
      [text][@doc-id:link-label]  (cross-document full reference link)

* every generated HTML ``id`` is derived from the stable document
  identifier, so reordering documents may change the displayed footnote
  numbers but never changes document identity;
* unknown references, duplicate document identifiers and conflicting
  duplicate definitions are reported as diagnostics.

The normal single-document :meth:`Markdown.parse` semantics are unchanged.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Tuple, Union

from .core import BlockState
from .helpers import LINK_LABEL, parse_link_href, parse_link_title, unescape_char
from .plugins.footnotes import REF_FOOTNOTE, parse_footnote_item
from .util import escape_url, unikey

if TYPE_CHECKING:
    from .markdown import Markdown

__all__ = [
    "Document",
    "Diagnostic",
    "BookResult",
    "BookError",
    "MarkdownBook",
    "combine_markdown",
]

# A document identifier is part of HTML ids, therefore only URL/token safe
# characters are allowed.
_DOC_ID_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._-]*"
_DOC_ID_RE = re.compile(r"^" + _DOC_ID_PATTERN + r"$")

_FOOTNOTE_DEF_RE = re.compile(REF_FOOTNOTE, re.M)
_LINK_DEF_RE = re.compile(r"^ {0,3}\[(?P<label>" + LINK_LABEL + r")\]:", re.M)
_BLANK_LINE_RE = re.compile(r"(^[ \t\v\f]*\n)+", re.M)
_BLANK_TO_LINE_RE = re.compile(r"[ \t]*\n")

# Code spans/blocks must not be inspected for unresolved references.
_FENCED_CODE_RE = re.compile(
    r"^ {0,3}(`{3,}|~{3,})[^\n]*\n.*?^ {0,3}\1[ \t]*$",
    re.M | re.S,
)
_INDENT_CODE_RE = re.compile(r"(?:^ {4}[^\n]*(?:\n|$))+", re.M)
_INLINE_CODE_RE = re.compile(r"`+[^`\n]*`+")
_ESCAPE_RE = re.compile(r"\\[\\\[\]]")

# [text][@doc-id:label]
_LINK_LABEL_BODY = r"(?:[^\\\[\]]|\\.){0,500}"
_CROSS_FULL_LINK_RE = re.compile(
    r"\[(?P<text>" + _LINK_LABEL_BODY + r")\]\[(?P<label>@(?P<doc>" + _DOC_ID_PATTERN + r"):(?P<key>[^\]\s]+))\]"
)
# [@doc-id:label] not immediately followed by another label group
_CROSS_SHORTCUT_LINK_RE = re.compile(
    r"\[@(?P<doc>" + _DOC_ID_PATTERN + r"):(?P<key>[^\]\s]+)\](?![\[])"
)


@dataclass
class Document:
    """A Markdown document with a stable identifier.

    :param id: stable, unique document identifier
    :param text: Markdown source text
    """

    id: str
    text: str


@dataclass
class Diagnostic:
    """A problem reported while combining documents."""

    code: str
    message: str
    document: Optional[str] = None
    line: Optional[int] = None

    def format(self) -> str:
        location = ""
        if self.document is not None:
            location = repr(self.document)
            if self.line is not None:
                location += ":" + str(self.line)
            location += ": "
        return "[" + self.code + "] " + location + self.message


class BookError(Exception):
    """Raised for structural errors (e.g. duplicate document identifiers)
    or when a book is compiled in strict mode and diagnostics exist."""

    def __init__(self, diagnostics: Sequence[Diagnostic]):
        self.diagnostics: List[Diagnostic] = list(diagnostics)
        message = "\n".join(d.format() for d in self.diagnostics)
        super().__init__(message)


@dataclass
class BookResult:
    """Result of combining documents into a book."""

    html: str
    diagnostics: List[Diagnostic] = field(default_factory=list)
    documents: List[str] = field(default_factory=list)

    def raise_if_errors(self) -> "BookResult":
        if self.diagnostics:
            raise BookError(self.diagnostics)
        return self


def render_book_footnote_ref(
    renderer: Any, number: int, ref_id: str, href: str
) -> str:
    return (
        '<sup class="footnote-ref" id="'
        + ref_id
        + '"><a href="'
        + href
        + '">'
        + str(number)
        + "</a></sup>"
    )


# ---------------------------------------------------------------------------
# Stable id allocation
# ---------------------------------------------------------------------------

def _slug(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^\w.-]+", "-", value, flags=re.UNICODE)
    value = value.strip(".-")
    return value or "x"


class _IdAllocator:
    """Allocate stable, collision free HTML ids within one book."""

    def __init__(self) -> None:
        self._used: Dict[str, int] = {}

    def reserve(self, value: str) -> str:
        if value not in self._used:
            self._used[value] = 0
            return value
        count = self._used[value]
        while True:
            count += 1
            candidate = value + "-" + str(count)
            if candidate not in self._used:
                self._used[value] = count
                self._used[candidate] = 0
                return candidate

    def allocate(self, *parts: str) -> str:
        return self.reserve("-".join(_slug(p) for p in parts))


# ---------------------------------------------------------------------------
# Definition / usage scanning (for duplicate definitions and unknown refs)
# ---------------------------------------------------------------------------

def _line_of(src: str, pos: int) -> int:
    return src.count("\n", 0, pos) + 1


def _iter_definitions(src: str):
    """Yield ``(kind, line, key, payload)`` for every footnote/link
    definition block.

    ``kind`` is ``"footnote"`` or ``"link"``.
    """
    footnote_spans: List[Tuple[int, int]] = []
    for m in _FOOTNOTE_DEF_RE.finditer(src):
        key = unikey(m.group("footnote_key"))
        text = re.sub(r"\s+", " ", m.group("footnote_text")).strip()
        footnote_spans.append((m.start(), m.end()))
        yield "footnote", _line_of(src, m.start()), key, text

    for m in _LINK_DEF_RE.finditer(src):
        if any(start <= m.start() < end for start, end in footnote_spans):
            continue
        label = m.group("label")
        key = unikey(label)
        if not key:
            continue

        href, href_pos = parse_link_href(src, m.end(), block=True)
        if href is None:
            continue
        assert href_pos is not None

        blank = _BLANK_LINE_RE.search(src, href_pos)
        max_pos = blank.start() if blank else len(src)
        title, title_pos = parse_link_title(src, href_pos, max_pos)
        if title_pos:
            m2 = _BLANK_TO_LINE_RE.match(src, title_pos)
            if m2:
                title_pos = m2.end()
            else:
                title_pos = None
                title = None

        if title_pos is None:
            m3 = _BLANK_TO_LINE_RE.match(src, href_pos)
            if m3:
                href_pos = m3.end()
            else:
                href_pos = None
                href = None

        if not (title_pos or href_pos) or href is None:
            continue

        data: Dict[str, Any] = {"url": escape_url(unescape_char(href)), "label": label}
        if title:
            data["title"] = title
        yield "link", _line_of(src, m.start()), key, data


def _mask_code(src: str) -> str:
    """Replace code content with blanks so reference scanning ignores it."""
    parts: List[str] = []
    pos = 0
    for m in _FENCED_CODE_RE.finditer(src):
        parts.append(src[pos : m.start()])
        parts.append(re.sub(r"[^\n]", " ", src[m.start() : m.end()]))
        pos = m.end()
    parts.append(src[pos:])
    src = "".join(parts)

    parts = []
    pos = 0
    for m in _INDENT_CODE_RE.finditer(src):
        parts.append(src[pos : m.start()])
        parts.append(re.sub(r"[^\n]", " ", src[m.start() : m.end()]))
        pos = m.end()
    parts.append(src[pos:])
    src = "".join(parts)

    src = _INLINE_CODE_RE.sub(lambda m: " " * (m.end() - m.start()), src)
    src = _ESCAPE_RE.sub(lambda m: " " * len(m.group(0)), src)
    return src


def _mask_definitions(src: str) -> str:
    """Mask code plus the label part of every footnote/link definition, so
    usage scanning never treats definitions as references.  Non-newline
    characters are replaced with spaces to keep line numbers intact.
    """
    src = _mask_code(src)

    def blank(span_start: int, span_end: int) -> str:
        return re.sub(r"[^\n]", " ", src[span_start:span_end])

    parts: List[str] = []
    pos = 0
    for m in _FOOTNOTE_DEF_RE.finditer(src):
        parts.append(src[pos : m.start()])
        # mask only "[^key]:" keeping the definition body untouched
        end = m.end() - len(m.group("footnote_text"))
        parts.append(blank(m.start(), end))
        pos = end
    parts.append(src[pos:])
    src = "".join(parts)

    parts = []
    pos = 0
    footnote_spans = [(m.start(), m.end()) for m in _FOOTNOTE_DEF_RE.finditer(src)]
    for m in _LINK_DEF_RE.finditer(src):
        if any(start <= m.start() < end for start, end in footnote_spans):
            continue
        parts.append(src[pos : m.start()])
        parts.append(blank(m.start(), m.end()))
        pos = m.end()
    parts.append(src[pos:])
    return "".join(parts)


def _find_label_end(src: str, start: int) -> Optional[int]:
    """Find the closing ``]`` of a link label beginning just after ``[``."""
    pos = start
    while pos < len(src):
        char = src[pos]
        if char == "\\":
            pos += 2
            continue
        if char == "[":
            return None
        if char == "]":
            return pos
        pos += 1
    return None


def _find_paren_end(src: str, start: int) -> int:
    depth = 0
    pos = start
    while pos < len(src):
        char = src[pos]
        if char == "\\":
            pos += 2
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return pos + 1
        pos += 1
    return len(src)


def _split_cross(label: str) -> Optional[Tuple[str, str]]:
    if not label.startswith("@") or ":" not in label:
        return None
    doc, key = label[1:].split(":", 1)
    if not _DOC_ID_RE.match(doc) or not key:
        return None
    return doc, key


def rewrite_cross_links(src: str) -> str:
    """Rewrite explicit cross-document link syntax into normal reference
    links whose injected definitions resolve them.

    Shortcut ``[@doc:key]`` becomes ``[key][@doc:key]`` so the readable
    link text is preserved; full references ``[text][@doc:key]`` keep their
    text and only the label is normalized.  Replacements never insert line
    breaks, so line numbers stay identical to the original source.
    """
    masked = _mask_code(src)
    edits: List[Tuple[int, int, str]] = []

    full_spans: List[Tuple[int, int]] = []
    for m in _CROSS_FULL_LINK_RE.finditer(masked):
        full_spans.append((m.start(), m.end()))
    for m in _CROSS_SHORTCUT_LINK_RE.finditer(masked):
        if any(start <= m.start() < end for start, end in full_spans):
            continue
        edits.append(
            (
                m.start(),
                m.end(),
                "[" + m.group("key") + "][@" + m.group("doc") + ":" + m.group("key") + "]",
            )
        )

    edits.sort(key=lambda e: e[0])
    out: List[str] = []
    pos = 0
    for start, end, replacement in edits:
        out.append(src[pos:start])
        out.append(replacement)
        pos = end
    out.append(src[pos:])
    return "".join(out)


def _iter_usages(src: str):
    """Yield ``(kind, line, raw_label, doc, key)`` for reference usages.

    ``kind`` is ``footnote`` or ``link``; ``doc`` is set for explicit
    cross-document references.  Only reference *labels* are reported: the
    text part of a full reference ``[text][label]`` and direct link
    destinations are skipped.
    """
    masked = _mask_definitions(src)
    pos = 0
    while True:
        idx = masked.find("[", pos)
        if idx < 0:
            return
        line = _line_of(src, idx)
        pos = idx + 1

        is_image = idx > 0 and masked[idx - 1] == "!"
        if masked.startswith("[^", idx):
            close = masked.find("]", idx + 2)
            if close < 0:
                continue
            inner = masked[idx + 2 : close]
            pos = close + 1
            if not inner or re.search(r"\s", inner) or "[" in inner:
                continue
            cross = re.match(r"@(" + _DOC_ID_PATTERN + r"):(.+)$", inner)
            if cross:
                yield "footnote", line, inner, cross.group(1), cross.group(2)
            else:
                yield "footnote", line, inner, None, inner
            continue

        close = _find_label_end(masked, idx + 1)
        if close is None:
            continue
        text_label = masked[idx + 1 : close]
        after = close + 1

        if after < len(masked) and masked[after] == "(":
            pos = _find_paren_end(masked, after)
            continue

        if after < len(masked) and masked[after] == "[":
            label_close = _find_label_end(masked, after + 1)
            if label_close is None:
                pos = after + 1
                continue
            label = masked[after + 1 : label_close]
            pos = label_close + 1
        else:
            label = text_label
            pos = after

        if is_image or not label or "[" in label:
            continue
        cross = _split_cross(label)
        if cross:
            doc, key = cross
            yield "link", line, label, doc, key
        else:
            yield "link", line, label, None, label



# ---------------------------------------------------------------------------
# Book compiler
# ---------------------------------------------------------------------------

class _ParsedDocument:
    def __init__(self, doc: Document, src: str, original_src: str = ""):
        self.doc = doc
        self.src = src
        self.original_src = original_src or src
        self.tokens: List[Dict[str, Any]] = []
        self.env: Dict[str, Any] = {}
        self.ast_tokens: List[Dict[str, Any]] = []
        self.seen_footnotes: Dict[str, Tuple[str, int]] = {}
        self.seen_links: Dict[str, Tuple[Dict[str, Any], int]] = {}
        self.wrapper_id: str = ""
        self.ref_counts: Dict[Tuple[str, str], int] = {}

    def render_state(self) -> BlockState:
        state = BlockState()
        state.env = self.env
        return state

    def item_state(self) -> BlockState:
        return self.render_state()

    def count_ref(self, target: Tuple[str, str]) -> int:
        count = self.ref_counts.get(target, 0)
        self.ref_counts[target] = count + 1
        return count


class MarkdownBook:
    """Combine multiple Markdown documents into a single rendered book.

    :param md: a :class:`~mistune.Markdown` instance configured with the
        ``footnotes`` plugin (and any other plugins the documents use); a
        default instance with footnotes enabled is created when omitted
    """

    def __init__(self, md: Optional["Markdown"] = None) -> None:
        if md is None:
            from . import create_markdown

            md = create_markdown(escape=False, plugins=["footnotes"])
        self.md = md
        self.diagnostics: List[Diagnostic] = []
        self._parsed: List[_ParsedDocument] = []
        self._register_renderers()

    def _register_renderers(self) -> None:
        renderer = getattr(self.md, "renderer", None)
        if renderer is None or renderer.NAME != "html":
            return
        if "book_footnote_ref" not in getattr(renderer, "_book_registered", set()):
            renderer.register("book_footnote_ref", render_book_footnote_ref)
            renderer._book_registered = {"book_footnote_ref"}

    def compile(
        self,
        documents: Sequence[Union[Document, Tuple[str, str]]],
        strict: bool = False,
    ) -> BookResult:
        """Parse and combine all documents.

        :param documents: list of :class:`Document` or ``(id, text)`` tuples
        :param strict: raise :class:`BookError` on any diagnostic instead of
            returning them in the result
        """
        self.diagnostics = []
        parsed = self._prepare_documents(documents)
        self._parsed = parsed

        structural = [d for d in self.diagnostics if d.code == "duplicate-document"]
        if structural:
            raise BookError(structural)

        doc_by_id: Dict[str, _ParsedDocument] = {}
        for parsed_doc in parsed:
            self._scan_document(parsed_doc)
            doc_by_id[unikey(parsed_doc.doc.id)] = parsed_doc
        for parsed_doc in parsed:
            self._check_unknown(parsed_doc, doc_by_id)

        injections = self._build_injections()
        for parsed_doc in parsed:
            ref_links = parsed_doc.env.setdefault("ref_links", {})
            for env_key, data in injections["links"].items():
                ref_links.setdefault(env_key, data)
            ref_footnotes = parsed_doc.env.setdefault("ref_footnotes", {})
            for note_key, text in injections["footnotes"].items():
                ref_footnotes.setdefault(note_key, text)

        allocator = _IdAllocator()
        for parsed_doc in parsed:
            parsed_doc.wrapper_id = allocator.allocate("doc", parsed_doc.doc.id)
            self._expand(parsed_doc)

        items_by_id: Dict[Tuple[str, str], Dict[str, Any]] = {}
        number_counter = 0

        def walk(tokens: List[Dict[str, Any]], owner: _ParsedDocument) -> None:
            nonlocal number_counter
            for token in tokens:
                if token.get("type") == "footnote_ref":
                    raw_key = token["raw"]
                    if raw_key.startswith("@") and ":" in raw_key:
                        target_id_raw, local_key = raw_key[1:].split(":", 1)
                        target_doc = doc_by_id[unikey(target_id_raw)]
                    else:
                        local_key = raw_key
                        target_doc = owner
                    item_key = (unikey(target_doc.doc.id), unikey(local_key))

                    item = items_by_id.get(item_key)
                    if item is None:
                        number_counter += 1
                        children = parse_footnote_item(
                            self.md.block, local_key, number_counter, target_doc.item_state()
                        )["children"]
                        item = {
                            "owner": target_doc,
                            "number": number_counter,
                            "doc_id": target_doc.doc.id,
                            "key": local_key,
                            "block_children": children,
                            "expanded": None,
                            "backrefs": [],
                        }
                        items_by_id[item_key] = item

                    occurrence = owner.count_ref(item_key)
                    ref_id = allocator.allocate(
                        "fnref", owner.doc.id, target_doc.doc.id, local_key, str(occurrence + 1)
                    )
                    item["backrefs"].append({"id": ref_id, "document": owner.doc.id})
                    token["type"] = "book_footnote_ref"
                    token.pop("raw", None)
                    token["attrs"] = {"number": item["number"], "ref_id": ref_id, "item_key": item_key}
                elif "children" in token:
                    walk(token["children"], owner)

        # Bodies, in document order.
        for parsed_doc in parsed:
            body = [t for t in parsed_doc.ast_tokens if t.get("type") != "footnotes"]
            parsed_doc.body_tokens = body
            walk(body, parsed_doc)

        # Expand footnote items with the defining document's scope.  An item
        # may itself cite new footnotes, so repeat until a fixed point.
        pending = [it for it in items_by_id.values() if it["expanded"] is None]
        while pending:
            current = sorted(pending, key=lambda it: it["number"])
            pending = []
            for item in current:
                item["expanded"] = list(
                    self.md._iter_render(item["block_children"], item["owner"].render_state())
                )
                walk(item["expanded"], item["owner"])
            pending = [it for it in items_by_id.values() if it["expanded"] is None]

        item_ids: Dict[Tuple[str, str], str] = {}
        for item in sorted(items_by_id.values(), key=lambda it: it["number"]):
            item_key = (unikey(item["doc_id"]), unikey(item["key"]))
            item_ids[item_key] = allocator.allocate("fn", item["doc_id"], item["key"])
        for parsed_doc in parsed:
            self._resolve_refs(parsed_doc.body_tokens, item_ids)
        for item in items_by_id.values():
            item["item_id"] = item_ids[(unikey(item["doc_id"]), unikey(item["key"]))]
            self._resolve_refs(item["expanded"], item_ids)

        ordered_items = sorted(items_by_id.values(), key=lambda it: it["number"])
        html_parts: List[str] = []
        for parsed_doc in parsed:
            inner = self.md.renderer(parsed_doc.body_tokens, parsed_doc.render_state())
            html_parts.append(
                '<section class="book-document" id="'
                + parsed_doc.wrapper_id
                + '" data-document="'
                + _slug(parsed_doc.doc.id)
                + '">\n'
                + inner
                + "</section>\n"
            )
        if ordered_items:
            html_parts.append(self._render_footnotes(ordered_items))

        result = BookResult(
            html="".join(html_parts),
            diagnostics=list(self.diagnostics),
            documents=[d.doc.id for d in parsed],
        )
        if strict and self.diagnostics:
            raise BookError(self.diagnostics)
        return result

    # -- preparation -------------------------------------------------------

    def _prepare_documents(
        self, documents: Sequence[Union[Document, Tuple[str, str]]]
    ) -> List[_ParsedDocument]:
        parsed: List[_ParsedDocument] = []
        normalized_seen: Dict[str, str] = {}
        for entry in documents:
            doc = entry if isinstance(entry, Document) else Document(entry[0], entry[1])

            if not _DOC_ID_RE.match(doc.id):
                self.diagnostics.append(
                    Diagnostic(
                        "duplicate-document",
                        "invalid document identifier " + repr(doc.id),
                        document=doc.id,
                    )
                )
                continue

            normalized = unikey(doc.id)
            if normalized in normalized_seen:
                self.diagnostics.append(
                    Diagnostic(
                        "duplicate-document",
                        "duplicate document identifier "
                        + repr(doc.id)
                        + " (first seen as "
                        + repr(normalized_seen[normalized])
                        + ")",
                        document=doc.id,
                    )
                )
                continue
            normalized_seen[normalized] = doc.id

            normalized = doc.text.replace("\r\n", "\n").replace("\r", "\n")
            original_src = normalized
            if not original_src.endswith("\n"):
                original_src += "\n"
            src = rewrite_cross_links(normalized)
            if not src.endswith("\n"):
                src += "\n"
            parsed.append(_ParsedDocument(doc, src, original_src))
        return parsed

    def _scan_document(self, parsed_doc: _ParsedDocument) -> None:
        seen_footnotes: Dict[str, Tuple[str, int]] = {}
        seen_links: Dict[str, Tuple[Dict[str, Any], int]] = {}
        for kind, line, key, payload in _iter_definitions(parsed_doc.src):
            if kind == "footnote":
                if key in seen_footnotes and seen_footnotes[key][0] != payload:
                    self.diagnostics.append(
                        Diagnostic(
                            "conflicting-definition",
                            "conflicting duplicate footnote definitions for [^" + key + "]",
                            document=parsed_doc.doc.id,
                            line=line,
                        )
                    )
                seen_footnotes.setdefault(key, (payload, line))
            else:
                previous = seen_links.get(key)
                if previous is not None and (
                    previous[0].get("url") != payload.get("url")
                    or previous[0].get("title") != payload.get("title")
                ):
                    self.diagnostics.append(
                        Diagnostic(
                            "conflicting-definition",
                            "conflicting duplicate link definitions for [" + key + "]",
                            document=parsed_doc.doc.id,
                            line=line,
                        )
                    )
                seen_links.setdefault(key, (payload, line))

        state = self.md.block.state_cls()
        state.process(parsed_doc.src)
        for hook in self.md.before_parse_hooks:
            hook(self.md, state)
        self.md.block.parse(state)
        for hook in self.md.before_render_hooks:
            hook(self.md, state)
        parsed_doc.tokens = state.tokens
        parsed_doc.env = state.env
        parsed_doc.seen_footnotes = seen_footnotes
        parsed_doc.seen_links = seen_links

    def _check_unknown(
        self, parsed_doc: _ParsedDocument, doc_by_id: Dict[str, _ParsedDocument]
    ) -> None:
        footnotes = parsed_doc.env.get("ref_footnotes", {})
        links = parsed_doc.env.get("ref_links", {})
        for kind, line, label, cross_doc, key in _iter_usages(parsed_doc.original_src):
            if cross_doc is not None:
                target = doc_by_id.get(unikey(cross_doc))
                if target is None:
                    self.diagnostics.append(
                        Diagnostic(
                            "unknown-document",
                            "unknown document identifier " + repr(cross_doc) + " in cross reference",
                            document=parsed_doc.doc.id,
                            line=line,
                        )
                    )
                    continue
                defs = target.seen_footnotes if kind == "footnote" else target.seen_links
                if unikey(key) not in defs:
                    code = "unknown-cross-footnote" if kind == "footnote" else "unknown-cross-link"
                    self.diagnostics.append(
                        Diagnostic(
                            code,
                            "cross reference to undefined "
                            + kind
                            + " "
                            + repr(key)
                            + " in document "
                            + repr(cross_doc),
                            document=parsed_doc.doc.id,
                            line=line,
                        )
                    )
                continue

            defs = footnotes if kind == "footnote" else links
            if unikey(key) not in defs:
                code = "unknown-footnote" if kind == "footnote" else "unknown-link"
                ref_text = "[^" + key + "]" if kind == "footnote" else "[" + key + "]"
                self.diagnostics.append(
                    Diagnostic(
                        code,
                        "unresolved " + kind + " reference " + ref_text,
                        document=parsed_doc.doc.id,
                        line=line,
                    )
                )

    def _build_injections(self) -> Dict[str, Dict[str, Any]]:
        footnotes: Dict[str, str] = {}
        links: Dict[str, Dict[str, Any]] = {}
        for parsed_doc in self._parsed:
            doc_id = parsed_doc.doc.id
            for key in parsed_doc.seen_footnotes:
                footnotes[unikey("@" + doc_id + ":" + key)] = parsed_doc.env["ref_footnotes"][key]
            for key, (data, _line) in parsed_doc.seen_links.items():
                links[unikey("@" + doc_id + ":" + key)] = data
        return {"footnotes": footnotes, "links": links}

    # -- rendering ---------------------------------------------------------

    def _expand(self, parsed_doc: _ParsedDocument) -> None:
        parsed_doc.ast_tokens = list(
            self.md._iter_render(list(parsed_doc.tokens), parsed_doc.render_state())
        )

    def _resolve_refs(
        self, tokens: List[Dict[str, Any]], item_ids: Dict[Tuple[str, str], str]
    ) -> None:
        for token in tokens:
            if token.get("type") == "book_footnote_ref":
                item_key = token["attrs"].pop("item_key")
                token["attrs"]["href"] = "#" + item_ids[item_key]
            elif "children" in token:
                self._resolve_refs(token["children"], item_ids)

    def _render_footnotes(self, items: List[Dict[str, Any]]) -> str:
        parts = ['<section class="footnotes">\n<ol>\n']
        for item in items:
            multi = len(item["backrefs"]) > 1
            backref_html = "".join(
                '<a href="#'
                + ref["id"]
                + '" class="footnote"'
                + (
                    ' id="' + item["item_id"] + "-back-" + str(i + 1) + '"'
                    if multi
                    else ""
                )
                + ">&#8617;</a>"
                for i, ref in enumerate(item["backrefs"])
            )
            text = self.md.renderer(item["expanded"], item["owner"].render_state()).rstrip()
            if text.endswith("</p>"):
                text = text[: -4] + backref_html + "</p>"
            else:
                text = text + "\n" + backref_html
            parts.append('<li id="' + item["item_id"] + '">' + text + "</li>\n")
        parts.append("</ol>\n</section>\n")
        return "".join(parts)


def combine_markdown(
    documents: Sequence[Union[Document, Tuple[str, str]]],
    md: Optional["Markdown"] = None,
    strict: bool = False,
) -> BookResult:
    """Convenience wrapper around :class:`MarkdownBook`."""
    return MarkdownBook(md).compile(documents, strict=strict)

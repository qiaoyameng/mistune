"""Pandoc-style citation plugin for mistune.

Citation groups are written inside square brackets, with each citation
starting with an ``@key``.  Examples::

    See [@smith2020] for details.
    See [@smith2020, p. 3] for details.
    As shown [@smith2020, p. 3; -@lee2021].

Bibliographic data is never fetched from the network: the caller provides
a *resolver* callable that receives every unique key in one batch and
returns the corresponding entries.  Unknown keys keep a visible diagnostic
instead of being rendered as if they were known references.

The plugin intentionally implements a plain numeric citation style only;
it is not a complete CSL processor.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Match,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from ..core import BlockState
from ..inline_parser import INLINE_HTML
from ..util import escape as escape_text

if TYPE_CHECKING:
    from ..core import BaseRenderer, InlineState
    from ..inline_parser import InlineParser
    from ..markdown import Markdown

__all__ = [
    "citations",
    "CitationEntry",
    "Citation",
    "CitationGroup",
    "CitationResolver",
    "parse_citation_group",
]

# ---------------------------------------------------------------------------
# Lexer
# ---------------------------------------------------------------------------

_ESCAPEABLE = r"!\"#$%&'()*+,\-./:;<=>?@[\\\]^_`{|}~"

# A citation group starts with "[" and must contain at least one unescaped
# "@".  The first body alternative excludes "@" so that the marker always
# matches the second alternative; square brackets are excluded (no nesting).
# Everything up to (but excluding) the key marker.  The tail allows "]" as
# well: a nested bracket is rejected by the lexer, but letting the regex
# stop at the first "]" would break multi-citation groups sharing brackets.
_BODY_HEAD = r"(?:\\[" + _ESCAPEABLE + r"]|[^@\\\[])*"
_BODY_TAIL = r"(?:\\[" + _ESCAPEABLE + r"]|[^\\\[])*"
INLINE_CITATION = (
    r"\[(?P<citation_body>"
    + _BODY_HEAD
    + r"@(?:"
    + _BODY_TAIL
    + r"))\](?![(\[])"
)

# Valid citation keys. Punctuation is allowed in the middle but terminal
# punctuation is stripped by the caller when it does not fit the character
# class (e.g. "[@smith2020]." leaves the final dot outside the group).
_KEY_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_:\-./#?+<>~&]*")

_SUPPRESS_RE = re.compile(r"^\s*-\s*$")

# Locator grammar: an optional label ("p.", "pages", "§", ...) followed by a
# page/range expression.
_LOCATOR_RE = re.compile(
    r"^(?:(?P<label>p|pp|page|pages|sec|secs|section|sections|ch|chs|chap|chaps|chapter|chapters|para|paras|"
    r"paragraph|paragraphs|vol|vols|volume|volumes|fig|figs|figure|figures|eq|eqs|equation|"
    r"equations|n|nn|note|notes|\u00a7)\s*\.?\s*)?"
    r"(?P<locator>[0-9][0-9A-Za-z]*(?:\s*(?:[-,;]|\u2013|\u2014)\s*[0-9A-Za-z]+)*)\s*$",
    re.IGNORECASE,
)

_LABEL_CANONICAL = {
    "p": "p.",
    "pp": "pp.",
    "page": "p.",
    "pages": "pp.",
    "sec": "sec.",
    "secs": "secs.",
    "section": "sec.",
    "sections": "secs.",
    "ch": "ch.",
    "chs": "chs.",
    "chap": "ch.",
    "chaps": "chs.",
    "chapter": "ch.",
    "chapters": "chs.",
    "para": "\u00b6",
    "paras": "\u00b6\u00b6",
    "paragraph": "\u00b6",
    "paragraphs": "\u00b6\u00b6",
    "vol": "vol.",
    "vols": "vols.",
    "volume": "vol.",
    "volumes": "vols.",
    "fig": "fig.",
    "figs": "figs.",
    "figure": "fig.",
    "figures": "figs.",
    "eq": "eq.",
    "eqs": "eqs.",
    "equation": "eq.",
    "equations": "eqs.",
    "n": "n.",
    "nn": "nn.",
    "note": "n.",
    "notes": "nn.",
    "\u00a7": "\u00a7",
}


@dataclass
class Citation:
    """A single citation inside a citation group.

    :ivar key: citation key, e.g. ``smith2020``
    :ivar prefix: text before the key
    :ivar suffix: text after the key (including any locator), e.g. ``p. 3``
    :ivar suppress_author: ``True`` for ``-@key`` (author suppressed)
    :ivar locator: normalized locator, e.g. ``p. 3``, or ``None``
    """

    key: str
    prefix: str = ""
    suffix: str = ""
    suppress_author: bool = False
    locator: Optional[str] = None


@dataclass
class CitationGroup:
    """The parsed structure of a ``[...]`` citation group."""

    citations: List[Citation] = field(default_factory=list)


def _unescape(text: str) -> str:
    """Remove backslashes that escape punctuation, like CommonMark escapes."""
    return re.sub(r"\\([" + _ESCAPEABLE + r"])", r"\1", text)


def _split_segments(body: str) -> Optional[List[str]]:
    """Split a citation body on unescaped semicolons.

    Returns ``None`` when the body contains an unescaped square bracket,
    since nested citation groups are not allowed.
    """
    segments: List[str] = []
    start = 0
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\":
            i += 2
            continue
        if ch in "[]":
            return None
        if ch == ";":
            segments.append(body[start:i])
            start = i + 1
        i += 1
    segments.append(body[start:])
    return segments


def _find_marker(segment: str) -> Optional[int]:
    """Find the index of the single unescaped ``@`` key marker."""
    marker: Optional[int] = None
    i = 0
    while i < len(segment):
        ch = segment[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "@":
            if marker is not None:
                return None
            marker = i
        i += 1
    return marker


def _strip_affix(text: str) -> str:
    """Trim an affix: whitespace and one leading/trailing comma."""
    stripped = text.strip()
    if stripped.startswith(","):
        stripped = stripped[1:].lstrip()
    if stripped.endswith(","):
        stripped = stripped[:-1].rstrip()
    return stripped.strip()


def _parse_locator(suffix: str) -> Optional[str]:
    text = suffix.strip()
    if not text:
        return ""
    match = _LOCATOR_RE.match(text)
    if not match:
        return None
    locator_value = re.sub(r"\s+", " ", match.group("locator").strip())
    label = match.group("label")
    if label:
        canonical = _LABEL_CANONICAL[label.strip().rstrip(".").lower()]
        return canonical + " " + locator_value
    return "p. " + locator_value


def parse_citation_group(raw: str) -> Optional[CitationGroup]:
    """Parse the text of a citation group (with or without brackets).

    Returns ``None`` when *raw* is not a valid citation group: every segment
    must contain exactly one ``@key`` marker and the key must be valid.
    """
    body = raw.strip()
    if body.startswith("[") and body.endswith("]"):
        body = body[1:-1]

    segments = _split_segments(body)
    if segments is None:
        return None

    group = CitationGroup()
    for segment in segments:
        marker = _find_marker(segment)
        if marker is None:
            return None

        prefix_raw = segment[:marker]
        if _SUPPRESS_RE.match(prefix_raw):
            suppress = True
            prefix = ""
        else:
            suppress = False
            prefix = _unescape(_strip_affix(prefix_raw))

        key_match = _KEY_RE.match(segment, marker + 1)
        if key_match is None:
            return None
        key = _unescape(key_match.group(0))
        suffix_raw = segment[key_match.end() :]
        suffix = _unescape(_strip_affix(suffix_raw))
        locator = _parse_locator(suffix)

        group.citations.append(
            Citation(
                key=key,
                prefix=prefix,
                suffix=suffix,
                suppress_author=suppress,
                locator=locator or None,
            )
        )
    return group


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class CitationEntry(dict):
    """A bibliographic entry returned by a resolver.

    Fields (all optional except a displayable value):

    * ``text`` -- preformatted bibliography text (preferred)
    * ``authors`` -- sequence of author strings
    * ``year`` -- publication year
    * ``title`` -- work title
    * ``container`` -- journal / collection title
    * ``publisher``, ``volume``, ``issue``, ``pages``
    * ``url`` -- URL used for a hyperlinked title
    """


CitationResolver = Callable[[Sequence[str]], Union[Mapping[str, Any], Sequence[Any], None]]


# ---------------------------------------------------------------------------
# Whole-document scanning
# ---------------------------------------------------------------------------

_INLINE_HTML_RE = re.compile(INLINE_HTML)
_INLINE_CITATION_RE = re.compile(INLINE_CITATION)
_BACKTICK_RE = re.compile(r"`+")


def _is_escaped(src: str, pos: int) -> bool:
    """True when the bracket at *pos* is preceded by an odd run of slashes."""
    count = 0
    pos -= 1
    while pos >= 0 and src[pos] == "\\":
        count += 1
        pos -= 1
    return bool(count % 2)


def _find_codespan_end(src: str, start: int, marker: str) -> int:
    """Return the end of the code span opened by *marker* at *start*."""
    pos = start
    length = len(src)
    while pos < length:
        tick = src.find("`", pos)
        if tick < 0:
            return length
        run = tick
        while run < length and src[run] == "`":
            run += 1
        if run - tick == len(marker):
            return run
        pos = run
    return length


def find_citation_groups(src: str) -> Iterable[Tuple[int, int, CitationGroup]]:
    """Yield ``(start, end, group)`` of citation groups in inline *src*.

    Code spans and inline HTML tags are skipped, and brackets that are
    escaped or part of a link are never yielded.
    """
    pos = 0
    length = len(src)
    in_link = False
    while pos < length:
        ch = src[pos]
        if ch == "`":
            tick = _BACKTICK_RE.match(src, pos)
            assert tick is not None
            pos = _find_codespan_end(src, tick.end(), tick.group(0))
            continue
        if ch == "<":
            html_match = _INLINE_HTML_RE.match(src, pos)
            if html_match:
                tag = html_match.group(0)
                if tag.startswith(("<a ", "<a>", "<A ", "<A>")):
                    in_link = True
                elif tag.startswith(("</a ", "</a>", "</A ", "</A>")):
                    in_link = False
                pos = html_match.end()
                continue
        if ch == "[" and not in_link and not _is_escaped(src, pos):
            candidate = _INLINE_CITATION_RE.match(src, pos)
            if candidate is not None:
                end = candidate.end()
                if end >= length or src[end] not in "([":
                    group = parse_citation_group(candidate.group("citation_body"))
                    if group is not None:
                        yield pos, end, group
                    pos = end
                    continue
        pos += 1


_RAW_BLOCK_TYPES = frozenset({"block_code", "block_html", "code_block"})


def _iter_inline_texts(tokens: Iterable[Dict[str, Any]]) -> Iterable[str]:
    for token in tokens:
        token_type = token.get("type")
        if token_type in _RAW_BLOCK_TYPES:
            continue
        if "text" in token:
            yield token["text"]
        children = token.get("children")
        if children:
            yield from _iter_inline_texts(children)


def collect_citation_keys(state: "BlockState") -> List[str]:
    """Collect unique citation keys in document order."""
    keys: List[str] = []
    seen: set[str] = set()
    sources: List[str] = []
    sources.extend(_iter_inline_texts(state.tokens))
    ref_footnotes = state.env.get("ref_footnotes")
    if ref_footnotes:
        sources.extend(ref_footnotes.values())
    for text in sources:
        for _start, _end, group in find_citation_groups(text):
            for citation in group.citations:
                if citation.key not in seen:
                    seen.add(citation.key)
                    keys.append(citation.key)
    return keys


# ---------------------------------------------------------------------------
# Entry normalization
# ---------------------------------------------------------------------------


def normalize_entry(value: Any) -> Optional[Dict[str, Any]]:
    """Normalize one resolver value into an entry mapping, or ``None``."""
    if value is None:
        return None
    if isinstance(value, str):
        return {"text": value}
    if isinstance(value, Mapping) and value:
        entry = dict(value)
        if any(entry.get(field) for field in ("text", "title", "authors")):
            return entry
    return None


def entry_text(entry: Mapping[str, Any]) -> str:
    """Compose plain (already-escaped by the renderer) bibliography text."""
    if entry.get("text"):
        return str(entry["text"])
    parts: List[str] = []
    authors = entry.get("authors")
    if authors:
        parts.append(", ".join(str(author) for author in authors))
    title = entry.get("title")
    if title:
        parts.append(str(title))
    container = entry.get("container")
    meta: List[str] = []
    if entry.get("publisher"):
        meta.append(str(entry["publisher"]))
    volume = entry.get("volume")
    issue = entry.get("issue")
    if volume and issue:
        meta.append(str(volume) + "(" + str(issue) + ")")
    elif volume:
        meta.append(str(volume))
    if entry.get("pages"):
        meta.append(str(entry["pages"]))
    year = entry.get("year")
    if year:
        meta.append(str(year))
    if container:
        if meta:
            parts.append(str(container) + ", " + ", ".join(meta))
        else:
            parts.append(str(container))
    elif meta:
        parts.append(", ".join(meta))
    cleaned = [part.rstrip(".") for part in parts if part]
    return ". ".join(cleaned) + ("." if cleaned else "")


def key_slug(key: str) -> str:
    """Turn a citation key into a unique, URL-safe id fragment."""
    out = ["bib-"]
    for ch in key:
        if ch.isascii() and (ch.isalnum() or ch == "_"):
            out.append(ch)
        else:
            out.append("-" + format(ord(ch), "X") + "-")
    return "".join(out)


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------


def _reset_env(env: Dict[str, Any]) -> None:
    env["cite_keys"] = []
    env["cite_entries"] = {}
    env["cite_occurrences"] = {}


def before_parse_citations(md: "Markdown", state: "BlockState") -> None:
    _reset_env(state.env)


def before_render_citations(
    resolver: Optional[CitationResolver],
    md: "Markdown",
    state: "BlockState",
) -> None:
    keys = collect_citation_keys(state)
    state.env["cite_keys"] = keys
    state.env["cite_occurrences"] = {key: 0 for key in keys}
    entries: Dict[str, Optional[Dict[str, Any]]] = {key: None for key in keys}
    if resolver is not None and keys:
        try:
            resolved = resolver(list(keys))
        except Exception:
            resolved = None
        if isinstance(resolved, Mapping):
            for key in keys:
                entries[key] = normalize_entry(resolved.get(key))
        elif resolved is not None:
            for key, value in zip(keys, resolved):
                entries[key] = normalize_entry(value)
    state.env["cite_entries"] = entries


def after_render_citations(
    md: "Markdown",
    result: Union[str, List[Dict[str, Any]]],
    state: "BlockState",
) -> Union[str, List[Dict[str, Any]]]:
    keys: List[str] = state.env.get("cite_keys") or []
    if not keys:
        return result
    entries: Dict[str, Optional[Dict[str, Any]]] = state.env.get("cite_entries") or {}
    known = [key for key in keys if entries.get(key)]
    if not known:
        return result

    children = [
        {
            "type": "bib_entry",
            "text": entry_text(entries[key] or {}),
            "attrs": {
                "key": key,
                "index": position,
                "url": (entries[key] or {}).get("url"),
                "backrefs": state.env.get("cite_occurrences", {}).get(key, 0),
            },
        }
        for position, key in enumerate(known, start=1)
    ]
    token = {"type": "bib_section", "children": children}

    if isinstance(result, list):
        return result + [token]

    block_state = BlockState(parent=state)
    block_state.tokens = [token]
    return result + str(md.render_state(block_state))


# ---------------------------------------------------------------------------
# Inline parsing
# ---------------------------------------------------------------------------


def parse_citation(inline: "InlineParser", m: Match[str], state: "InlineState") -> Optional[int]:
    if state.in_link or state.in_image:
        return None
    body = m.group("citation_body")
    group = parse_citation_group(body)
    if group is None:
        return None

    keys: List[str] = state.env.get("cite_keys") or []
    entries: Dict[str, Optional[Dict[str, Any]]] = state.env.get("cite_entries") or {}
    occurrences: Dict[str, int] = state.env.get("cite_occurrences") or {}
    index_map = {key: position for position, key in enumerate(keys, start=1)}

    children: List[Dict[str, Any]] = []
    for position, citation in enumerate(group.citations):
        if position:
            children.append({"type": "cite_separator", "raw": "; "})
        if citation.prefix:
            children.append({"type": "cite_prefix", "raw": citation.prefix + " "})
        entry = entries.get(citation.key)
        if not entry or citation.key not in index_map:
            children.append(
                {
                    "type": "cite_ref_unknown",
                    "raw": ("-@" if citation.suppress_author else "@") + citation.key,
                    "attrs": {"key": citation.key},
                }
            )
        else:
            occurrences[citation.key] = occurrences.get(citation.key, 0) + 1
            children.append(
                {
                    "type": "cite_ref",
                    "raw": "",
                    "attrs": {
                        "key": citation.key,
                        "index": index_map[citation.key],
                        "locator": citation.locator,
                        "suffix": citation.suffix,
                        "suppress_author": citation.suppress_author,
                        "occurrence": occurrences[citation.key],
                    },
                }
            )
    state.append_token({"type": "cite_group", "children": children})
    return m.end()


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def render_cite_group(renderer: "BaseRenderer", text: str) -> str:
    return '<sup class="citation-group">[' + text + "]</sup>"


def render_cite_prefix(renderer: "BaseRenderer", text: str) -> str:
    return escape_text(text)


def render_cite_separator(renderer: "BaseRenderer", text: str) -> str:
    return "; "


def render_cite_ref_unknown(renderer: "BaseRenderer", text: str, key: str) -> str:
    # the surrounding cite_group already supplies the brackets
    return '<span class="citation-missing" title="unknown citation key">' + escape_text(text) + "</span>"


def render_cite_ref(
    renderer: "BaseRenderer",
    text: str,
    key: str,
    index: int,
    locator: Optional[str] = None,
    suffix: str = "",
    suppress_author: bool = False,
    occurrence: int = 1,
) -> str:
    slug = key_slug(key)
    cls = "citation-ref" + (" citation-suppress" if suppress_author else "")
    html = '<a class="' + cls + '" href="#' + slug + '" id="' + slug + "-ref-" + str(occurrence) + '">'
    html += str(index)
    if locator:
        html += ", " + escape_text(locator)
    elif suffix:
        html += ", " + escape_text(suffix)
    html += "</a>"
    return html


def render_bib_section(renderer: "BaseRenderer", text: str) -> str:
    return (
        '<section class="bibliography">\n'
        "<h2>References</h2>\n"
        '<ol>\n' + text + "</ol>\n"
        "</section>\n"
    )


def render_bib_entry(
    renderer: "BaseRenderer",
    text: str,
    key: str,
    index: int,
    url: Optional[str] = None,
    backrefs: int = 0,
) -> str:
    slug = key_slug(key)
    back_links = []
    for occurrence in range(1, max(backrefs, 0) + 1):
        back_links.append(
            '<a href="#' + slug + "-ref-" + str(occurrence) + '" class="bib-backref" title="jump back">&#8617;</a>'
        )
    back_html = (" " + " ".join(back_links)) if back_links else ""
    return '<li id="' + slug + '">' + text + back_html + "</li>\n"


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------


def citations(resolver: Optional[CitationResolver] = None) -> Callable[["Markdown"], None]:
    """Create a plugin for pandoc-style numeric citations.

    .. code-block:: text

        See [@smith2020, p. 3] and [-@lee2021].

    *resolver* is called once per document with the ordered list of unique
    citation keys.  It may return:

    * a mapping of ``key -> entry``, or
    * a sequence aligned with the requested keys,

    where each entry is a plain string or a mapping with fields such as
    ``text``, ``authors``, ``year``, ``title``, ``container``, ``publisher``,
    ``volume``, ``issue``, ``pages`` or ``url``.  Entries for unknown keys
    may be omitted, ``None`` or an empty mapping; such keys keep a visible
    "missing citation" marker and are excluded from the bibliography.

    The resolver performs no network access itself; this plugin never does
    either.  Numbering follows the order of first citation, repeated keys
    share a number but keep their own locator.

    :param resolver: batch bibliographic resolver
    """

    def plugin(md: "Markdown") -> None:
        md.inline.register("citation", INLINE_CITATION, parse_citation, before="link")
        md.before_parse_hooks.append(before_parse_citations)
        md.before_render_hooks.append(lambda markdown, state: before_render_citations(resolver, markdown, state))
        md.after_render_hooks.append(after_render_citations)

        if md.renderer and md.renderer.NAME == "html":
            md.renderer.register("cite_group", render_cite_group)
            md.renderer.register("cite_prefix", render_cite_prefix)
            md.renderer.register("cite_separator", render_cite_separator)
            md.renderer.register("cite_ref", render_cite_ref)
            md.renderer.register("cite_ref_unknown", render_cite_ref_unknown)
            md.renderer.register("bib_section", render_bib_section)
            md.renderer.register("bib_entry", render_bib_entry)

    return plugin

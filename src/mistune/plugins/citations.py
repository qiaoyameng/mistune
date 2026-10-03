import re
import string
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Mapping, Match, Optional, Tuple, Union
from urllib.parse import quote

from ..core import BlockState
from ..util import escape

if TYPE_CHECKING:
    from ..core import BaseRenderer, InlineState
    from ..inline_parser import InlineParser
    from ..markdown import Markdown

__all__ = ["citations"]

#: a citation key starts with a letter, digit or underscore, and may
#: further contain any of these characters (pandoc style citekeys)
_KEY_START_CHARS = set(string.ascii_letters + string.digits + "_")
_KEY_CHARS = _KEY_START_CHARS | set("-:.#$%&+?<>~/")

#: backslash escapes: a backslash before ASCII punctuation quotes that
#: character, so ``\;`` never splits items and ``\]`` never closes a citation
_ESCAPABLE_CHARS = set(string.punctuation)
_ESCAPE_RE = re.compile(r"\\([" + re.escape(string.punctuation) + r"])")

#: locator at the start of a suffix, e.g. "p. 3", "pp. 3-5", "ch. 2"
_LOCATOR_RE = re.compile(
    r"(?P<label>p{1,2}|ch|sec|vol|fig|no|§|¶)\.?[ \t]+(?P<locator>[^\s,;]+)(?P<rest>[\s\S]*)$",
    re.IGNORECASE,
)

#: characters kept as-is in generated HTML ids; everything else is
#: percent-encoded so ids stay valid and collisions are impossible
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_\-.:]+$")

#: trigger pattern: a ``[`` (not part of an image) with an ``@`` later on
#: the same line; full validation happens in ``parse_citation``
CITATION = r"(?<!!)\[(?=[^\n]*@)"

CitationEntry = Union[str, Mapping[str, Any], None]
CitationResolver = Callable[[List[str]], Mapping[str, CitationEntry]]


def _key_id(key: str) -> str:
    """Encode a citation key for use in HTML ids and fragment links."""
    if _SAFE_ID_RE.match(key):
        return key
    return quote(key, safe="_-.:")


def _unescape(text: str) -> str:
    return _ESCAPE_RE.sub(lambda m: m.group(1), text)


def _scan_bracket(src: str, pos: int) -> Tuple[Optional[str], Optional[int]]:
    """Scan from ``pos`` (just after ``[``) to the matching unescaped ``]``.

    Returns the raw inner content (escapes preserved) and the position just
    after the closing bracket, or ``(None, None)`` if there is none.
    Balanced nested brackets are allowed inside the content.
    """
    depth = 1
    buf: List[str] = []
    i = pos
    n = len(src)
    while i < n:
        c = src[i]
        if c == "\\" and i + 1 < n and src[i + 1] in _ESCAPABLE_CHARS:
            buf.append(src[i : i + 2])
            i += 2
            continue
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return "".join(buf), i + 1
        buf.append(c)
        i += 1
    return None, None


def _split_items(content: str) -> List[str]:
    """Split bracket content into items on top-level unescaped semicolons."""
    items = []
    depth = 0
    buf: List[str] = []
    i = 0
    n = len(content)
    while i < n:
        c = content[i]
        if c == "\\" and i + 1 < n and content[i + 1] in _ESCAPABLE_CHARS:
            buf.append(content[i : i + 2])
            i += 2
            continue
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
        if c == ";" and depth == 0:
            items.append("".join(buf))
            buf = []
        else:
            buf.append(c)
        i += 1
    items.append("".join(buf))
    return items


def _find_marker(text: str) -> Optional[Tuple[int, bool, int]]:
    """Find the ``@key`` marker in an item.

    Returns ``(at_pos, suppress_author, prefix_end)`` or ``None``. The ``@``
    must appear at the start of the item, after whitespace, or after a ``-``
    (suppress-author flag) that is itself at a boundary, so addresses like
    ``foo@bar`` are never treated as keys.
    """
    depth = 0
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\\" and i + 1 < n and text[i + 1] in _ESCAPABLE_CHARS:
            i += 2
            continue
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
        elif c == "@" and depth == 0:
            prev = text[i - 1] if i > 0 else None
            if prev is None or prev in " \t\n":
                return i, False, i
            if prev == "-" and (i < 2 or text[i - 2] in " \t\n"):
                return i, True, i - 1
        i += 1
    return None


def _parse_suffix(text: str) -> Tuple[str, str, str]:
    """Split raw suffix text into ``(locator_label, locator, suffix)``."""
    suffix = _unescape(text).strip()
    if suffix.startswith(","):
        suffix = suffix[1:].strip()
    m = _LOCATOR_RE.match(suffix)
    if not m:
        return "", "", suffix
    label = m.group("label").lower()
    locator = m.group("locator")
    rest = m.group("rest").strip()
    if rest.startswith(","):
        rest = rest[1:].strip()
    return label, locator, rest


def _parse_item(text: str) -> Optional[Dict[str, Any]]:
    marker = _find_marker(text)
    if marker is None:
        return None
    at_pos, suppress_author, prefix_end = marker

    start = at_pos + 1
    if start >= len(text) or text[start] not in _KEY_START_CHARS:
        return None
    end = start + 1
    while end < len(text) and text[end] in _KEY_CHARS:
        end += 1

    label, locator, suffix = _parse_suffix(text[end:])
    return {
        "key": text[start:end],
        "prefix": _unescape(text[:prefix_end]).strip(),
        "suffix": suffix,
        "locator": locator,
        "locator_label": label,
        "suppress_author": suppress_author,
    }


def _parse_items(content: str) -> Optional[List[Dict[str, Any]]]:
    items = []
    for part in _split_items(content):
        if not part.strip():
            continue
        item = _parse_item(part)
        if item is None:
            return None
        items.append(item)
    return items or None


def parse_citation(inline: "InlineParser", m: Match[str], state: "InlineState") -> Optional[int]:
    if state.in_link:
        # never recognize citations inside link text
        state.append_token({"type": "text", "raw": m.group(0)})
        return m.end()

    content, end_pos = _scan_bracket(state.src, m.end())
    if content is None or end_pos is None:
        return inline.parse_link(m, state)

    if end_pos < len(state.src) and state.src[end_pos] in "([":
        # a link or image destination follows, e.g. ``[@key](url)``
        return inline.parse_link(m, state)

    items = _parse_items(content)
    if not items:
        return inline.parse_link(m, state)

    indexes = state.env.get("citation_indexes")
    if indexes is None:
        indexes = {}
        state.env["citation_keys"] = []
        state.env["citation_indexes"] = indexes
        state.env["citation_occurrences"] = {}
    keys = state.env["citation_keys"]
    occurrences = state.env["citation_occurrences"]

    for item in items:
        key = item["key"]
        if key not in indexes:
            keys.append(key)
            indexes[key] = len(keys)  # 1-based, in first-citation order
            occurrences[key] = 0
        occurrences[key] += 1
        item["index"] = indexes[key]
        item["occurrence"] = occurrences[key]

    state.append_token(
        {
            "type": "citation",
            "raw": state.src[m.start() : end_pos],
            "attrs": {"items": items},
        }
    )
    return end_pos


def _normalize_entry(entry: CitationEntry) -> Optional[str]:
    if entry is None:
        return None
    if isinstance(entry, str):
        return entry
    if isinstance(entry, Mapping):
        text = entry.get("text")
        return None if text is None else str(text)
    return str(entry)


def render_citation(renderer: "BaseRenderer", text: str, items: List[Dict[str, Any]]) -> str:
    parts = []
    for item in items:
        key_id = _key_id(item["key"])
        seg = '<a href="#ref-' + key_id + '">' + str(item["index"]) + "</a>"
        tail = []
        if item["locator"]:
            label = item["locator_label"]
            sep = " " if label in ("§", "¶") else ". "
            tail.append(escape(label) + sep + escape(item["locator"]))
        if item["suffix"]:
            tail.append(escape(item["suffix"]))
        if tail:
            seg += ", " + ", ".join(tail)
        if item["prefix"]:
            seg = escape(item["prefix"]) + " " + seg
        parts.append('<span id="cite-' + key_id + "-" + str(item["occurrence"]) + '">' + seg + "</span>")
    return '<span class="citation">[' + "; ".join(parts) + "]</span>"


def render_bibliography(renderer: "BaseRenderer", text: str) -> str:
    return '<section class="bibliography">\n<ol>\n' + text + "</ol>\n</section>\n"


def render_bibliography_item(
    renderer: "BaseRenderer",
    key: str,
    index: int,
    text: Optional[str],
    known: bool,
    occurrences: int,
) -> str:
    key_id = _key_id(key)
    if known and text:
        body = escape(text)
    else:
        body = '<span class="citation-unknown">Unresolved citation: @' + escape(key) + "</span>"
    backlinks = "".join(
        '<a class="citation-backref" href="#cite-' + key_id + "-" + str(i) + '">&#8617;</a>'
        for i in range(1, occurrences + 1)
    )
    if backlinks:
        body += " " + backlinks
    return '<li id="ref-' + key_id + '">' + body + "</li>\n"


def citations(md: "Markdown", resolver: Optional[CitationResolver] = None) -> None:
    r"""A mistune plugin to support pandoc style citations for research
    notes, for example::

        A claim [@smith2020, p. 3; -@lee2021], see also [see @wu2022, ch. 2].

    Each item in a bracket holds an optional prefix, a citation key, an
    optional ``-`` suppress-author flag, and an optional suffix which may
    start with a locator (``p.``, ``pp.``, ``ch.``, ``sec.``, ``vol.``,
    ``fig.``, ``no.``, ``§``, ``¶``). Items are separated by ``;`` and a
    backslash escapes punctuation, so ``\;``, ``\,``, ``\[``, ``\]`` and
    ``\@`` never act as delimiters. Balanced nested brackets are allowed
    in prefixes and suffixes.

    The plugin never accesses the network or fabricates references. All
    unique keys are collected while parsing, then resolved in a single
    batch call to ``resolver``::

        def resolver(keys):
            return {key: lookup(key) for key in keys}

        md = create_markdown(plugins=[partial(citations, resolver=resolver)])

    ``resolver`` receives the list of unique keys in first-citation order
    and returns a mapping of key to a formatted reference string (or a
    mapping with a ``"text"`` item). Keys missing from the result are
    rendered as visible "unresolved" diagnostics. References are numbered
    and ordered by first citation; repeated citations of a key share its
    number while keeping their own locators and back-links.

    :param md: Markdown instance
    :param resolver: batch callback turning citation keys into references
    """

    def md_citations_hook(
        md: "Markdown", result: Union[str, List[Dict[str, Any]]], state: BlockState
    ) -> Union[str, List[Dict[str, Any]]]:
        keys = state.env.get("citation_keys")
        if not keys:
            return result

        entries: Mapping[str, CitationEntry] = resolver(list(keys)) if resolver else {}
        occurrences = state.env["citation_occurrences"]
        children = []
        for i, key in enumerate(keys):
            text = _normalize_entry(entries.get(key))
            children.append(
                {
                    "type": "bibliography_item",
                    "attrs": {
                        "key": key,
                        "index": i + 1,
                        "text": text,
                        "known": text is not None,
                        "occurrences": occurrences[key],
                    },
                }
            )
        bib_state = BlockState(parent=state)
        bib_state.tokens = [{"type": "bibliography", "children": children}]
        output = md.render_state(bib_state)
        return result + output  # type: ignore[operator]

    md.inline.register("citation", CITATION, parse_citation, before="link")
    md.after_render_hooks.append(md_citations_hook)

    if md.renderer and md.renderer.NAME == "html":
        md.renderer.register("citation", render_citation)
        md.renderer.register("bibliography", render_bibliography)
        md.renderer.register("bibliography_item", render_bibliography_item)

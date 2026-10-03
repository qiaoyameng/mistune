import re

from mistune import create_markdown
from mistune.plugins.citations import (
    Citation,
    citations,
    key_slug,
    parse_citation_group,
)

ENTRIES = {
    "smith2020": {"text": "Smith, J. (2020). A book. Publisher."},
    "lee2021": {
        "authors": ["Lee, K.", "Wang, Y."],
        "year": 2021,
        "title": "A paper",
        "container": "Journal X",
        "volume": 5,
        "issue": 2,
        "pages": "10-20",
        "url": "https://example.org/lee2021",
    },
    "doe-99": "Doe, A. (1999). An old report.",
}


def make_resolver(entries=ENTRIES):
    calls = []

    def resolver(keys):
        calls.append(list(keys))
        return {key: entries.get(key) for key in keys}

    resolver.calls = calls
    return resolver


def make_md(entries=None, resolver=None):
    if resolver is None:
        resolver = make_resolver(ENTRIES if entries is None else entries)
    md = create_markdown(escape=False, plugins=[citations(resolver)])
    return md, resolver


# ---------------------------------------------------------------------------
# Lexer
# ---------------------------------------------------------------------------


class TestLexer:
    def test_simple_key(self):
        group = parse_citation_group("[@smith2020]")
        assert group is not None
        assert group.citations == [Citation(key="smith2020")]

    def test_prefix_suffix_and_locator(self):
        group = parse_citation_group("[see @smith2020, p. 3]")
        citation = group.citations[0]
        assert citation.key == "smith2020"
        assert citation.prefix == "see"
        assert citation.suffix == "p. 3"
        assert citation.locator == "p. 3"
        assert citation.suppress_author is False

    def test_suppress_author(self):
        group = parse_citation_group("[-@lee2021]")
        citation = group.citations[0]
        assert citation.key == "lee2021"
        assert citation.suppress_author is True
        assert citation.prefix == ""

    def test_semicolon_separates_citations(self):
        group = parse_citation_group("[@smith2020, p. 3; -@lee2021]")
        assert [c.key for c in group.citations] == ["smith2020", "lee2021"]
        assert group.citations[0].locator == "p. 3"
        assert group.citations[1].suppress_author is True

    def test_comma_is_affix_not_separator(self):
        # a comma only trims the single boundary comma of an affix
        citation = parse_citation_group("[@a, p. 5,]").citations[0]
        assert citation.suffix == "p. 5"
        group = parse_citation_group("[compare, @a; @b]")
        assert group.citations[0].prefix == "compare"
        assert group.citations[1].key == "b"

    def test_escaped_semicolon(self):
        group = parse_citation_group(r"[@a;b\;c @b]")
        assert len(group.citations) == 2
        assert group.citations[0].key == "a"

    def test_escaped_at_sign(self):
        # the escaped @ is not a marker, so there is exactly one key
        group = parse_citation_group(r"[a\@b @key]")
        assert group is not None
        assert group.citations[0].key == "key"
        assert group.citations[0].prefix == "a@b"

    def test_escaped_brackets_inside(self):
        group = parse_citation_group(r"[@a, see x\[1\]]")
        assert group.citations[0].suffix == r"see x[1]"

    def test_nested_brackets_rejected(self):
        assert parse_citation_group("[@a [@b]]") is None

    def test_double_marker_rejected(self):
        assert parse_citation_group("[@@a]") is None
        assert parse_citation_group("[@a@b]") is None

    def test_missing_marker_rejected(self):
        assert parse_citation_group("[plain text]") is None
        assert parse_citation_group("[a;b]") is None

    def test_locator_labels(self):
        assert parse_citation_group("[@a, pp. 10-12]").citations[0].locator == "pp. 10-12"
        assert parse_citation_group("[@a, section 2]").citations[0].locator == "sec. 2"
        assert parse_citation_group("[@a, chap. 4]").citations[0].locator == "ch. 4"
        assert parse_citation_group("[@a, § 7]").citations[0].locator == "\u00a7 7"
        assert parse_citation_group("[@a, 12]").citations[0].locator == "p. 12"

    def test_freeform_suffix_kept(self):
        citation = parse_citation_group("[@a, especially the intro]").citations[0]
        assert citation.suffix == "especially the intro"
        assert citation.locator is None

    def test_key_punctuation(self):
        citation = parse_citation_group("[@doe-99]").citations[0]
        assert citation.key == "doe-99"

    def test_accepts_without_brackets(self):
        group = parse_citation_group("@smith2020, p. 3")
        assert group.citations[0].locator == "p. 3"


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------


class TestHTMLRendering:
    def test_basic_citation_and_bibliography(self):
        md, resolver = make_md()
        html = md("See [@smith2020, p. 3] for details.")
        assert 'href="#bib-smith2020"' in html
        assert ">1, p. 3</a>" in html
        assert '<li id="bib-smith2020">' in html
        assert resolver.calls == [["smith2020"]]

    def test_resolver_called_once_with_unique_keys(self):
        md, resolver = make_md()
        md("[@smith2020] [@lee2021] [@smith2020] [@lee2021]")
        assert resolver.calls == [["smith2020", "lee2021"]]

    def test_numbering_follows_first_citation_order(self):
        md, _ = make_md()
        html = md("[@lee2021] then [@smith2020]")
        assert re.search(r"bib-lee2021-ref-1\">1<", html)
        assert re.search(r"bib-smith2020-ref-1\">2<", html)
        assert html.index('id="bib-lee2021"') < html.index('id="bib-smith2020"')

    def test_shared_number_distinct_locators(self):
        md, _ = make_md()
        html = md("[@smith2020, p. 1] ... [@smith2020, pp. 9-10]")
        assert html.count('id="bib-smith2020-ref-1"') == 1
        assert html.count('id="bib-smith2020-ref-2"') == 1
        assert ">1, p. 1</a>" in html
        assert ">1, pp. 9-10</a>" in html
        # two in-text links and two back-links
        assert html.count('id="bib-smith2020-ref-') == 2
        assert html.count('class="bib-backref"') == 2

    def test_suppress_author_class(self):
        md, _ = make_md()
        html = md("[-@lee2021]")
        assert "citation-suppress" in html

    def test_unknown_key_visible_diagnostic(self):
        md, resolver = make_md()
        html = md("[@ghost]")
        assert "citation-missing" in html
        assert "@ghost" in html
        assert "<section" not in html
        assert resolver.calls == [["ghost"]]

    def test_no_fabricated_entry_for_unknown(self):
        md, _ = make_md()
        html = md("[@smith2020] and [@ghost]")
        assert '<li id="bib-ghost"' not in html
        assert '<li id="bib-smith2020">' in html

    def test_codespan_not_recognized(self):
        md, _ = make_md()
        html = md("use `[@smith2020]` here")
        assert "citation-ref" not in html
        assert "<code>[@smith2020]</code>" in html
        assert "<section" not in html

    def test_fenced_code_not_recognized(self):
        md, _ = make_md()
        html = md("```\n[@smith2020]\n```\n")
        assert "citation-ref" not in html
        assert "<section" not in html

    def test_direct_link_not_recognized(self):
        md, _ = make_md()
        html = md("[@smith2020](https://example.org)")
        assert "citation-ref" not in html
        assert "<section" not in html
        assert 'href="https://example.org"' in html

    def test_reference_link_not_recognized(self):
        md, _ = make_md()
        html = md("[@smith2020][r]\n\n[r]: https://example.org\n")
        assert "citation-ref" not in html
        assert 'href="https://example.org"' in html

    def test_link_followed_by_citation(self):
        md, _ = make_md()
        html = md("[text](https://x.org) and [@smith2020]")
        assert 'href="https://x.org"' in html
        assert 'href="#bib-smith2020"' in html

    def test_escaped_brackets_are_text(self):
        md, _ = make_md()
        html = md(r"\[@smith2020\]")
        assert "citation-ref" not in html
        assert "[@smith2020]" in html
        assert "<section" not in html

    def test_html_anchor_interior_not_recognized(self):
        md, _ = make_md()
        html = md('<a href="x">[@smith2020]</a> [@lee2021]')
        assert html.count("citation-ref") == 1
        assert 'id="bib-lee2021"' in html

    def test_multi_citation_group(self):
        md, _ = make_md()
        html = md("[@smith2020, p. 3; -@lee2021]")
        assert ">1, p. 3</a>" in html
        assert "citation-suppress" in html and ">2</a>" in html

    def test_id_escaping_and_collision_free(self):
        entries = {"a-b": "dash", "a/b": "slash", "a:b": "colon"}
        md, _ = make_md(entries)
        html = md("[@a-b] [@a/b] [@a:b]")
        assert 'id="bib-a-2D-b"' in html
        assert 'id="bib-a-2F-b"' in html
        assert 'id="bib-a-3A-b"' in html

    def test_entry_url_hyperlink_is_not_required(self):
        md, _ = make_md()
        html = md("[@lee2021]")
        # url stored on token/entry but plain numeric style renders text only
        assert "Journal X" in html

    def test_environment_resets_between_documents(self):
        md, resolver = make_md()
        md("[@smith2020] [@lee2021]")
        md("[@lee2021]")
        assert resolver.calls[1] == ["lee2021"]
        html = md("[@lee2021]")
        assert ">1</a>" in html

    def test_string_entries(self):
        md, _ = make_md()
        html = md("[@doe-99]")
        assert "Doe, A. (1999). An old report." in html
        assert 'id="bib-doe-2D-99"' in html


# ---------------------------------------------------------------------------
# AST
# ---------------------------------------------------------------------------


class TestAST:
    def test_ast_contains_structured_citation(self):
        md = create_markdown(renderer="ast", plugins=[citations(lambda k: {"smith2020": {"text": "T"}})])
        tokens = md("[@smith2020, p. 3]")
        paragraph = tokens[0]
        cite = paragraph["children"][0]
        assert cite["type"] == "cite_group"
        ref = cite["children"][0]
        assert ref["type"] == "cite_ref"
        attrs = ref["attrs"]
        assert attrs["key"] == "smith2020"
        assert attrs["index"] == 1
        assert attrs["locator"] == "p. 3"
        assert attrs["suppress_author"] is False
        # bibliography is available unrendered too
        bib = tokens[-1]
        assert bib["type"] == "bib_section"
        assert bib["children"][0]["attrs"]["key"] == "smith2020"

    def test_ast_unknown_key(self):
        md = create_markdown(renderer="ast", plugins=[citations(lambda k: {})])
        tokens = md("[@ghost]")
        ref = tokens[0]["children"][0]["children"][0]
        assert ref["type"] == "cite_ref_unknown"
        assert ref["attrs"]["key"] == "ghost"
        assert tokens[-1]["type"] != "bib_section"

    def test_ast_suppress_flag(self):
        md = create_markdown(renderer="ast", plugins=[citations(lambda k: {"a": "A"})])
        ref = md("[-@a]")[0]["children"][0]["children"][0]
        assert ref["attrs"]["suppress_author"] is True


# ---------------------------------------------------------------------------
# Resolver protocols
# ---------------------------------------------------------------------------


class TestResolver:
    def test_mapping_result(self):
        md = create_markdown(escape=False, plugins=[citations(lambda keys: {"a": "Entry A"})])
        html = md("[@a]")
        assert "Entry A" in html

    def test_sequence_result(self):
        md = create_markdown(escape=False, plugins=[citations(lambda keys: ["Entry " + key for key in keys])])
        html = md("[@alpha] [@beta]")
        assert "Entry alpha" in html and "Entry beta" in html

    def test_none_result_marks_all_unknown(self):
        md = create_markdown(escape=False, plugins=[citations(lambda keys: None)])
        html = md("[@a]")
        assert "citation-missing" in html

    def test_no_resolver(self):
        md = create_markdown(escape=False, plugins=[citations()])
        html = md("[@a]")
        assert "citation-missing" in html
        assert "<section" not in html

    def test_resolver_exception_is_safe(self):
        def broken(keys):
            raise RuntimeError("boom")

        md = create_markdown(escape=False, plugins=[citations(broken)])
        html = md("[@a]")
        assert "citation-missing" in html


def test_key_slug_roundtrip_and_collision():
    pairs = {key_slug("a-b"), key_slug("a/b"), key_slug("a:b"), key_slug("a.b")}
    assert len(pairs) == 4
    assert re.fullmatch(r"bib-[A-Za-z0-9_-]+", key_slug("a.b:c"))

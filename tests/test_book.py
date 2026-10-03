import re
from unittest import TestCase

import mistune
from mistune import (
    BookError,
    Document,
    MarkdownBook,
    combine_markdown,
)


def _sup_refs(html):
    return re.findall(
        r'<sup class="footnote-ref" id="([^"]+)"><a href="([^"]+)">(\d+)</a></sup>',
        html,
    )


def _footnote_items(html):
    return re.findall(r'<li id="([^"]+)">(.*?)</li>', html, re.S)


def _backlinks(html):
    return re.findall(
        r'<a href="(#[^"]+)" class="footnote"(?: id="([^"]+)")?>&#8617;</a>',
        html,
    )


class TestCombineBasics(TestCase):
    def test_local_references_resolve_in_own_scope(self):
        a = "A uses [home].\n\n[home]: /a-home\n[^1]: unused note\n"
        b = "B uses [home].\n\n[home]: /b-home\n"
        result = combine_markdown([("a", a), ("b", b)])
        self.assertIn('href="/a-home"', result.html)
        self.assertIn('href="/b-home"', result.html)
        # uncited footnote definition is never rendered
        self.assertNotIn("unused note", result.html)
        self.assertEqual(result.documents, ["a", "b"])
        self.assertEqual(result.diagnostics, [])

    def test_accepts_document_objects(self):
        result = MarkdownBook().compile([Document("d", "hello\n")])
        self.assertEqual(result.documents, ["d"])
        self.assertIn("hello", result.html)

    def test_wraps_each_document_with_stable_id(self):
        result = combine_markdown([("doc-one", "x\n"), ("doc.two", "y\n")])
        self.assertIn('id="doc-doc-one"', result.html)
        self.assertIn('id="doc-doc.two"', result.html)
        self.assertIn('data-document="doc-one"', result.html)


class TestFootnoteNumbering(TestCase):
    def test_global_numbering_by_first_citation_order(self):
        a = "a [^x] [^y]\n\n[^x]: xx\n[^y]: yy\n"
        b = "b [^z]\n\n[^z]: zz\n"
        html = combine_markdown([("a", a), ("b", b)]).html
        numbers = [int(ref[2]) for ref in _sup_refs(html)]
        self.assertEqual(numbers, [1, 2, 3])

    def test_reordering_changes_display_numbers_but_keeps_identity(self):
        a = "a [^x] [^y]\n\n[^x]: xx\n[^y]: yy\n"
        b = "b [^z]\n\n[^z]: zz\n"
        first = combine_markdown([("a", a), ("b", b)]).html
        second = combine_markdown([("b", b), ("a", a)]).html

        first_ids = {ref[0] for ref in _sup_refs(html=first)}
        second_ids = {ref[0] for ref in _sup_refs(html=second)}
        self.assertEqual(first_ids, second_ids)

        def number_map(html):
            return {ref[1]: int(ref[2]) for ref in _sup_refs(html)}

        first_map = number_map(first)
        second_map = number_map(second)
        self.assertEqual(first_map["#fn-a-x"], 1)
        self.assertEqual(first_map["#fn-a-y"], 2)
        self.assertEqual(first_map["#fn-b-z"], 3)
        self.assertEqual(second_map["#fn-b-z"], 1)
        self.assertEqual(second_map["#fn-a-x"], 2)
        self.assertEqual(second_map["#fn-a-y"], 3)

    def test_same_footnote_key_different_content(self):
        a = "Alpha [^1]\n\n[^1]: alpha content\n"
        b = "Beta [^1]\n\n[^1]: beta content\n"
        html = combine_markdown([("alpha", a), ("beta", b)]).html

        self.assertIn("alpha content", html)
        self.assertIn("beta content", html)
        self.assertIn('id="fn-alpha-1"', html)
        self.assertIn('id="fn-beta-1"', html)

        refs = _sup_refs(html)
        alpha_ref = next(r for r in refs if r[0].startswith("fnref-alpha"))
        beta_ref = next(r for r in refs if r[0].startswith("fnref-beta"))
        self.assertEqual(alpha_ref[1], "#fn-alpha-1")
        self.assertEqual(beta_ref[1], "#fn-beta-1")

        back = _backlinks(html)
        hrefs = {href for href, _id in back}
        self.assertIn("#" + alpha_ref[0], hrefs)
        self.assertIn("#" + beta_ref[0], hrefs)

    def test_repeated_citation_produces_multiple_backlinks(self):
        a = "one [^1] two [^1] three [^1]\n\n[^1]: the note\n"
        html = combine_markdown([("a", a)]).html
        refs = _sup_refs(html)
        self.assertEqual(len(refs), 3)
        self.assertEqual({r[0] for r in refs}, {r[0] for r in refs})
        ref_ids = [r[0] for r in refs]
        back = _backlinks(html)
        back_targets = [href[1:] for href, _ in back]
        self.assertEqual(sorted(back_targets), sorted(ref_ids))

    def test_cross_document_footnote_shares_one_numbered_item(self):
        a = "a [^1]\n\n[^1]: shared note\n"
        b = "b local [^2] and cross [^@a:1]\n\n[^2]: local note\n"
        html = combine_markdown([("a", a), ("b", b)]).html
        # only one rendered item for the shared footnote
        self.assertEqual(html.count("shared note"), 1)
        refs = _sup_refs(html)
        cross_ref = next(r for r in refs if r[0].startswith("fnref-b-a-1"))
        self.assertEqual(cross_ref[1], "#fn-a-1")
        self.assertEqual(cross_ref[2], "1")
        local_ref = next(r for r in refs if r[0].startswith("fnref-b-b-2"))
        self.assertEqual(local_ref[2], "2")
        # shared item has backlinks from both documents
        item = next(i for i in _footnote_items(html) if i[0] == "fn-a-1")
        self.assertIn('href="#fnref-a-a-1-1"', item[1])
        self.assertIn('href="#fnref-b-a-1-1"', item[1])

    def test_nested_footnotes_follow_single_document_ordering(self):
        text = "body [^a]\n\n[^a]: A cites [^b]\n[^b]: B cites [^c]\n[^c]: C\n"
        html = combine_markdown([("d", text)]).html
        numbers = [int(r[2]) for r in _sup_refs(html)]
        self.assertEqual(numbers, [1, 2, 3])


class TestCrossLinks(TestCase):
    def _docs(self):
        a = 'Defs.\n\n[guide]: https://g.example/ "Guide"\n'
        b = "shortcut [@a:guide] full [text][@a:guide]\n"
        return a, b

    def test_cross_links_require_explicit_syntax(self):
        a, b = self._docs()
        html = combine_markdown([("a", a), ("b", b)]).html
        self.assertIn('<a href="https://g.example/" title="Guide">guide</a>', html)
        self.assertIn('<a href="https://g.example/" title="Guide">text</a>', html)

    def test_same_name_does_not_resolve_across_documents_by_coincidence(self):
        a = "[guide]: https://a.example/\n"
        b = "local [guide] here\n"
        result = combine_markdown([("a", a), ("b", b)])
        # [guide] in b has no definition; it stays literal text
        self.assertIn("[guide]", result.html)
        self.assertNotIn("a.example", result.html)
        self.assertTrue(any(d.code == "unknown-link" for d in result.diagnostics))

    def test_unknown_cross_document_reference(self):
        a = "no defs here\n"
        b = "[@a:missing] link [^@a:absent] note\n"
        result = combine_markdown([("a", a), ("b", b)])
        codes = {d.code for d in result.diagnostics}
        self.assertIn("unknown-cross-link", codes)
        self.assertIn("unknown-cross-footnote", codes)

    def test_unknown_document_identifier(self):
        b = "[@ghost:x] [^@ghost:y]\n"
        result = combine_markdown([("b", b)])
        codes = [d.code for d in result.diagnostics]
        self.assertEqual(codes.count("unknown-document"), 2)


class TestDiagnostics(TestCase):
    def test_unknown_local_references(self):
        text = "missing [nope] footnote [^9]\n"
        result = combine_markdown([("d", text)])
        codes = {d.code for d in result.diagnostics}
        self.assertIn("unknown-link", codes)
        self.assertIn("unknown-footnote", codes)
        self.assertEqual(result.diagnostics[0].line, 1)

    def test_duplicate_document_identifier_raises(self):
        with self.assertRaises(BookError) as ctx:
            combine_markdown([("A", "x\n"), ("a", "y\n")])
        self.assertEqual(ctx.exception.diagnostics[0].code, "duplicate-document")

    def test_invalid_document_identifier_raises(self):
        with self.assertRaises(BookError) as ctx:
            combine_markdown([("bad id!", "x\n")])
        self.assertEqual(ctx.exception.diagnostics[0].code, "duplicate-document")

    def test_conflicting_duplicate_footnote_definitions(self):
        text = "[^1]: first\n[^1]: second different\n\nuse [^1]\n"
        result = combine_markdown([("d", text)])
        self.assertTrue(
            any(d.code == "conflicting-definition" for d in result.diagnostics)
        )

    def test_conflicting_duplicate_link_definitions(self):
        text = "[x]: https://one/\n[x]: https://two/\n\nsee [x]\n"
        result = combine_markdown([("d", text)])
        self.assertTrue(
            any(d.code == "conflicting-definition" for d in result.diagnostics)
        )

    def test_identical_duplicate_definitions_are_not_conflicts(self):
        text = "[^1]: same text\n[^1]: same text\n\n[x]: /x\n[x]: /x\n\nuse [^1] [x]\n"
        result = combine_markdown([("d", text)])
        self.assertFalse(
            any(d.code == "conflicting-definition" for d in result.diagnostics)
        )

    def test_strict_mode_raises_book_error(self):
        with self.assertRaises(BookError):
            combine_markdown([("d", "[missing]\n")], strict=True)

    def test_definitions_and_code_are_not_flagged_as_unknown(self):
        text = (
            "`[nope]` inline\n\n```\n[^x] [y]\n```\n\n"
            "[only-link]: /x\n[^unused]: note\n"
        )
        result = combine_markdown([("d", text)])
        self.assertEqual(result.diagnostics, [])


class TestStableIds(TestCase):
    def test_all_ids_are_unique(self):
        a = "[^1] [^1]\n\n[^1]: note A [^2]\n[^2]: note B\n"
        b = "[^1] [^@a:1]\n\n[^1]: note C\n"
        html = combine_markdown([("a", a), ("b", b)]).html
        ids = re.findall(r'id="([^"]+)"', html)
        self.assertEqual(len(ids), len(set(ids)))

    def test_ids_track_document_identity_across_reorder(self):
        a = "x [^1]\n\n[^1]: A\n"
        b = "y [^1]\n\n[^1]: B\n"
        first = combine_markdown([("a", a), ("b", b)]).html
        second = combine_markdown([("b", b), ("a", a)]).html
        for stable_id in ("fn-a-1", "fn-b-1", "doc-a", "doc-b"):
            self.assertIn(stable_id, first)
            self.assertIn(stable_id, second)


class TestSingleDocumentUnchanged(TestCase):
    def test_ordinary_parse_semantics_unchanged(self):
        text = "Text [home] [^1].\n\n[home]: /home\n[^1]: the note\n"
        md = mistune.create_markdown(escape=False, plugins=["footnotes"])
        html = md(text)
        self.assertIn('id="fnref-1"', html)
        self.assertIn('id="fn-1"', html)
        self.assertIn('href="/home"', html)
        # single-document plugin keeps its own renderer ids
        self.assertIn('<a href="#fnref-1" class="footnote">', html)

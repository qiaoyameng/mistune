import re

from mistune import BookComposer, create_markdown, compose_book
from tests import BaseTestCase


def _ids(html):
    return re.findall(r'id="([^"]+)"', html)


class TestBookCompose(BaseTestCase):
    def test_two_docs_same_footnote_name_different_content(self):
        # the headline scenario: two independent documents each define [^1]
        # with different text; references and back-links must stay separate.
        first = "Doc one has [^1].\n\n[^1]: note from one\n"
        second = "Doc two has [^1].\n\n[^1]: note from two\n"
        book = compose_book([("one", first), ("two", second)])

        self.assertIn("note from one", book.html)
        self.assertIn("note from two", book.html)

        self.assertIn('id="fn-one-1"', book.html)
        self.assertIn('id="fn-two-1"', book.html)

        # references point at their own document's definition
        self.assertIn('href="#fn-one-1">1<', book.html)
        self.assertIn('href="#fn-two-1">2<', book.html)

        # back links return to the reference in the owning document
        one_item = re.search(r'<li id="fn-one-1">(.*?)</li>', book.html, re.S).group(1)
        two_item = re.search(r'<li id="fn-two-1">(.*?)</li>', book.html, re.S).group(1)
        self.assertIn('href="#fnref-one-1"', one_item)
        self.assertNotIn("fnref-two", one_item)
        self.assertIn('href="#fnref-two-1"', two_item)
        self.assertNotIn("fnref-one", two_item)

    def test_footnotes_numbered_by_first_reference_order(self):
        first = "A [^b] [^a]\n\n[^a]: aa\n[^b]: bb\n"
        second = "B [^c]\n\n[^c]: cc\n"
        book = compose_book([("a", first), ("b", second)])
        nums = re.findall(r'class="footnote-ref"[^>]*><a[^>]*>(\d+)<', book.html)
        self.assertEqual(nums, ["1", "2", "3"])
        # first reference is b even though a is defined first
        self.assertIn('href="#fn-a-b">1<', book.html)
        self.assertIn('href="#fn-a-a">2<', book.html)

    def test_reorder_changes_display_numbers_but_not_identity(self):
        first = "A [^x]\n\n[^x]: xx\n"
        second = "B [^y]\n\n[^y]: yy\n"
        ab = compose_book([("a", first), ("b", second)]).html
        ba = compose_book([("b", second), ("a", first)]).html

        self.assertIn('href="#fn-a-x">1<', ab)
        self.assertIn('href="#fn-b-y">2<', ab)
        self.assertIn('href="#fn-b-y">1<', ba)
        self.assertIn('href="#fn-a-x">2<', ba)
        # identity ids never change
        self.assertIn('id="fn-a-x"', ab)
        self.assertIn('id="fn-a-x"', ba)

    def test_repeated_reference_produces_multiple_backlinks(self):
        text = "one [^1] two [^1]\n\n[^1]: shared\n"
        book = compose_book([("d", text)])
        self.assertIn('id="fnref-d-1"', book.html)
        self.assertIn('id="fnref-d-1-2"', book.html)
        item = re.search(r'<li id="fn-d-1">(.*?)</li>', book.html, re.S).group(1)
        self.assertIn('href="#fnref-d-1"', item)
        self.assertIn('href="#fnref-d-1-2"', item)
        # both back links belong to the same displayed number
        self.assertEqual(
            re.findall(r'class="footnote-ref"[^>]*><a[^>]*>(\d+)<', book.html),
            ["1", "1"],
        )

    def test_unreferenced_definition_is_omitted(self):
        text = "used [^used]\n\n[^used]: shown\n[^orphan]: hidden\n"
        book = compose_book([("d", text)])
        self.assertIn("shown", book.html)
        self.assertNotIn("hidden", book.html)
        self.assertNotIn('id="fn-d-orphan"', book.html)

    def test_local_references_do_not_resolve_across_documents(self):
        first = "own [home]\n\n[home]: https://a.example\n"
        second = "stranger [home]\n"
        book = compose_book([("a", first), ("b", second)])
        self.assertIn('href="https://a.example"', book.html)
        self.assertIn("stranger [home]", book.html)
        codes = {(d.document, d.code) for d in book.diagnostics}
        self.assertIn(("b", "unknown-link-reference"), codes)
        # the local reference in the second doc must not silently use a's def
        self.assertNotIn('stranger <a href="https://a.example"', book.html)

    def test_explicit_cross_document_footnote(self):
        first = "see [^@two/ref]\n"
        second = "body [^ref]\n\n[^ref]: cross note\n"
        book = compose_book([("one", first), ("two", second)])
        self.assertIn('href="#fn-two-ref"', book.html)
        self.assertIn("cross note", book.html)
        self.assertEqual(book.diagnostics, [])

    def test_explicit_cross_document_link_forms(self):
        first = "go [@two/home] and [label][@two/home]\n"
        second = "target\n\n[home]: https://e.example\n"
        book = compose_book([("one", first), ("two", second)])
        self.assertEqual(book.html.count('href="https://e.example"'), 2)
        self.assertIn(">home<", book.html)
        self.assertIn(">label<", book.html)
        self.assertEqual(book.diagnostics, [])

    def test_unknown_cross_reference_diagnostic(self):
        first = "bad [^@missing/k] and [x][@missing/l]\n"
        book = compose_book([("one", first)])
        codes = [d.code for d in book.diagnostics]
        self.assertEqual(codes.count("unknown-cross-reference"), 2)

    def test_unknown_local_footnote_diagnostic(self):
        book = compose_book([("d", "nope [^ghost]\n")])
        self.assertEqual([d.code for d in book.diagnostics], ["unknown-footnote-reference"])
        self.assertIn("[^ghost]", book.html)

    def test_duplicate_document_id_diagnostic(self):
        book = compose_book([("x", "first\n"), ("x", "second\n")])
        self.assertEqual(len(book.diagnostics), 1)
        self.assertEqual(book.diagnostics[0].code, "duplicate-document-id")
        self.assertIn("first", book.html)
        self.assertNotIn("second", book.html)

    def test_duplicate_and_conflicting_footnote_definitions(self):
        same = "p [^q]\n\n[^q]: once\n[^q]: once\n"
        book = compose_book([("d", same)])
        self.assertEqual([d.code for d in book.diagnostics], ["duplicate-footnote-definition"])

        conflict = "p [^q]\n\n[^q]: once\n[^q]: twice\n"
        book = compose_book([("d", conflict)])
        self.assertEqual([d.code for d in book.diagnostics], ["conflicting-footnote-definition"])
        self.assertIn("once", book.html)
        self.assertNotIn("twice", book.html)

    def test_conflicting_link_definition(self):
        text = "p [l]\n\n[l]: https://a\n[l]: https://b\n"
        book = compose_book([("d", text)])
        self.assertEqual([d.code for d in book.diagnostics], ["conflicting-link-definition"])
        self.assertIn("https://a", book.html)
        self.assertNotIn("https://b", book.html)

    def test_ids_are_unique_across_book(self):
        text = "p [^k] [^k]\n\n[^k]: note\n"
        book = compose_book([("A/B", text), ("a-b", text), ("a b", text)])
        ids = _ids(book.html)
        self.assertEqual(len(ids), len(set(ids)))

    def test_document_section_wrappers_and_input_forms(self):
        book = compose_book(
            [
                "plain string",
                ("named", "tuple text\n"),
                {"id": "mapped", "text": "mapping text\n"},
            ]
        )
        self.assertIn('data-doc-id="doc-1"', book.html)
        self.assertIn('data-doc-id="named"', book.html)
        self.assertIn('data-doc-id="mapped"', book.html)
        self.assertEqual(book.diagnostics, [])

    def test_nested_footnote_in_footnote_body(self):
        first = "start [^1]\n\n[^1]: body cites [^@two/2].\n"
        second = "b\n\n[^2]: inner\n"
        book = compose_book([("one", first), ("two", second)])
        nums = re.findall(r'class="footnote-ref"[^>]*><a[^>]*>(\d+)<', book.html)
        self.assertEqual(nums, ["1", "2"])
        self.assertIn("inner", book.html)


class TestSingleDocumentUnchanged(BaseTestCase):
    def test_plain_parse_keeps_original_semantics(self):
        md = create_markdown(escape=False, plugins=["footnotes"])
        out = md("oneline [^1]\n\n[^1]: foo\n")
        self.assertEqual(
            out,
            '<p>oneline <sup class="footnote-ref" id="fnref-1">'
            '<a href="#fn-1">1</a></sup></p>\n'
            '<section class="footnotes">\n<ol>\n'
            '<li id="fn-1"><p>foo<a href="#fnref-1" class="footnote">&#8617;</a></p></li>\n'
            "</ol>\n</section>\n",
        )

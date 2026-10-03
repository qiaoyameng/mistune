from functools import partial
from unittest import TestCase

import mistune
from mistune.plugins.citations import citations

ENTRIES = {
    "smith2020": "Smith, J. (2020). A Study of Things.",
    "lee2021": "Lee, K. (2021). Another Look.",
    "wu2022": {"text": "Wu, L. (2022). Mapping Entry."},
}


def make_md(resolver=None, **kwargs):
    kwargs.setdefault("escape", False)
    if resolver is None:
        resolver = lambda keys: {k: ENTRIES[k] for k in keys if k in ENTRIES}
    return mistune.create_markdown(plugins=[partial(citations, resolver=resolver)], **kwargs)


class TestCitationLexer(TestCase):
    def parse_items(self, text):
        md = mistune.create_markdown(renderer=None, plugins=[partial(citations, resolver=lambda keys: {})])
        tokens, _ = md.parse(text)
        for tok in tokens:
            for child in tok.get("children", []):
                if child["type"] == "citation":
                    return child["attrs"]["items"]
        return None

    def test_key_prefix_suffix_locator(self):
        items = self.parse_items("[see @smith2020, p. 3]")
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["key"], "smith2020")
        self.assertEqual(item["prefix"], "see")
        self.assertEqual(item["locator_label"], "p")
        self.assertEqual(item["locator"], "3")
        self.assertEqual(item["suffix"], "")
        self.assertFalse(item["suppress_author"])

    def test_suppress_author(self):
        items = self.parse_items("[-@lee2021]")
        self.assertTrue(items[0]["suppress_author"])
        self.assertEqual(items[0]["prefix"], "")

    def test_suppress_author_with_prefix(self):
        items = self.parse_items("[compare -@lee2021, ch. 2]")
        self.assertTrue(items[0]["suppress_author"])
        self.assertEqual(items[0]["prefix"], "compare")
        self.assertEqual(items[0]["locator"], "2")
        self.assertEqual(items[0]["locator_label"], "ch")

    def test_semicolon_separates_items(self):
        items = self.parse_items("[@smith2020; @lee2021; @wu2022]")
        self.assertEqual([i["key"] for i in items], ["smith2020", "lee2021", "wu2022"])

    def test_escaped_semicolon_does_not_split(self):
        items = self.parse_items(r"[@smith2020, p. 3\; see also]")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["suffix"], "; see also")

    def test_escaped_comma_and_brackets(self):
        items = self.parse_items(r"[@smith2020, note\, with \[brackets\]]")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["suffix"], "note, with [brackets]")

    def test_escaped_at_sign_is_not_a_key(self):
        self.assertIsNone(self.parse_items(r"[see \@smith2020]"))

    def test_nested_brackets_in_prefix(self):
        items = self.parse_items("[see [the appendix] @smith2020]")
        self.assertEqual(items[0]["prefix"], "see [the appendix]")

    def test_unbalanced_bracket_is_not_citation(self):
        self.assertIsNone(self.parse_items("[see [nested] @smith2020"))

    def test_locator_variants(self):
        for text, label, locator in [
            ("[@a, p. 3]", "p", "3"),
            ("[@a, pp. 3-5]", "pp", "3-5"),
            ("[@a, ch. 2]", "ch", "2"),
            ("[@a, sec. 4.1]", "sec", "4.1"),
            ("[@a, § 7]", "§", "7"),
        ]:
            items = self.parse_items(text)
            self.assertEqual((items[0]["locator_label"], items[0]["locator"]), (label, locator), text)

    def test_suffix_after_locator(self):
        items = self.parse_items("[@a, p. 3, emphasis added]")
        self.assertEqual(items[0]["locator"], "3")
        self.assertEqual(items[0]["suffix"], "emphasis added")

    def test_email_like_text_is_not_a_key(self):
        self.assertIsNone(self.parse_items("[contact foo@bar.com]"))

    def test_empty_key_is_not_citation(self):
        self.assertIsNone(self.parse_items("[@]"))
        self.assertIsNone(self.parse_items("[@, p. 3]"))


class TestCitationHTML(TestCase):
    def test_basic_render(self):
        md = make_md()
        html = md("A claim [@smith2020, p. 3; -@lee2021].")
        self.assertIn('<span class="citation">', html)
        self.assertIn('<a href="#ref-smith2020">1</a>, p. 3', html)
        self.assertIn('<a href="#ref-lee2021">2</a>', html)
        self.assertIn('<section class="bibliography">', html)
        self.assertIn('<li id="ref-smith2020">Smith, J. (2020). A Study of Things.', html)
        self.assertIn('<li id="ref-lee2021">Lee, K. (2021). Another Look.', html)

    def test_mapping_entry_text(self):
        md = make_md()
        html = md("[@wu2022]")
        self.assertIn("Wu, L. (2022). Mapping Entry.", html)

    def test_first_citation_order_and_shared_numbers(self):
        md = make_md()
        html = md("[@lee2021] then [@smith2020] then [@lee2021, p. 9].")
        # lee2021 cited first gets number 1, shared across both citations
        self.assertIn('<li id="ref-lee2021">', html)
        self.assertLess(html.index('id="ref-lee2021"'), html.index('id="ref-smith2020"'))
        self.assertIn('<a href="#ref-lee2021">1</a>', html)
        self.assertIn('<a href="#ref-lee2021">1</a>, p. 9', html)
        self.assertIn('<a href="#ref-smith2020">2</a>', html)

    def test_backlinks_per_occurrence(self):
        md = make_md()
        html = md("[@smith2020] and [@smith2020, p. 3].")
        self.assertIn('id="cite-smith2020-1"', html)
        self.assertIn('id="cite-smith2020-2"', html)
        self.assertIn('href="#cite-smith2020-1"', html)
        self.assertIn('href="#cite-smith2020-2"', html)

    def test_unknown_key_visible_diagnostic(self):
        md = make_md()
        html = md("Unknown [@ghost1999].")
        self.assertIn("citation-unknown", html)
        self.assertIn("Unresolved citation: @ghost1999", html)
        # no fabricated reference text
        self.assertNotIn("Ghost", html.replace("@ghost1999", ""))

    def test_no_citations_no_bibliography(self):
        md = make_md()
        html = md("Just plain text.")
        self.assertNotIn("bibliography", html)

    def test_resolver_called_once_with_unique_keys(self):
        calls = []

        def resolver(keys):
            calls.append(list(keys))
            return {}

        md = make_md(resolver=resolver)
        md("[@a] [@b; @a] [@c] [@a]")
        self.assertEqual(calls, [["a", "b", "c"]])

    def test_html_escaping_in_entries_and_affixes(self):
        md = make_md(resolver=lambda keys: {"a": "Smith <script> & Co."})
        html = md("[see <b> @a]")
        self.assertIn("Smith &lt;script&gt; &amp; Co.", html)
        self.assertIn("see &lt;b&gt;", html)

    def test_id_escaping_and_uniqueness(self):
        md = make_md(resolver=lambda keys: {k: "ref " + k for k in keys})
        html = md("[@smith/2020] and [@a#b].")
        self.assertIn('id="ref-smith%2F2020"', html)
        self.assertIn('href="#ref-smith%2F2020"', html)
        self.assertIn('id="ref-a%23b"', html)
        self.assertIn('href="#cite-a%23b-1"', html)


class TestCitationCoexistence(TestCase):
    def test_codespan_not_matched(self):
        md = make_md()
        html = md("`[@smith2020]`")
        self.assertIn("<code>[@smith2020]</code>", html)
        self.assertNotIn("citation", html)

    def test_indented_code_not_matched(self):
        md = make_md()
        html = md("    [@smith2020]")
        self.assertIn("<code>[@smith2020]", html)
        self.assertNotIn("class=\"citation\"", html)

    def test_inline_link_with_at_text(self):
        md = make_md()
        html = md("[@smith2020](http://example.com)")
        self.assertIn('<a href="http://example.com">@smith2020</a>', html)
        self.assertNotIn("bibliography", html)

    def test_link_text_containing_at(self):
        md = make_md()
        html = md("[see @smith2020](http://example.com)")
        self.assertIn('<a href="http://example.com">see @smith2020</a>', html)

    def test_image_not_matched(self):
        md = make_md()
        html = md("![@smith2020](img.png)")
        self.assertIn('<img src="img.png" alt="@smith2020"', html)

    def test_citation_not_matched_inside_link(self):
        md = make_md()
        html = md("[a [@smith2020] b](http://example.com)")
        self.assertIn("<a href=\"http://example.com\">a [@smith2020] b</a>", html)
        self.assertNotIn("bibliography", html)

    def test_reference_link_still_works(self):
        md = make_md()
        html = md("[a @b][x]\n\n[x]: http://example.com")
        self.assertIn('<a href="http://example.com">a @b</a>', html)

    def test_plugin_loads_by_name(self):
        md = mistune.create_markdown(escape=False, plugins=["citations"])
        html = md("[@smith2020]")
        # no resolver: everything is an unresolved diagnostic
        self.assertIn("citation-unknown", html)


class TestCitationAST(TestCase):
    def test_ast_tokens(self):
        md = mistune.create_markdown(renderer=None, plugins=[partial(citations, resolver=lambda keys: ENTRIES)])
        tokens, _ = md.parse("See [see @smith2020, ch. 2; -@lee2021].")
        citation = None
        bib = None
        for tok in tokens:
            if tok["type"] == "bibliography":
                bib = tok
            for child in tok.get("children", []):
                if child["type"] == "citation":
                    citation = child
        self.assertIsNotNone(citation)
        self.assertEqual(citation["raw"], "[see @smith2020, ch. 2; -@lee2021]")
        items = citation["attrs"]["items"]
        self.assertEqual(
            [(i["key"], i["prefix"], i["locator"], i["suppress_author"]) for i in items],
            [("smith2020", "see", "2", False), ("lee2021", "", "", True)],
        )
        self.assertIsNotNone(bib)
        bib_items = bib["children"]
        self.assertEqual([b["attrs"]["key"] for b in bib_items], ["smith2020", "lee2021"])
        self.assertEqual(bib_items[0]["attrs"]["text"], "Smith, J. (2020). A Study of Things.")
        self.assertTrue(all(b["attrs"]["known"] for b in bib_items))

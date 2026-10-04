from textwrap import indent
from unittest import TestCase

from mistune import create_markdown
from mistune.plugins.grid_table import grid_table_in_list, grid_table_in_quote
from mistune.renderers.markdown import MarkdownRenderer


class TestGridTablePlugin(TestCase):
    def test_simple_grid_table(self):
        text = (
            "+-------+-------+\n"
            "| A     | B     |\n"
            "+-------+-------+\n"
            "| C     | D     |\n"
            "+-------+-------+\n"
        )
        html = create_markdown(escape=False, plugins=["grid_table"])(text)

        self.assertEqual(html.count("<tr>"), 2)
        self.assertIn("<td><p>A</p>", html)
        self.assertIn("<td><p>D</p>", html)

    def test_row_and_column_spans(self):
        text = (
            "+---------------+-------+\n"
            "| Title         | C     |\n"
            "+=======+=======+=======+\n"
            "| Side  | B     | C     |\n"
            "|       +-------+-------+\n"
            "|       | D     | E     |\n"
            "+-------+-------+-------+\n"
        )
        md = create_markdown(escape=False, plugins=["grid_table"])

        self.assertEqual(
            md(text),
            "<table>\n"
            "<thead>\n"
            "<tr>\n"
            '  <th colspan="2"><p>Title</p>\n'
            "</th>\n"
            '  <th><p>C</p>\n'
            "</th>\n"
            "</tr>\n"
            "</thead>\n"
            "<tbody>\n"
            "<tr>\n"
            '  <td rowspan="2"><p>Side</p>\n'
            "</td>\n"
            '  <td><p>B</p>\n'
            "</td>\n"
            '  <td><p>C</p>\n'
            "</td>\n"
            "</tr>\n"
            "<tr>\n"
            '  <td><p>D</p>\n'
            "</td>\n"
            '  <td><p>E</p>\n'
            "</td>\n"
            "</tr>\n"
            "</tbody>\n"
            "</table>\n",
        )

        ast = create_markdown(renderer=None, plugins=["grid_table"])(text)
        table = ast[0]
        head_cell = table["children"][0]["children"][0]["children"][0]
        body_cell = table["children"][1]["children"][0]["children"][0]

        self.assertEqual(table["attrs"], {"cols": 3})
        self.assertEqual(head_cell["attrs"]["colspan"], 2)
        self.assertEqual(body_cell["attrs"]["rowspan"], 2)

    def test_cell_blocks_and_unaligned_pipe(self):
        text = (
            "+------------+------------+\n"
            "| - a        | ```        |\n"
            "| - b        | x = 1      |\n"
            "|            | ```        |\n"
            "| paragraph  | a | b      |\n"
            "+------------+------------+\n"
        )
        html = create_markdown(escape=False, plugins=["grid_table"])(text)

        self.assertIn("<ul>", html)
        self.assertIn("<li>a</li>", html)
        self.assertIn("<pre><code>x = 1\n</code></pre>", html)
        self.assertIn("<p>a | b</p>", html)

    def test_blank_border_line_keeps_cell_together(self):
        text = (
            "+-------------+\n"
            "| first       |\n"
            "|             |\n"
            "| second      |\n"
            "+-------------+\n"
        )
        html = create_markdown(escape=False, plugins=["grid_table"])(text)

        self.assertIn("<p>first</p>", html)
        self.assertIn("<p>second</p>", html)
        self.assertNotIn("<table", html.replace("<table", "", 1))

    def test_markdown_renderer_fallback(self):
        text = (
            "+---------------+-------+\n"
            "| Title         | C     |\n"
            "+=======+=======+=======+\n"
            "| Side  | B     | C     |\n"
            "|       +-------+-------+\n"
            "|       | D     | E     |\n"
            "+-------+-------+-------+\n"
        )
        result = create_markdown(renderer=MarkdownRenderer(), plugins=["grid_table"])(text)

        self.assertIn("[grid-table]", result)
        self.assertIn("[th colspan=2]\n    Title", result)
        self.assertIn("[rowspan=2]\n    Side", result)

    def test_in_quote_and_list(self):
        text = "+-------+\n| A     |\n+-------+\n"

        quote = create_markdown(escape=False, plugins=["grid_table", grid_table_in_quote])
        self.assertIn("<table>", quote(indent(text, "> ")))

        listing = create_markdown(escape=False, plugins=["grid_table", grid_table_in_list])
        self.assertIn("<table>", listing("- item\n\n" + indent(text, "  ")))

    def test_invalid_grid_is_plain_text(self):
        text = (
            "+---------------+\n"
            "| Cell          |\n"
            "|       +-------+\n"
            "| A     | B     |\n"
            "+-------+-------+\n"
        )
        html = create_markdown(escape=False, plugins=["grid_table"])(text)

        self.assertTrue(html.startswith("<p>"))
        self.assertNotIn("<table", html)

    def test_crossing_border_is_plain_text(self):
        text = (
            "+-------+-------+\n"
            "| A     | B     |\n"
            "+-------|-------+\n"
            "| C     | D     |\n"
            "+-------+-------+\n"
        )
        html = create_markdown(escape=False, plugins=["grid_table"])(text)

        self.assertTrue(html.startswith("<p>"))
        self.assertNotIn("<table", html)

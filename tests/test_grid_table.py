from mistune import HTMLRenderer, Markdown
from mistune.plugins.grid_table import grid_table
from mistune.renderers.markdown import MarkdownRenderer


def markdown(**kwargs):
    options = {"renderer": HTMLRenderer(escape=False)}
    options.update(kwargs)
    return Markdown(plugins=[grid_table], **options)


def test_grid_table_horizontal_and_vertical_spans():
    text = """\
+-----+-----+-----+
| Head      | H3  |
+=====+=====+=====+
| A   | B         |
|     +-----+-----+
|     | C   | D   |
+-----+-----+-----+
"""
    md = markdown()
    html = md(text)

    assert '<th colspan="2">Head</th>' in html
    assert '<th>H3</th>' in html
    assert '<td rowspan="2">A</td>' in html
    assert '<td colspan="2">B</td>' in html
    assert "<td>C</td>" in html
    assert "<td>D</td>" in html


def test_grid_table_multiple_simple_rows():
    text = """\
+-----+-----+
| A   | B   |
+-----+-----+
| C   | D   |
+-----+-----+
"""
    html = markdown()(text)
    assert "<th" not in html
    assert "<td>A</td>" in html
    assert "<td>B</td>" in html
    assert "<td>C</td>" in html
    assert "<td>D</td>" in html


def test_grid_table_cell_blocks():
    text = """\
+-----------+-------+
| first     | - one |
|           | - two |
|           |       |
| ```python |       |
| value = 1 |       |
| ```       |       |
+-----------+-------+
"""
    html = markdown()(text)

    assert "<p>first</p>" in html
    assert "<ul>\n<li>one</li>\n<li>two</li>\n</ul>" in html
    assert '<pre><code class="language-python">value = 1\n</code></pre>' in html


def test_unaligned_pipe_kept_inside_cell():
    text = """\
+-----------+
| a | b     |
+-----------+
paragraph
"""
    html = markdown()(text)
    assert "<td>a | b</td>" in html
    assert "<p>paragraph</p>" in html


def test_invalid_grid_becomes_paragraph():
    text = """\
+-----+-----+
| A   |
+-----+-----+
"""
    html = markdown()(text)
    assert "<table>" not in html
    assert "+-----+-----+" in html


def test_non_rectangular_merge_becomes_paragraph():
    text = """\
+-----+-----+-----+
| A         |     |
|     +-----+     |
| B   | C   | D   |
+-----+-----+-----+
"""
    html = markdown()(text)
    assert "<table>" not in html
    assert "+-----+-----+-----+" in html


def test_ast_contains_explicit_spans():
    tokens = markdown(renderer=None)("""\
+-----+
| A   |
+-----+
""")
    cell = tokens[0]["children"][-1]["children"][0]["children"][0]
    assert cell["attrs"]["colspan"] == 1
    assert cell["attrs"]["rowspan"] == 1


def test_markdown_renderer_degrades_explicitly():
    text = """\
+-----+-----+
| A         |
+-----+-----+
"""
    result = Markdown(renderer=MarkdownRenderer(), plugins=[grid_table])(text)
    assert "[grid-table]" in result
    assert "[cell colspan=2]" in result

import re
from textwrap import dedent, indent
from typing import TYPE_CHECKING, Any, Dict, List, Match, NamedTuple, Optional, Sequence, Set, Tuple

if TYPE_CHECKING:
    from ..block_parser import BlockParser
    from ..core import BaseRenderer, BlockState
    from ..markdown import Markdown

__all__ = ["grid_table"]


GRID_TABLE_PATTERN = r"^(?P<grid_indent>[ ]{0,3})\+(?:-{3,}-*\+)+[ \t]*(?:\n|$)"


class _Grid(NamedTuple):
    columns: List[int]
    borders: List[int]
    horizontal: List[List[bool]]
    vertical: List[List[bool]]
    header: Optional[int]
    cells: List[Dict[str, int]]


def parse_grid_table(block: "BlockParser", m: Match[str], state: "BlockState") -> Optional[int]:
    indent = m.group("grid_indent")
    pos = m.end()
    lines = [m.group(0).rstrip("\n")]

    while pos < state.cursor_max:
        line = state.get_line(pos)
        if not line.strip():
            break

        raw_line = line.rstrip("\n")
        if not raw_line.startswith(indent):
            break

        body = raw_line[len(indent) :].rstrip(" \t")
        if not body:
            break

        lines.append(raw_line)
        pos += len(line)

        if _is_full_horizontal_line(body, "-") and not _next_line_is_grid(state, pos, indent):
            break

    table = state.src[m.start() : pos]
    try:
        grid, table_lines = _build_grid(lines)
    except ValueError:
        state.add_paragraph(table)
        return pos

    thead: Optional[Dict[str, Any]] = None
    body_rows: List[Dict[str, Any]] = []

    for row, cell_specs in _iter_logical_rows(grid):
        cells = [
            _parse_cell(block, state, spec, table_lines, grid.header is not None and row == 0)
            for spec in cell_specs
        ]
        row_token = {
            "type": "grid_table_row",
            "children": cells,
        }
        if grid.header is not None and row == 0:
            thead = {"type": "grid_table_head", "children": [row_token]}
        else:
            body_rows.append(row_token)

    children: List[Dict[str, Any]] = []
    if thead is not None:
        children.append(thead)
    children.append({"type": "grid_table_body", "children": body_rows})
    state.append_token({"type": "grid_table", "children": children})
    return pos


def _next_line_is_grid(state: "BlockState", pos: int, indent: str) -> bool:
    if pos >= state.cursor_max:
        return False

    line = state.get_line(pos)
    if not line.strip() or not line.startswith(indent):
        return False

    body = line[len(indent) :].strip()
    return bool(body) and body[0] in "+|"


def _parse_cell(
    block: "BlockParser",
    state: "BlockState",
    spec: Dict[str, int],
    lines: Sequence[str],
    head: bool,
) -> Dict[str, Any]:
    left = spec["left"]
    right = spec["right"]
    content_lines: List[str] = []

    for line_index in range(spec["top"] + 1, spec["bottom"]):
        line = lines[line_index]
        content_lines.append(line[left + 1 : right].rstrip())

    text = dedent("\n".join(content_lines)).strip("\n")
    if text:
        child = state.child_state(text + "\n")
        if state.depth() >= block.max_nested_level - 1:
            rules = [rule for rule in block.rules if rule != "grid_table"]
        else:
            rules = block.rules
        block.parse(child, rules)
        children = child.tokens
        if len(children) == 1 and children[0]["type"] == "paragraph":
            children[0]["type"] = "block_text"
    else:
        children = []

    attrs = {
        "head": head,
        "colspan": spec["width"],
        "rowspan": spec["height"],
    }
    return {"type": "grid_table_cell", "children": children, "attrs": attrs}


def _iter_logical_rows(
    grid: _Grid,
) -> List[Tuple[int, List[Dict[str, int]]]]:
    rows: Dict[int, List[Dict[str, int]]] = {}
    for cell in grid.cells:
        rows.setdefault(cell["top"], []).append(cell)
    return [
        (grid.borders.index(top), sorted(cells, key=lambda item: item["left"]))
        for top, cells in sorted(rows.items())
    ]


def _build_grid(raw_lines: Sequence[str]) -> Tuple[_Grid, List[str]]:
    if len(raw_lines) < 3:
        raise ValueError("grid table is incomplete")

    indent_len = len(raw_lines[0]) - len(raw_lines[0].lstrip(" "))
    lines = [line[indent_len:].rstrip(" \t") for line in raw_lines]
    width = len(lines[0])

    columns: List[int] = [index for index, char in enumerate(lines[0]) if char == "+"]
    if len(columns) < 2 or columns[0] != 0 or columns[-1] != width - 1:
        raise ValueError("invalid top grid border")
    if not _is_full_horizontal_line(lines[0], "-"):
        raise ValueError("invalid top grid border")

    if not _is_full_horizontal_line(lines[-1], "-"):
        raise ValueError("grid table is missing a bottom border")

    for line in lines:
        if len(line) != width:
            raise ValueError("grid line does not reach both outer borders")

    column_count = len(columns) - 1
    horizontal = [[False for _ in range(column_count)] for _ in lines]
    horizontal[0] = [True for _ in range(column_count)]
    horizontal[-1] = [True for _ in range(column_count)]
    border_lines: Set[int] = {0, len(lines) - 1}

    for line_index in range(1, len(lines) - 1):
        line = lines[line_index]
        walls: List[bool] = []
        styles: List[Optional[str]] = []

        for start, end in zip(columns, columns[1:]):
            interior = line[start + 1 : end]
            style: Optional[str] = None
            wall = False
            if all(char in "-=" for char in interior):
                style = interior[0]
                wall = line[start] == "+" and line[end] == "+"
            walls.append(wall)
            styles.append(style)

        if any(walls):
            border_lines.add(line_index)
            horizontal[line_index] = walls

            for cell_index, wall in enumerate(walls):
                start = columns[cell_index]
                end = columns[cell_index + 1]
                interior = line[start + 1 : end]
                if wall:
                    if any(char != styles[cell_index] for char in interior):
                        raise ValueError("mixed horizontal grid border styles")
                elif any(char != " " for char in interior):
                    raise ValueError("grid border line crosses cell content")

            active_styles = {styles[i] for i, wall in enumerate(walls) if wall}
            if len(active_styles) != 1:
                raise ValueError("mixed horizontal grid border styles")
            if "=" in active_styles and not all(walls):
                raise ValueError("partial grid header border")

    borders = sorted(border_lines)
    if any(borders[index + 1] - borders[index] < 2 for index in range(len(borders) - 1)):
        raise ValueError("grid table row has no content")

    header: Optional[int] = None
    for line_index in range(1, len(lines) - 1):
        if all(horizontal[line_index]) and lines[line_index][columns[0] + 1] == "=":
            if line_index != borders[1]:
                raise ValueError("invalid grid table header border")
            header = line_index

    band_count = len(borders) - 1
    vertical = [[False for _ in columns] for _ in range(band_count)]

    for band_index, (top, bottom) in enumerate(zip(borders, borders[1:])):
        for column_index, column in enumerate(columns):
            chars = {lines[line_index][column] for line_index in range(top + 1, bottom)}
            if column_index in (0, len(columns) - 1):
                if chars != {"|"}:
                    raise ValueError("outer vertical grid border is not closed")
                vertical[band_index][column_index] = True
            elif chars == {"|"}:
                vertical[band_index][column_index] = True
            elif chars != {" "}:
                raise ValueError("grid cell crosses an unaligned border")

        for border in (top, bottom):
            line = lines[border]
            for column_index, column in enumerate(columns):
                adjacent = any(
                    horizontal[border][max(column_index - 1, 0) : min(column_index + 1, column_count)]
                )
                if adjacent:
                    expected = "+"
                elif column_index in (0, len(columns) - 1) or vertical[band_index][column_index]:
                    expected = "|"
                else:
                    expected = " "
                if line[column] != expected:
                    raise ValueError("grid borders do not form a closed rectangle")

    cells: List[Dict[str, int]] = []
    seen = [[False for _ in range(column_count)] for _ in range(band_count)]

    for start_row in range(band_count):
        for start_col in range(column_count):
            if seen[start_row][start_col]:
                continue

            queue = [(start_row, start_col)]
            component: List[Tuple[int, int]] = []
            seen[start_row][start_col] = True

            while queue:
                row, col = queue.pop()
                component.append((row, col))

                if col + 1 < column_count and not vertical[row][col + 1] and not seen[row][col + 1]:
                    seen[row][col + 1] = True
                    queue.append((row, col + 1))
                if row + 1 < band_count and not horizontal[borders[row + 1]][col] and not seen[row + 1][col]:
                    seen[row + 1][col] = True
                    queue.append((row + 1, col))

            min_row = min(row for row, _ in component)
            max_row = max(row for row, _ in component)
            min_col = min(col for _, col in component)
            max_col = max(col for _, col in component)
            expected_size = (max_row - min_row + 1) * (max_col - min_col + 1)
            if len(component) != expected_size:
                raise ValueError("merged grid cell is not rectangular")

            for col in range(min_col, max_col + 1):
                if not horizontal[borders[min_row]][col] or not horizontal[borders[max_row + 1]][col]:
                    raise ValueError("grid cell is not closed horizontally")
            for row in range(min_row, max_row + 1):
                if not vertical[row][min_col] or not vertical[row][max_col + 1]:
                    raise ValueError("grid cell is not closed vertically")

            cells.append({
                "top": borders[min_row],
                "bottom": borders[max_row + 1],
                "left": columns[min_col],
                "right": columns[max_col + 1],
                "width": max_col - min_col + 1,
                "height": max_row - min_row + 1,
            })

    return _Grid(columns, borders, horizontal, vertical, header, cells), lines


def _is_full_horizontal_line(line: str, char: str) -> bool:
    return (
        len(line) >= 5
        and line[0] == "+"
        and line[-1] == "+"
        and all(part and all(c == char for c in part) for part in line[1:-1].split("+"))
    )


def render_grid_table(renderer: "BaseRenderer", text: str) -> str:
    return "<table>\n" + text + "</table>\n"


def render_grid_table_head(renderer: "BaseRenderer", text: str) -> str:
    return "<thead>\n" + text + "</thead>\n"


def render_grid_table_body(renderer: "BaseRenderer", text: str) -> str:
    return "<tbody>\n" + text + "</tbody>\n"


def render_grid_table_row(renderer: "BaseRenderer", text: str) -> str:
    return "<tr>\n" + text + "</tr>\n"


def render_grid_table_cell(
    renderer: "BaseRenderer",
    text: str,
    head: bool = False,
    colspan: int = 1,
    rowspan: int = 1,
) -> str:
    tag = "th" if head else "td"
    html = "  <" + tag
    if colspan > 1:
        html += ' colspan="' + str(colspan) + '"'
    if rowspan > 1:
        html += ' rowspan="' + str(rowspan) + '"'
    return html + ">" + text + "</" + tag + ">\n"


def grid_table(md: "Markdown") -> None:
    """Enable reStructuredText-style grid tables with row and column spans."""
    md.block.register("grid_table", GRID_TABLE_PATTERN, parse_grid_table, before="paragraph")

    if md.renderer and md.renderer.NAME == "html":
        md.renderer.register("grid_table", render_grid_table)
        md.renderer.register("grid_table_head", render_grid_table_head)
        md.renderer.register("grid_table_body", render_grid_table_body)
        md.renderer.register("grid_table_row", render_grid_table_row)
        md.renderer.register("grid_table_cell", render_grid_table_cell)
    elif md.renderer and md.renderer.NAME == "markdown":
        md.renderer.register("grid_table", render_markdown_grid_table)
        md.renderer.register("grid_table_head", render_markdown_grid_container)
        md.renderer.register("grid_table_body", render_markdown_grid_container)
        md.renderer.register("grid_table_row", render_markdown_grid_row)
        md.renderer.register("grid_table_cell", render_markdown_grid_cell)


def render_markdown_grid_table(renderer: "BaseRenderer", token: Dict[str, Any], state: "BlockState") -> str:
    text = renderer.render_tokens(token.get("children", []), state)
    return "[grid-table]\n" + indent(text.rstrip(), "  ") + "\n[/grid-table]\n\n"


def render_markdown_grid_container(renderer: "BaseRenderer", token: Dict[str, Any], state: "BlockState") -> str:
    label = "head" if token["type"] == "grid_table_head" else "body"
    text = renderer.render_tokens(token.get("children", []), state)
    return "[" + label + "]\n" + text


def render_markdown_grid_row(renderer: "BaseRenderer", token: Dict[str, Any], state: "BlockState") -> str:
    text = renderer.render_tokens(token.get("children", []), state)
    return "[row]\n" + text


def render_markdown_grid_cell(renderer: "BaseRenderer", token: Dict[str, Any], state: "BlockState") -> str:
    attrs = token.get("attrs", {})
    flags = []
    if attrs.get("head"):
        flags.append("head")
    colspan = attrs.get("colspan", 1)
    rowspan = attrs.get("rowspan", 1)
    if colspan > 1:
        flags.append("colspan=" + str(colspan))
    if rowspan > 1:
        flags.append("rowspan=" + str(rowspan))

    label = "cell"
    if flags:
        label += " " + " ".join(flags)

    text = renderer.render_tokens(token.get("children", []), state).rstrip()
    if not text:
        return "[" + label + "]\n"
    return "[" + label + "]\n" + indent(text, "  ") + "\n"

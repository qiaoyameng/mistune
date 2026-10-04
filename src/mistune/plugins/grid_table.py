from collections import deque
from dataclasses import dataclass
from textwrap import indent
from typing import TYPE_CHECKING, Any, Dict, List, Match, Optional, Sequence, Set, Tuple

if TYPE_CHECKING:
    from ..block_parser import BlockParser
    from ..core import BaseRenderer, BlockState
    from ..markdown import Markdown
    from ..renderers.markdown import MarkdownRenderer

__all__ = ["grid_table", "grid_table_in_quote", "grid_table_in_list"]


GRID_TABLE_PATTERN = r"^ {0,3}(?=[+-]{2,})(?=.*\+[ \t]*(?:\n|$))[^\n]*(?:\n|$)"


@dataclass(frozen=True)
class _Node:
    row: int
    col: int


@dataclass(frozen=True)
class _Cell:
    top: int
    bottom: int
    left: int
    right: int


@dataclass(frozen=True)
class _Grid:
    rows: List[int]
    cols: List[int]
    horizontal: Set[Tuple[int, int, int]]
    vertical: Set[Tuple[int, int, int]]
    cells: List[_Cell]
    header_bottom: Optional[int]


def parse_grid_table(block: "BlockParser", m: Match[str], state: "BlockState") -> Optional[int]:
    first_line = state.get_line(m.start())
    indent_len = len(first_line) - len(first_line.lstrip(" "))

    lines, end_pos = _collect_grid_lines(state, m.start(), indent_len)
    grid = _build_grid(lines)
    if grid is None:
        state.add_paragraph(state.src[state.cursor:end_pos])
        return end_pos

    children: List[Dict[str, Any]] = []
    header_rows = _grid_header_rows(grid)

    if header_rows:
        head_cells = _parse_cells(block, state, lines, grid, header_rows, True)
        if head_cells is None:
            state.add_paragraph(state.src[state.cursor:end_pos])
            return end_pos
        children.append({"type": "grid_table_head", "children": head_cells})

    body_row_start = header_rows[-1] + 1 if header_rows else 0
    body_rows = range(body_row_start, len(grid.rows) - 1)
    body_cells = _parse_cells(block, state, lines, grid, body_rows, False)
    if body_cells is None:
        state.add_paragraph(state.src[state.cursor:end_pos])
        return end_pos

    children.append({"type": "grid_table_body", "children": body_cells})
    state.append_token(
        {"type": "grid_table", "attrs": {"cols": len(grid.cols) - 1}, "children": children}
    )
    return end_pos


def _collect_grid_lines(state: "BlockState", start: int, indent_len: int) -> Tuple[List[str], int]:
    pos = start
    raw_lines: List[str] = []

    while pos < state.cursor_max:
        line = state.get_line(pos)
        if line in ("\n", ""):
            break

        indent = line[:indent_len]
        if indent != " " * indent_len or len(line) <= indent_len or line[indent_len] not in "+|":
            break

        raw_lines.append(line.rstrip("\n"))
        pos += len(line)

    return [line[indent_len:] for line in raw_lines], pos


def _build_grid(raw_lines: Sequence[str]) -> Optional[_Grid]:
    lines = [line.rstrip(" \t") for line in raw_lines]
    if len(lines) < 3 or not lines[0] or any(not line for line in lines):
        return None

    width = len(lines[0])
    if width < 4 or not _is_outer_border(lines[0]) or "=" in lines[0]:
        return None
    if len(lines[-1]) != width or not _is_outer_border(lines[-1]) or "=" in lines[-1]:
        return None
    if any(len(line) < width for line in lines[1:-1]):
        return None

    padded = [line.ljust(width) for line in lines]
    nodes = _find_nodes(padded)
    if not nodes:
        return None

    node_points = {(node.row, node.col) for node in nodes}
    horizontal, header_bottom = _find_horizontal_edges(padded, node_points)
    vertical = _find_vertical_edges(padded, node_points)

    connected = _connected_nodes(horizontal, vertical, _Node(0, 0), node_points)
    outer_nodes = {
        _Node(0, 0),
        _Node(0, width - 1),
        _Node(len(lines) - 1, 0),
        _Node(len(lines) - 1, width - 1),
    }
    if not outer_nodes.issubset(connected):
        return None

    horizontal = {edge for edge in horizontal if _edge_nodes(edge, horizontal=True) <= connected}
    vertical = {edge for edge in vertical if _edge_nodes(edge, horizontal=False) <= connected}
    if any(not _has_horizontal_and_vertical(node, horizontal, vertical) for node in connected):
        return None

    row_values = sorted({node.row for node in connected})
    col_values = sorted({node.col for node in connected})
    if any(row_values[index + 1] - row_values[index] < 2 for index in range(len(row_values) - 1)):
        return None
    if any(col_values[index + 1] - col_values[index] < 3 for index in range(len(col_values) - 1)):
        return None
    col_set = set(col_values)
    if any(left not in col_set or right not in col_set for _, left, right in horizontal):
        return None
    vertical_cols = {col for col, _, _ in vertical}
    if any(left not in vertical_cols or right not in vertical_cols for _, left, right in horizontal):
        return None

    cells = _find_cells(row_values, col_values, horizontal, vertical)
    if cells is None:
        return None

    return _Grid(row_values, col_values, horizontal, vertical, cells, header_bottom)


def _is_outer_border(line: str) -> bool:
    if not line.startswith("+") or not line.endswith("+"):
        return False

    fill: Optional[str] = None
    for char in line[1:-1]:
        if char in "-=":
            if fill is None:
                fill = char
            elif fill != char:
                return False
        elif char != "+":
            return False
    return fill is not None


def _find_nodes(lines: Sequence[str]) -> Set[_Node]:
    nodes: Set[_Node] = set()
    height = len(lines)
    width = len(lines[0])

    for row, line in enumerate(lines):
        for col, char in enumerate(line):
            if char != "+":
                continue

            horizontal_neighbor = (
                (col > 0 and line[col - 1] in "-=")
                or (col + 1 < width and line[col + 1] in "-=")
            )
            vertical_neighbor = (
                (row > 0 and lines[row - 1][col] == "|")
                or (row + 1 < height and lines[row + 1][col] == "|")
            )
            if horizontal_neighbor and vertical_neighbor:
                nodes.add(_Node(row, col))
    return nodes


def _find_horizontal_edges(
    lines: Sequence[str], nodes: Set[Tuple[int, int]]
) -> Tuple[Set[Tuple[int, int, int]], Optional[int]]:
    edges: Set[Tuple[int, int, int]] = set()
    header_bottom: Optional[int] = None
    height = len(lines)

    for row, line in enumerate(lines):
        col = 1
        while col < len(line) - 1:
            if line[col] not in "-=":
                col += 1
                continue

            fill = line[col]
            end = col
            while end + 1 < len(line) - 1 and line[end + 1] == fill:
                end += 1

            left = col - 1
            right = end + 1
            if (row, left) in nodes and (row, right) in nodes:
                edges.add((row, left, right))
            col = end + 1
            continue

        if "=" in line:
            is_header = (
                row not in (0, height - 1)
                and line[0] == "+"
                and line[-1] == "+"
                and all(char in "+=" for char in line[1:-1])
                and any(char == "=" for char in line[1:-1])
            )
            if line[0] == "+" and not is_header:
                return set(), None
            if is_header:
                if header_bottom is not None:
                    return set(), None
                header_bottom = row

        edge_ranges = [
            (left, right)
            for edge_row, left, right in edges
            if edge_row == row
        ]
        if edge_ranges:
            if not _separator_line_is_valid(line, edge_ranges, nodes, row, height):
                return set(), None

    return edges, header_bottom


def _separator_line_is_valid(
    line: str,
    edge_ranges: Sequence[Tuple[int, int]],
    nodes: Set[Tuple[int, int]],
    row: int,
    height: int,
) -> bool:
    edge_cols: Set[int] = set()
    endpoint_cols: Set[int] = set()
    node_cols = {col for edge_row, col in nodes if edge_row == row}
    for col, char in enumerate(line):
        for left, right in edge_ranges:
            if left <= col <= right:
                edge_cols.add(col)
                if col in (left, right):
                    endpoint_cols.add(col)
    if not edge_cols:
        return True

    for col, char in enumerate(line):
        if char == " ":
            continue
        if col in endpoint_cols:
            if char != "+":
                return False
            continue
        if char == "|" and col in edge_cols and 0 < col < len(line) - 1:
            return False
        if char == "+" and col in node_cols:
            continue
        if col in edge_cols:
            if char not in "-=":
                return False
            continue
        if (col == 0 or col == len(line) - 1) and char == "|":
            continue
        return False
    return True


def _find_vertical_edges(lines: Sequence[str], nodes: Set[Tuple[int, int]]) -> Set[Tuple[int, int, int]]:
    edges: Set[Tuple[int, int, int]] = set()
    height = len(lines)
    width = len(lines[0])

    for col in range(width):
        row = 1
        while row < height - 1:
            if lines[row][col] != "|":
                row += 1
                continue

            start = row
            while row < height - 1 and lines[row][col] == "|":
                row += 1
            end = row

            top = start - 1
            bottom = end
            if (top, col) in nodes and (bottom, col) in nodes:
                edges.add((col, top, bottom))

    return edges


def _edge_nodes(edge: Tuple[int, int, int], horizontal: bool) -> Set[_Node]:
    a, b, c = edge
    if horizontal:
        return {_Node(a, b), _Node(a, c)}
    return {_Node(b, a), _Node(c, a)}


def _connected_nodes(
    horizontal: Set[Tuple[int, int, int]],
    vertical: Set[Tuple[int, int, int]],
    start: _Node,
    nodes: Set[Tuple[int, int]],
) -> Set[_Node]:
    adjacency: Dict[Tuple[int, int], Set[Tuple[int, int]]] = {point: set() for point in nodes}

    for row, left, right in horizontal:
        adjacency[(row, left)].add((row, right))
        adjacency[(row, right)].add((row, left))
    for col, top, bottom in vertical:
        adjacency[(top, col)].add((bottom, col))
        adjacency[(bottom, col)].add((top, col))

    seen = {(start.row, start.col)}
    queue = deque([(start.row, start.col)])
    while queue:
        point = queue.popleft()
        for neighbor in adjacency[point]:
            if neighbor not in seen:
                seen.add(neighbor)
                queue.append(neighbor)
    return {_Node(row, col) for row, col in seen}


def _has_horizontal_and_vertical(
    node: _Node,
    horizontal: Set[Tuple[int, int, int]],
    vertical: Set[Tuple[int, int, int]],
) -> bool:
    has_horizontal = any(
        row == node.row and node.col in (left, right) for row, left, right in horizontal
    )
    has_vertical = any(
        col == node.col and node.row in (top, bottom) for col, top, bottom in vertical
    )
    return has_horizontal and has_vertical


def _find_cells(
    rows: Sequence[int],
    cols: Sequence[int],
    horizontal: Set[Tuple[int, int, int]],
    vertical: Set[Tuple[int, int, int]],
) -> Optional[List[_Cell]]:
    row_count = len(rows) - 1
    col_count = len(cols) - 1
    row_index = {value: index for index, value in enumerate(rows)}
    col_index = {value: index for index, value in enumerate(cols)}

    horizontal_blocks = {
        (row_index[row], col_index[left], col_index[right]) for row, left, right in horizontal
    }
    vertical_blocks = {
        (col_index[col], row_index[top], row_index[bottom]) for col, top, bottom in vertical
    }

    owner: Dict[Tuple[int, int], int] = {}
    cells: List[_Cell] = []

    for top in range(row_count):
        for left in range(col_count):
            if (top, left) in owner:
                continue

            right = left + 1
            while right < col_count and not _has_v_block(vertical_blocks, left, top, right):
                right += 1

            bottom = top + 1
            while bottom < row_count and not _has_h_block(horizontal_blocks, top, left, bottom):
                bottom += 1

            if not _rectangle_is_closed(horizontal_blocks, vertical_blocks, top, bottom, left, right):
                return None

            cell_id = len(cells)
            for row in range(top, bottom):
                for col in range(left, right):
                    if (row, col) in owner:
                        return None
                    owner[(row, col)] = cell_id

            cells.append(
                _Cell(
                    rows[top],
                    rows[bottom],
                    cols[left],
                    cols[right],
                )
            )

    if len(owner) != row_count * col_count:
        return None

    for row, left, right in horizontal_blocks:
        if row == 0:
            continue
        if row == row_count:
            continue
        for col in range(left, right):
            if (row - 1, col) not in owner:
                return None
            if (row, col) not in owner:
                return None
            if owner[(row - 1, col)] == owner[(row, col)]:
                return None
    for col, top, bottom in vertical_blocks:
        if col == 0:
            continue
        if col == col_count:
            continue
        for row in range(top, bottom):
            if (row, col - 1) not in owner:
                return None
            if (row, col) not in owner:
                return None
            if owner[(row, col - 1)] == owner[(row, col)]:
                return None

    return cells


def _has_v_block(
    blocks: Set[Tuple[int, int, int]], col: int, top: int, edge_col: int
) -> bool:
    return any(
        block_col == edge_col and block_top <= top < block_bottom
        for block_col, block_top, block_bottom in blocks
    )


def _has_h_block(
    blocks: Set[Tuple[int, int, int]], row: int, left: int, edge_row: int
) -> bool:
    return any(
        block_row == edge_row and block_left <= left < block_right
        for block_row, block_left, block_right in blocks
    )


def _rectangle_is_closed(
    horizontal: Set[Tuple[int, int, int]],
    vertical: Set[Tuple[int, int, int]],
    top: int,
    bottom: int,
    left: int,
    right: int,
) -> bool:
    for edge_row in (top, bottom):
        covered = {
            col
            for edge_left, edge_right in (
                (edge_left, edge_right)
                for row, edge_left, edge_right in horizontal
                if row == edge_row
            )
            for col in range(edge_left, edge_right)
        }
        if not set(range(left, right)).issubset(covered):
            return False

    for edge_col in (left, right):
        covered = {
            row
            for edge_top, edge_bottom in (
                (edge_top, edge_bottom)
                for col, edge_top, edge_bottom in vertical
                if col == edge_col
            )
            for row in range(edge_top, edge_bottom)
        }
        if not set(range(top, bottom)).issubset(covered):
            return False

    return True


def _grid_header_rows(grid: _Grid) -> range:
    if grid.header_bottom is None:
        return range(0, 0)

    bottom_index = grid.rows.index(grid.header_bottom)
    return range(0, bottom_index)


def _parse_cells(
    block: "BlockParser",
    state: "BlockState",
    lines: Sequence[str],
    grid: _Grid,
    logical_rows: range,
    head: bool,
) -> Optional[List[Dict[str, Any]]]:
    row_index = {value: index for index, value in enumerate(grid.rows)}
    col_index = {value: index for index, value in enumerate(grid.cols)}
    tokens: List[Dict[str, Any]] = []

    for logical_row in logical_rows:
        row_cells = sorted(
            (
                cell
                for cell in grid.cells
                if row_index[cell.top] == logical_row
            ),
            key=lambda cell: cell.left,
        )

        row_children: List[Dict[str, Any]] = []
        for cell in row_cells:
            text = _cell_text(lines, cell)
            child = state.child_state(text)
            block.parse(child)
            attrs = {
                "head": head,
                "colspan": col_index[cell.right] - col_index[cell.left],
                "rowspan": row_index[cell.bottom] - row_index[cell.top],
            }
            row_children.append(
                {"type": "grid_table_cell", "attrs": attrs, "children": child.tokens}
            )

        tokens.append({"type": "grid_table_row", "children": row_children})

    return tokens


def _cell_text(lines: Sequence[str], cell: _Cell) -> str:
    content: List[str] = []

    for row in range(cell.top + 1, cell.bottom):
        line = lines[row][cell.left + 1 : cell.right].rstrip(" \t")
        if line.startswith(" "):
            line = line[1:]
        content.append(line)

    return "\n".join(content).strip("\n")


def render_grid_table(renderer: "BaseRenderer", text: str, cols: int) -> str:
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
    attrs = ""
    if colspan > 1:
        attrs += ' colspan="' + str(colspan) + '"'
    if rowspan > 1:
        attrs += ' rowspan="' + str(rowspan) + '"'
    return "  <" + tag + attrs + ">" + text + "</" + tag + ">\n"


def render_markdown_grid_table(
    renderer: "MarkdownRenderer", token: Dict[str, Any], state: "BlockState"
) -> str:
    lines = ["[grid-table]"]
    sections = token.get("children", [])
    for section in sections:
        section_type = section.get("type")
        for row in section.get("children", []):
            for cell in row.get("children", []):
                attrs = cell.get("attrs", {})
                labels = []
                if section_type == "grid_table_head":
                    labels.append("th")
                colspan = attrs.get("colspan", 1)
                rowspan = attrs.get("rowspan", 1)
                if colspan > 1:
                    labels.append("colspan=" + str(colspan))
                if rowspan > 1:
                    labels.append("rowspan=" + str(rowspan))

                text = renderer.render_children(cell, state).strip()
                if labels:
                    text = "[" + " ".join(labels) + "]\n" + text
                lines.append(indent(text, "    "))
    return "\n".join(lines) + "\n\n"


def grid_table(md: "Markdown") -> None:
    """Parse reStructuredText-style grid tables with row and column spans."""
    md.block.register("grid_table", GRID_TABLE_PATTERN, parse_grid_table, before="paragraph")

    if md.renderer and md.renderer.NAME == "html":
        md.renderer.register("grid_table", render_grid_table)
        md.renderer.register("grid_table_head", render_grid_table_head)
        md.renderer.register("grid_table_body", render_grid_table_body)
        md.renderer.register("grid_table_row", render_grid_table_row)
        md.renderer.register("grid_table_cell", render_grid_table_cell)
    elif md.renderer and md.renderer.NAME == "markdown":
        md.renderer.register("grid_table", render_markdown_grid_table)


def grid_table_in_quote(md: "Markdown") -> None:
    md.block.insert_rule(md.block.block_quote_rules, "grid_table", before="paragraph")


def grid_table_in_list(md: "Markdown") -> None:
    md.block.insert_rule(md.block.list_rules, "grid_table", before="paragraph")

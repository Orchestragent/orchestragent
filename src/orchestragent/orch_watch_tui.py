"""Terminal view for `orch watch`: the primary, browser-free experience.

`render` is pure: model + clock + terminal size + capabilities -> a character/style grid.
The curses driver only paints that grid, reads keys and advances the clock, so layout is
tested without a terminal. Capabilities degrade from 256 colours + Unicode box drawing to
8 colours, monochrome (NO_COLOR) and ASCII; small terminals get a compact list, and a
missing TTY (or `--plain`) gets a line-per-event text stream with the same information.
"""

import locale
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, TextIO

try:
    import curses
except ImportError:  # pragma: no cover - Python builds without _curses
    curses = None  # type: ignore[assignment]

from orchestragent.orch_watch import Json, clean, flying, state_at, stats, view

# --- capabilities and glyphs -------------------------------------------------------


@dataclass(frozen=True)
class Caps:
    colors: int = 256  # 256, 8 or 0 (monochrome)
    unicode: bool = True


def detect(env: dict[str, str], encoding: str, colors: int, has_colors: bool,
           ascii_only: bool = False) -> Caps:
    """Pick capabilities from the environment; never more than the terminal reports."""
    utf = "utf8" in encoding.lower().replace("-", "").replace("_", "")
    depth = 0 if env.get("NO_COLOR") or not has_colors else 256 if colors >= 256 else (
        8 if colors >= 8 else 0)
    return Caps(depth, utf and not ascii_only)


def choose(*, web: bool, plain: bool, stdout_tty: bool, stdin_tty: bool, term: str) -> str:
    """web | plain | tui. The browser is opt-in; plain text is the only fallback."""
    if web:
        return "web"
    if plain or not stdout_tty or not stdin_tty or term in {"", "dumb", "unknown"}:
        return "plain"
    return "tui"


UP, DOWN, LEFT, RIGHT = 1, 2, 4, 8
JOIN_UNI = {LEFT | RIGHT: "─", UP | DOWN: "│", DOWN | RIGHT: "┌", DOWN | LEFT: "┐",
            UP | RIGHT: "└", UP | LEFT: "┘", UP | DOWN | RIGHT: "├", UP | DOWN | LEFT: "┤",
            DOWN | LEFT | RIGHT: "┬", UP | LEFT | RIGHT: "┴", UP | DOWN | LEFT | RIGHT: "┼",
            LEFT: "─", RIGHT: "─", UP: "│", DOWN: "│"}


@dataclass(frozen=True)
class Glyphs:
    box: str  # tl tr bl br h v
    sel: str
    msg: str
    trail: str
    bar: str
    live_bar: str
    span: str
    cursor: str
    spark: str
    states: dict[str, str]

    def join(self, bits: int, unicode: bool) -> str:
        if unicode:
            return JOIN_UNI.get(bits, "┼")
        if bits & (UP | DOWN) and bits & (LEFT | RIGHT):
            return "+"
        return "|" if bits & (UP | DOWN) else "-"


UNI = Glyphs("┌┐└┘─│", "╔╗╚╝═║", "●", "•", "█", "▒", "▁", "│", " ▁▂▃▄▅▆▇█", {
    "idle": "○", "planned": "·", "ready": "◌", "running": "●", "verifying": "◆",
    "merging": "»", "done": "✓", "failed": "✗", "parked": "‖", "replanned": "↻"})
ASC = Glyphs("++++-|", "####=#", "o", ".", "#", "=", "_", "|", " .:-=+*#%", {
    "idle": " ", "planned": ".", "ready": "r", "running": "*", "verifying": "v",
    "merging": "m", "done": "+", "failed": "x", "parked": "P", "replanned": "~"})
BUSY = frozenset({"running", "verifying", "merging"})
SHORT = {"running": "run", "verifying": "test", "merging": "merg", "planned": "plan",
         "replanned": "repl", "parked": "park", "failed": "fail", "ready": "redy"}

# style -> (256-colour fg, 8-colour fg, attributes). Background stays the terminal's own.
BLACK, RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN, WHITE = range(8)
PALETTE: dict[str, tuple[int, int, tuple[str, ...]]] = {
    "state.idle": (240, WHITE, ("dim",)), "state.planned": (245, WHITE, ("dim",)),
    "state.ready": (110, BLUE, ()), "state.running": (81, CYAN, ("bold",)),
    "state.verifying": (141, MAGENTA, ("bold",)), "state.merging": (214, YELLOW, ("bold",)),
    "state.done": (78, GREEN, ()), "state.failed": (203, RED, ("bold",)),
    "state.parked": (208, YELLOW, ("underline",)), "state.replanned": (246, WHITE, ("dim",)),
    "msg.request": (81, CYAN, ("bold",)), "msg.result": (78, GREEN, ("bold",)),
    "msg.retry": (213, MAGENTA, ("bold",)), "msg.error": (203, RED, ("bold",)),
    "msg.context": (220, YELLOW, ("bold",)), "msg.verify": (141, MAGENTA, ("bold",)),
    "msg.testfail": (204, RED, ("bold",)), "msg.merge": (214, YELLOW, ("bold",)),
    "msg.done": (78, GREEN, ("bold",)), "msg.dispatch": (75, BLUE, ("bold",)),
    "msg.plan": (177, MAGENTA, ("bold",)),
    "edge": (238, WHITE, ("dim",)), "edge.dep": (60, BLUE, ("dim",)),
    "edge.claude": (173, RED, ()), "edge.deepseek": (69, BLUE, ("bold",)),
    "edge.active": (81, CYAN, ()),
    "bar.claude": (173, RED, ()), "bar.deepseek": (69, BLUE, ()), "bar.other": (150, GREEN, ()),
    "bar.verify": (141, MAGENTA, ()), "bar.merge": (214, YELLOW, ()),
    "label": (255, WHITE, ("bold",)), "text": (252, WHITE, ()), "dim": (244, WHITE, ("dim",)),
    "header": (255, WHITE, ("bold", "reverse")), "title": (117, CYAN, ("bold",)),
    "cursor": (226, YELLOW, ("bold",)), "critical": (227, YELLOW, ("bold",)),
    "spark": (81, CYAN, ()), "warn": (214, YELLOW, ("bold",)), "sel": (231, WHITE, ("bold",)),
    "cost": (114, GREEN, ()), "key": (117, CYAN, ("bold",)),
}
MESSAGE_KINDS = ("request", "result", "retry", "context", "testfail", "error", "verify",
                 "merge", "done")


def palette(caps: Caps) -> dict[str, tuple[int, tuple[str, ...]]]:
    """style -> (foreground or -1 for none, attributes) for the given colour depth."""
    result: dict[str, tuple[int, tuple[str, ...]]] = {}
    for style, (rich, basic, attrs) in PALETTE.items():
        colour = rich if caps.colors >= 256 else basic if caps.colors >= 8 else -1
        if caps.colors == 0 and style.startswith(("msg.", "state.running", "cursor")):
            attrs = (*attrs, "bold")
        result[style] = (colour, attrs)
    return result


# --- the grid -----------------------------------------------------------------------


TRANSLIT = str.maketrans({"·": ".", "…": "~", "∫": "int", "→": ">", "←": "<", "×": "x",
                          "¥": "Y", "—": "-", "–": "-", "“": '"', "”": '"', "‘": "'", "’": "'"})


def ascii_text(text: str) -> str:
    """ASCII spelling of display text; may change its length, so apply before fitting."""
    if text.isascii():
        return text
    return "".join(c if c.isascii() else "?" for c in text.translate(TRANSLIT))


def ascii_model(model: Json) -> Json:
    """Node labels in ASCII (e.g. the integration shard "design T-004 int") before layout."""
    nodes = [{**node, "label": ascii_text(str(node["label"])), "sub": ascii_text(str(node["sub"]))}
             for node in model["nodes"]]
    return {**model, "nodes": nodes}


class Grid:
    def __init__(self, width: int, height: int, ascii_only: bool = False) -> None:
        self.width, self.height, self.ascii_only = width, height, ascii_only
        self.chars = [[" "] * width for _ in range(height)]
        self.styles = [[""] * width for _ in range(height)]

    def put(self, x: int, y: int, text: str, style: str = "text") -> int:
        if not 0 <= y < self.height:
            return x
        if self.ascii_only and not text.isascii():
            text = "".join(c if c.isascii() else (ascii_text(c) or "?")[0] for c in text)
        for char in text:
            if 0 <= x < self.width:
                self.chars[y][x], self.styles[y][x] = char, style
            x += 1
        return x

    def lines(self) -> list[str]:
        return ["".join(row) for row in self.chars]

    def runs(self, y: int) -> Iterator[tuple[int, str, str]]:
        """(x, text, style) chunks of one row for painting."""
        start = 0
        for x in range(1, self.width + 1):
            if x == self.width or self.styles[y][x] != self.styles[y][start]:
                yield start, "".join(self.chars[y][start:x]), self.styles[y][start]
                start = x


# --- formatting -------------------------------------------------------------------


def clock_text(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes = f"{rest // 60:02d}:{rest % 60:02d}"
    return f"{hours}:{minutes}" if hours else minutes


def duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m{int(seconds % 60):02d}s"
    return f"{int(seconds // 3600)}h{int(seconds % 3600 // 60):02d}m"


def amount(tokens: int) -> str:
    if tokens >= 1_000_000:
        return f"{tokens / 1e6:.1f}M"
    if tokens >= 1000:
        return f"{tokens / 1e3:.1f}k"
    return str(tokens)


def cost(cny_cents: float, usd: float, caps: Caps) -> str:
    parts = []
    if cny_cents:
        parts.append(("¥" if caps.unicode else "CNY") + f"{cny_cents / 100:.2f}")
    if usd:
        parts.append(f"${usd:.2f}")
    return " ".join(parts)


def brief_cost(fact: Json, caps: Caps) -> str:
    """One currency for tight cells: the CNY ledger if any, else the USD estimate."""
    if fact["cny_cents"]:
        return ("¥" if caps.unicode else "Y") + f"{float(fact['cny_cents']) / 100:.2f}"
    return f"${float(fact['usd']):.2f}" if fact["usd"] else ""


def fit(text: str, width: int) -> str:
    if width <= 0:
        return ""
    return text if len(text) <= width else text[: max(0, width - 1)] + "~"


# --- UI state ---------------------------------------------------------------------


@dataclass
class Ui:
    mode: str = "live"  # live | replay
    speed: float = 8.0
    paused: bool = False
    selected: str = "orch"
    detail: bool = False
    message: str = ""


@dataclass(frozen=True)
class Box:
    x: int
    y: int
    w: int
    h: int
    col: int

    @property
    def mid(self) -> int:
        return self.y + self.h // 2

    def contains(self, x: int, y: int) -> bool:
        return self.x <= x < self.x + self.w and self.y <= y < self.y + self.h


@dataclass
class Layout:
    boxes: dict[str, Box]
    edges: dict[tuple[str, str], list[tuple[int, int]]] = field(default_factory=dict)
    columns: list[list[str]] = field(default_factory=list)


def columns(model: Json) -> list[list[str]]:
    nodes = model["nodes"]
    group = {str(n["id"]): str(n["group"]) for n in nodes}
    tasks = [n for n in nodes if n["group"] == "task"]
    depth = max((int(n["depth"]) for n in tasks), default=-1)
    cols: list[list[str]] = [["cli"], ["orch"],
                             ["planner", *[str(n["id"]) for n in nodes if n["group"] == "design"]]]
    for level in range(depth + 1):
        cols.append([str(n["id"]) for n in tasks if int(n["depth"]) == level])
    cols.append(["guard", "sandbox", "merge"])
    others = [i for i, g in group.items() if g == "provider" and i not in {"claude", "deepseek"}]
    cols.append(["claude", "deepseek", *others])
    return [col for col in cols if col]


def layout(model: Json, x0: int, y0: int, width: int, height: int) -> Layout | None:
    """Columns of 3-row boxes, or None when the terminal is too small for the graph."""
    cols = columns(model)
    gap = 3
    labels = {str(n["id"]): str(n["label"]) for n in model["nodes"]}
    widths = [max(16, *(len(labels.get(i, i)) + 4 for i in col)) for col in cols]
    room = width - gap * (len(cols) - 1)
    while sum(widths) > room and max(widths) > 14:
        widest = widths.index(max(widths))
        widths[widest] -= 1
    if sum(widths) > room:
        return None
    spare = room - sum(widths)
    while spare > 0 and min(widths) < 24:
        for index in sorted(range(len(widths)), key=lambda i: widths[i]):
            if spare and widths[index] < 24:
                widths[index] += 1
                spare -= 1
    if max(len(col) for col in cols) * 4 - 1 > height or height < 15:
        return None
    used = sum(widths) + gap * (len(cols) - 1)
    lefts = [x0 + max(0, (width - used) // 2) + sum(widths[:i]) + gap * i
             for i in range(len(cols))]
    boxes: dict[str, Box] = {}

    def stack(index: int, ids: list[str], align: str) -> None:
        spacing = 2 if len(ids) * 5 - 2 <= height else 1
        total = len(ids) * (3 + spacing) - spacing
        top = {"top": y0, "bottom": y0 + height - total,
               "center": y0 + (height - total) // 2}[align]
        for k, node_id in enumerate(ids):
            boxes[node_id] = Box(lefts[index], top + k * (3 + spacing), widths[index], 3, index)

    last = len(cols) - 1
    for index, col in enumerate(cols):
        x, w = lefts[index], widths[index]
        if col[0] in {"cli", "orch"}:
            stack(index, col, "center")
        elif col[0] == "planner":
            stack(index, col, "top")
        elif col[0] == "guard":
            boxes["guard"] = Box(x, y0 + 4, w, 3, index)
            boxes["merge"] = Box(x, y0 + height - 3, w, 3, index)
            boxes["sandbox"] = Box(x, max(y0 + 8, y0 + height - 7), w, 3, index)
        elif index == last:
            for k, node_id in enumerate(col):
                boxes[node_id] = Box(x, y0 + 4 * k, w, 3, index)
        else:
            stack(index, col, "bottom")
    if len({(b.x, b.y) for b in boxes.values()}) != len(boxes):
        return None
    result = Layout(boxes, columns=cols)
    for edge in model["edges"]:
        a, b = str(edge["from"]), str(edge["to"])
        if a in boxes and b in boxes:
            result.edges[(a, b)] = route(boxes[a], boxes[b], gap)
    return result


def route(a: Box, b: Box, gap: int) -> list[tuple[int, int]]:
    """Orthogonal cells from box a to box b through the channel right of the left box."""
    flip = b.col < a.col
    first, second = (b, a) if flip else (a, b)
    start = first.x + first.w
    channel = start + (gap // 2 if second.col != first.col else 1)
    cells = [(x, first.mid) for x in range(start, channel)]
    step = 1 if second.mid >= first.mid else -1
    cells += [(channel, y) for y in range(first.mid, second.mid, step)]
    end = second.x - 1 if second.col != first.col else second.x + second.w
    step = 1 if end >= channel else -1
    cells += [(x, second.mid) for x in range(channel, end + step, step)]
    return list(reversed(cells)) if flip else cells


def path_cells(lay: Layout, path: list[str]) -> list[tuple[int, int]]:
    cells: list[tuple[int, int]] = []
    for a, b in zip(path, path[1:], strict=False):
        if (a, b) in lay.edges:
            cells += lay.edges[(a, b)]
        elif (b, a) in lay.edges:
            cells += list(reversed(lay.edges[(b, a)]))
    return cells


# --- rendering ----------------------------------------------------------------------


def node_lines(model: Json, t: float) -> dict[str, Json]:
    """Per node display facts at `t`: state, since, info, stats."""
    counted = stats(model, t)
    result: dict[str, Json] = {}
    for node in model["nodes"]:
        state, since, info = state_at(node["history"], t)
        first = next((float(h[0]) for h in node["history"] if h[1] in BUSY), None)
        if state in BUSY and since is not None:
            elapsed = t - since
        elif first is not None and since is not None and state in {"done", "failed", "parked"}:
            elapsed = since - first
        else:
            elapsed = None
        result[str(node["id"])] = {"node": node, "state": state, "since": since, "info": info,
                                   "elapsed": elapsed, **counted[str(node["id"])]}
    return result


def render(model: Json | None, t: float, width: int, height: int, caps: Caps, ui: Ui,
           messages: list[tuple[Json, float]] | None = None) -> Grid:
    """The whole screen as a grid. `messages` are (message, progress 0..1) in flight."""
    grid = Grid(width, height, ascii_only=not caps.unicode)
    glyphs = UNI if caps.unicode else ASC
    if model is None:
        grid.put(1, 0, fit("orch watch: waiting for an orchestration run ...", width - 2), "title")
        return grid
    if width < 40 or height < 10:
        grid.put(0, 0, fit("orch watch: terminal too small", width), "warn")
        grid.put(0, 1, fit(f"{width}x{height}; need 40x10", width), "dim")
        return grid
    if not caps.unicode:
        model = ascii_model(model)
    facts = node_lines(model, t)
    header(grid, model, t, caps, ui)
    footer(grid, ui, caps)
    detail_rows = 3
    lanes = gantt_lanes(model)
    graph_top = 2
    lay: Layout | None = None
    # The graph wins over the timeline: shrink the Gantt strip before going compact.
    for gantt_rows in range(min(len(lanes) + 2, max(3, (height - 6) // 3)), 3, -1):
        graph_bottom = height - 2 - detail_rows - gantt_rows - 1
        lay = layout(model, 1, graph_top, width - 2, graph_bottom - graph_top)
        if lay is not None:
            break
    if lay is None:
        free = height - 2 - detail_rows - 1 - graph_top  # rows between header and detail pane
        listed = len(compact_rows(facts, compact_columns(width), messages or []))
        gantt_rows = max(0, min(len(lanes) + 2, free - listed - 1))
        graph_bottom = height - 2 - detail_rows - gantt_rows - (1 if gantt_rows else 0)
        compact(grid, model, caps, facts, messages or [], graph_top, graph_bottom)
    else:
        graph(grid, model, t, caps, glyphs, facts, lay, ui, messages or [])
    if gantt_rows >= 3:
        gantt(grid, model, t, caps, glyphs, lanes, 1, graph_bottom + 1, width - 2, gantt_rows,
              ui)
    detail_pane(grid, model, t, caps, facts, ui, height - 1 - detail_rows, detail_rows)
    if ui.detail:
        overlay(grid, model, t, caps, glyphs, facts, ui)
    return grid


def header(grid: Grid, model: Json, t: float, caps: Caps, ui: Ui) -> None:
    seen = view(model, t)
    grid.put(0, 0, " " * grid.width, "header")
    run = model["run"]
    mode = (f"REPLAY {ui.speed:g}x" if ui.mode == "replay" else "LIVE") + (
        " PAUSED" if ui.paused else "")
    parts = [" orch watch", f"run {run.get('id', '?')}", f"phase {seen['phase']}",
             clock_text(seen["elapsed"]),
             f"calls {seen['calls']} ({len(seen['in_flight'])} in flight)",
             f"out {amount(seen['output'])} tok",
             cost(seen["cny_cents"], seen["usd"], caps) or "",
             mode]
    grid.put(0, 0, fit(" | ".join(p for p in parts if p), grid.width), "header")


def footer(grid: Grid, ui: Ui, caps: Caps) -> None:
    keys = "Tab/arrows select  Enter details  p pause  +/- speed  r restart  q quit"
    if ui.mode == "live":
        keys = "Tab/arrows select  Enter details  p pause/resume live  q quit"
    x = grid.put(1, grid.height - 1, fit(keys, grid.width - 2), "dim")
    legend = [(kind, kind if kind != "context" else "NEED_FILES") for kind in
              ("request", "result", "retry", "context", "testfail")]
    x += 2
    for kind, name in legend:
        if x + len(name) + 3 >= grid.width:
            break
        x = grid.put(x, grid.height - 1, (UNI if caps.unicode else ASC).msg, f"msg.{kind}")
        x = grid.put(x + 1, grid.height - 1, name, "dim") + 1
    if ui.message:
        grid.put(max(0, grid.width - len(ui.message) - 1), grid.height - 1, ui.message, "warn")


def graph(grid: Grid, model: Json, t: float, caps: Caps, glyphs: Glyphs, facts: dict[str, Json],
          lay: Layout, ui: Ui, messages: list[tuple[Json, float]]) -> None:
    bits: dict[tuple[int, int], int] = {}
    style: dict[tuple[int, int], str] = {}
    kinds = {(str(e["from"]), str(e["to"])): str(e["kind"]) for e in model["edges"]}
    active: dict[tuple[str, str], str] = {}
    for call in flying(model, t):
        hops = [str(call["node"]), *map(str, call["via"])]
        tone = "edge.claude" if call["provider"] == "claude" else (
            "edge.deepseek" if call["provider"] == "dsh" else "edge.active")
        for a, b in zip(hops, hops[1:], strict=False):
            active[(a, b)] = active[(b, a)] = tone
    for (a, b), cells in lay.edges.items():
        tone = active.get((a, b)) or ("edge.dep" if kinds.get((a, b)) == "dep" else "edge")
        for index, cell in enumerate(cells):
            prev = cells[index - 1] if index else None
            nxt = cells[index + 1] if index + 1 < len(cells) else None
            mask = 0
            for other in (prev, nxt):
                if other is None:
                    continue
                dx, dy = other[0] - cell[0], other[1] - cell[1]
                mask |= RIGHT if dx > 0 else LEFT if dx < 0 else DOWN if dy > 0 else UP
            if prev is None or nxt is None:
                mask |= LEFT | RIGHT if mask & (LEFT | RIGHT) or not mask else mask
            bits[cell] = bits.get(cell, 0) | mask
            if style.get(cell, "edge") in {"edge", "edge.dep"}:
                style[cell] = tone
    for (x, y), mask in bits.items():
        grid.put(x, y, glyphs.join(mask, caps.unicode), style[(x, y)])
    for node_id, box in lay.boxes.items():
        draw_box(grid, box, facts[node_id], caps, glyphs, node_id == ui.selected)
    for message, progress in messages:
        cells = path_cells(lay, [str(n) for n in message["path"]])
        if not cells:
            continue
        index = min(len(cells) - 1, int(progress * len(cells)))
        tone = f"msg.{message['kind']}" if f"msg.{message['kind']}" in PALETTE else "msg.request"
        for back, char in ((2, glyphs.trail), (1, glyphs.trail), (0, glyphs.msg)):
            if index - back < 0:
                continue
            x, y = cells[index - back]
            if not any(box.contains(x, y) for box in lay.boxes.values()):
                grid.put(x, y, char, tone)


def draw_box(grid: Grid, box: Box, fact: Json, caps: Caps, glyphs: Glyphs, selected: bool) -> None:
    node = fact["node"]
    state = str(fact["state"])
    tone = f"state.{state}"
    edge = glyphs.sel if selected else glyphs.box
    tl, tr, bl, br, h, v = edge[0], edge[1], edge[2], edge[3], edge[4], edge[5]
    border = "sel" if selected else tone
    inner = box.w - 2
    label = fit(str(node["label"]), inner - 2)
    grid.put(box.x, box.y, tl + h * inner + tr, border)
    grid.put(box.x + 1, box.y, f" {label} ", "critical" if node.get("critical") else "label")
    elapsed = duration(float(fact["elapsed"])) if fact["elapsed"] is not None else ""
    if node["group"] in {"guard", "provider", "sandbox", "merge"}:
        tail = f"x{fact['live']}" if fact["live"] > 1 else ""
    else:
        attempt = str(fact["info"]) if state in BUSY and node["group"] == "task" else ""
        tail = " ".join(part for part in (attempt, elapsed) if part)
    tail = tail[: max(0, inner - 3)]
    body = f"{glyphs.states.get(state, '?')} {state}"
    if len(body) + 1 + len(tail) > inner:
        body = f"{glyphs.states.get(state, '?')} {SHORT.get(state, state)}"
    body = body[: max(1, inner - len(tail) - 1)]
    line = body + " " * max(1, inner - len(body) - len(tail)) + tail
    grid.put(box.x, box.y + 1, v, border)
    grid.put(box.x + 1, box.y + 1, fit(line, inner).ljust(inner), tone)
    grid.put(box.x + box.w - 1, box.y + 1, v, border)
    spent = brief_cost(fact, caps)
    out = amount(int(fact["output"])) if fact["output"] else ""
    options = [" ".join(p for p in (f"{out} out" if out else "", spent) if p),
               " ".join(p for p in (out, spent) if p), f"{out} out" if out else spent, out]
    bottom = next((o for o in options if len(o) <= inner - 2), fit(options[0], inner - 2))
    grid.put(box.x, box.y + 2, bl + h * inner + br, border)
    if bottom:
        grid.put(box.x + 1, box.y + 2, f" {bottom} ", "cost")


SECTIONS = (("control", ("user", "orch")), ("planning", ("planner", "design")),
            ("build", ("task",)), ("infrastructure", ("guard", "sandbox", "merge")),
            ("providers", ("provider",)))


def compact_columns(width: int) -> int:
    return max(1, min(3, (width - 2) // 39))


def compact_rows(facts: dict[str, Json], per_row: int,
                 messages: list[tuple[Json, float]]) -> list[list[Any]]:
    """Section titles on their own row, members flowing across `per_row` cells."""
    rows: list[list[Any]] = []
    for title, groups in SECTIONS:
        members = [f for f in facts.values() if f["node"]["group"] in groups]
        if members:
            rows.append([title])
            rows += [members[i: i + per_row] for i in range(0, len(members), per_row)]
    return rows + [[track] for track in messages[:3]]


def compact(grid: Grid, model: Json, caps: Caps, facts: dict[str, Json],
            messages: list[tuple[Json, float]], top: int, bottom: int) -> None:
    """Small terminals: grouped node lines in columns, plus message tracks."""
    glyphs = UNI if caps.unicode else ASC
    labels = {str(n["id"]): str(n["label"]) for n in model["nodes"]}
    per_row = compact_columns(grid.width)
    cell = (grid.width - 2) // per_row
    rows = compact_rows(facts, per_row, messages)
    y = top
    for index, row in enumerate(rows):
        if y >= bottom:
            break
        if y == bottom - 1 and index < len(rows) - 1:
            grid.put(1, y, f"... {len(rows) - index} more rows (enlarge the terminal)", "dim")
            break
        first = row[0]
        if isinstance(first, str):
            grid.put(1, y, first.upper(), "title")
            if index == 0:  # column hint for the cells below
                for k in range(per_row):
                    grid.put(1 + k * cell + 20, y, "  time" + "out".rjust(6) + " cost", "dim")
        elif isinstance(first, tuple):
            message, progress = first
            path = [str(n) for n in message["path"]]
            a, b = fit(labels.get(path[0], path[0]), 14), fit(labels.get(path[-1], path[-1]), 14)
            span = max(6, grid.width - 34)
            track = [glyphs.trail] * span
            track[min(span - 1, int(progress * span))] = glyphs.msg
            x = grid.put(1, y, a.rjust(14), "text")
            x = grid.put(x + 1, y, "".join(track), f"msg.{message['kind']}")
            grid.put(x + 1, y, b, "text")
        else:
            for k, fact in enumerate(row):
                compact_cell(grid, fact, caps, glyphs, 1 + k * cell, y, cell)
        y += 1


def compact_cell(grid: Grid, fact: Json, caps: Caps, glyphs: Glyphs, x: int, y: int,
                 width: int) -> None:
    state = str(fact["state"])
    end = x + width
    x = grid.put(x, y, glyphs.states.get(state, "?"), f"state.{state}")
    label = str(fact["node"]["label"])
    if len(label) > 13:
        label = label.replace("design ", "d:")  # "d:T-004 int" keeps the shard suffix visible
    x = grid.put(x + 1, y, fit(label, 13).ljust(13), "label")
    x = grid.put(x + 1, y, SHORT.get(state, state)[:4].ljust(4), f"state.{state}")
    if fact["node"]["group"] in {"guard", "provider", "sandbox", "merge"}:
        elapsed = f"x{fact['live']}" if fact["live"] > 1 else ""
    else:
        elapsed = duration(float(fact["elapsed"])) if fact["elapsed"] is not None else ""
    used = amount(int(fact["output"])) if fact["output"] else ""
    x = grid.put(x, y, fit(f"{elapsed:>6}{used:>6}", max(0, end - x)), "text")
    spent = brief_cost(fact, caps)
    if len(spent) <= end - x - 1:
        grid.put(x + 1, y, spent, "cost")


LANE_ORDER = {"planner": 0, "design": 1, "task": 2, "orch": 3, "sandbox": 4}


def gantt_lanes(model: Json) -> list[str]:
    owners = {str(c["node"]) for c in model["calls"]} | {str(i["node"]) for i in model["intervals"]}
    nodes = [n for n in model["nodes"] if str(n["id"]) in owners]
    nodes.sort(key=lambda n: (LANE_ORDER.get(str(n["group"]), 9), int(n["order"])))
    return [str(n["id"]) for n in nodes]


def gantt(grid: Grid, model: Json, t: float, caps: Caps, glyphs: Glyphs, lanes: list[str],
          x0: int, y0: int, width: int, rows: int, ui: Ui) -> None:
    """Parallelism sparkline, one lane per worker, an axis and the clock cursor."""
    t0 = float(model["t0"])
    end = float(model["t1"]) if ui.mode == "replay" else max(float(model["t1"]), t)
    span = max(1.0, end - t0)
    label_w = 16
    bar_w = max(5, width - label_w - 1)
    left = x0 + label_w + 1

    def col(at: float) -> int:
        return left + min(bar_w - 1, max(0, int((at - t0) / span * bar_w)))

    labels = {str(n["id"]): (str(n["label"]), bool(n["critical"])) for n in model["nodes"]}
    calls = [c for c in model["calls"] if float(c["start"]) <= t]
    counts = [0] * bar_w
    for call in calls:
        stop = t if call["end"] is None or float(call["end"]) > t else float(call["end"])
        for cell in range(col(float(call["start"])) - left, col(stop) - left + 1):
            counts[cell] += 1
    peak = max(counts) or 1
    grid.put(x0, y0, "parallel".ljust(label_w), "dim")
    top = len(glyphs.spark) - 1
    spark = "".join(glyphs.spark[max(1, min(top, round(c / peak * top)))] if c else " "
                    for c in counts)
    grid.put(left, y0, spark, "spark")
    grid.put(max(left, left + bar_w - 8), y0, f" peak {max(counts)}", "dim")
    shown = lanes[: max(0, rows - 2)]
    for row, lane in enumerate(shown, start=1):
        y = y0 + row
        name, critical = labels.get(lane, (lane, False))
        grid.put(x0, y, fit(name, label_w).ljust(label_w), "critical" if critical else "dim")
        for item in model["intervals"]:
            if item["node"] != lane or float(item["start"]) > t:
                continue
            stop = t if item["end"] is None or float(item["end"]) > t else float(item["end"])
            for x in range(col(float(item["start"])), col(stop) + 1):
                tone = "bar.merge" if item["kind"] == "merge" else "bar.verify"
                grid.put(x, y, glyphs.span, tone)
        for call in calls:
            if call["node"] != lane:
                continue
            done = call["end"] is not None and float(call["end"]) <= t
            stop = float(call["end"]) if done else t
            tone = {"claude": "bar.claude", "dsh": "bar.deepseek"}.get(str(call["provider"]),
                                                                    "bar.other")
            if done and call["outcome"] != "ok":
                tone = "state.failed"
            char = glyphs.bar if done else glyphs.live_bar
            for x in range(col(float(call["start"])), col(stop) + 1):
                grid.put(x, y, char, "critical" if call["critical"] and done and caps.colors == 0
                         else tone)
    if len(lanes) > len(shown):
        grid.put(x0, y0 + rows - 1, f"+{len(lanes) - len(shown)} lanes", "dim")
    axis_y = y0 + min(rows - 1, len(shown) + 1)
    for k in range(0, bar_w, max(10, bar_w // 6)):
        label = "+" + clock_text(span * k / bar_w)
        if k + len(label) <= bar_w:
            grid.put(left + k, axis_y, label, "dim")
    cursor = col(t)
    for y in range(y0, axis_y):
        if 0 <= cursor < grid.width and grid.chars[y][cursor] == " ":
            grid.put(cursor, y, glyphs.cursor, "cursor")


def detail_pane(grid: Grid, model: Json, t: float, caps: Caps, facts: dict[str, Json], ui: Ui,
                y: int, rows: int) -> None:
    fact = facts.get(ui.selected)
    if fact is None:
        return
    node = fact["node"]
    rule = ("─" if caps.unicode else "-") * (grid.width - 2)
    grid.put(1, y, rule, "dim")
    state = str(fact["state"])
    info = "" if str(fact["info"]) == state else str(fact["info"])
    x = grid.put(3, y, f" {node['label']} ", "sel")
    x = grid.put(x, y, f" {state} {info} ".replace("  ", " "), f"state.{state}")
    grid.put(x, y, fit(f" {node['sub']} ", grid.width - x - 2), "dim")
    # The CLI and the scheduler make no model calls themselves: show the run's dispatches.
    calls = (sum(float(c["start"]) <= t for c in model["calls"])
             if node["id"] in {"cli", "orch"} else fact["calls"])
    numbers = [f"calls {calls}", f"{fact['live']} in flight" if fact["live"] else "",
               f"in {amount(int(fact['input']))} / out {amount(int(fact['output']))} tok"
               if fact["output"] or fact["input"] else "",
               cost(float(fact["cny_cents"]), float(fact["usd"]), caps),
               f"last {fact['tps']} tok/s" if fact["tps"] else "",
               f"elapsed {duration(float(fact['elapsed']))}" if fact["elapsed"] is not None else "",
               "critical path" if node.get("critical") else ""]
    grid.put(3, y + 1, fit("  ".join(p for p in numbers if p), grid.width - 4), "text")
    entries = [e for e in model["log"] if e["node"] == node["id"] and float(e["at"]) <= t]
    if not entries and node["id"] in {"cli", "orch"}:
        entries = [e for e in model["log"] if float(e["at"]) <= t]
    if entries and rows > 2:
        last = entries[-1]
        since = clock_text(float(last["at"]) - float(model["t0"]))
        text = f"+{since} {last['kind']} {last['summary']}"
        grid.put(3, y + 2, fit(text, grid.width - 4), "dim")


def overlay(grid: Grid, model: Json, t: float, caps: Caps, glyphs: Glyphs,
            facts: dict[str, Json], ui: Ui) -> None:
    fact = facts.get(ui.selected)
    if fact is None:
        return
    node = fact["node"]
    lines: list[tuple[str, str]] = [(f"{node['label']}  ({node['group']})", "label"),
                                    (str(node["sub"]), "dim")]
    state = str(fact["state"])
    lines.append((f"state {state} {fact['info']}".strip(), f"state.{state}"))
    meta = node.get("meta") or {}
    for key in ("goal", "files", "reqs", "depends_on", "attempts", "level", "reason", "commit"):
        value = meta.get(key)
        if value not in (None, "", [], 0):
            shown = ", ".join(map(str, value)) if isinstance(value, list) else str(value)
            lines.append((f"{key}: {clean(shown, 200)}", "text"))
    calls = [c for c in model["calls"] if float(c["start"]) <= t
             and (c["node"] == node["id"] or node["id"] in c["via"])]
    if calls:
        lines.append(("calls:", "title"))
    for call in calls[-12:]:
        live = call["end"] is None or float(call["end"]) > t
        took = (t if live else float(call["end"])) - float(call["start"])
        counts = call["tokens"]
        spent = cost(float(call["cny_cents"] or 0), float(call["usd"] or 0), caps)
        parts = [f"{call['purpose']} a{call['attempt']}",
                 "in flight" if live else str(call["outcome"]), duration(took),
                 f"in {amount(int(counts['input'] or 0))} / out "
                 f"{amount(int(counts['output'] or 0))} tok" if counts["output"] and not live
                 else "",
                 f"{call['tps']} tok/s" if call["tps"] and not live else "",
                 "" if live else spent, "critical" if call["critical"] else ""]
        lines.append(("  " + "  ".join(p for p in parts if p),
                      "state.running" if live else "critical" if call["critical"] else "text"))
    entries = [e for e in model["log"] if e["node"] == node["id"] and float(e["at"]) <= t
               and e["seq"] != -1]
    if entries:
        lines.append(("events:", "title"))
    for entry in entries[-8:]:
        lines.append((f"  +{clock_text(float(entry['at']) - float(model['t0']))} {entry['kind']} "
                      f"{entry['summary']}", "dim"))
    width = min(grid.width - 4, 96)
    height = min(grid.height - 4, len(lines) + 2)
    x0, y0 = (grid.width - width) // 2, (grid.height - height) // 2
    box = glyphs.sel
    grid.put(x0, y0, box[0] + box[4] * (width - 2) + box[1], "sel")
    for row in range(1, height - 1):
        grid.put(x0, y0 + row, box[5] + " " * (width - 2) + box[5], "sel")
        if row - 1 < len(lines):
            text, tone = lines[row - 1]
            grid.put(x0 + 2, y0 + row, fit(text, width - 4), tone)
    grid.put(x0, y0 + height - 1, box[2] + box[4] * (width - 2) + box[3], "sel")
    grid.put(x0 + 2, y0, " details (Enter/Esc to close) ", "title")


# --- plain line stream ------------------------------------------------------------


def settled(model: Json, now: float) -> float:
    """A finished run's clock stops at its last record instead of running on forever."""
    return min(now, float(model["t1"])) if not model["live"] else now


def plain_line(model: Json, entry: Json, unicode: bool = True) -> str:
    labels = {str(n["id"]): str(n["label"]) for n in model["nodes"]}
    who = labels.get(str(entry["node"]), str(entry["node"]))
    summary = str(entry["summary"])
    if not unicode:
        who, summary = ascii_text(who), ascii_text(summary)
    at = clock_text(float(entry["at"]) - float(model["t0"]))
    return f"[+{at}] {fit(who, 18):<18} {entry['kind']:<22} {summary}".rstrip()


def status_line(model: Json, t: float, caps: Caps) -> str:
    seen = view(model, t)
    return (f"status run={model['run'].get('id', '?')} phase={seen['phase']} "
            f"elapsed={clock_text(seen['elapsed'])} calls={seen['calls']} "
            f"in_flight={len(seen['in_flight'])} input_tokens={amount(seen['input'])} "
            f"output_tokens={amount(seen['output'])} "
            f"cost={cost(seen['cny_cents'], seen['usd'], caps) or '0'}")


def entry_key(entry: Json) -> tuple[object, ...]:
    return entry["kind"], entry["seq"], entry.get("call", ""), entry["at"]


def plain(load: Callable[[], Json | None], out: TextIO, *, replay: bool, speed: float,
          poll: float = 0.5, sleep: Callable[[float], None] = time.sleep,
          clock: Callable[[], float] = time.time, ascii_only: bool = False) -> int:
    """One line per event (same facts as the TUI); follows a live run until it ends."""
    encoding = (getattr(out, "encoding", "") or "").lower()
    caps = Caps(0, not ascii_only and encoding.startswith("utf"))
    model = load()
    if model is None:
        out.write("orch watch: waiting for an orchestration run (orch plan) ...\n")
        out.flush()
    while model is None:
        sleep(poll)
        model = load()
    out.write(f"orch watch (plain) run {model['run'].get('id', '?')}"
              f"{' replay ' + format(speed, 'g') + 'x' if replay else ''}\n")
    printed: set[tuple[object, ...]] = set()
    if replay:
        previous = float(model["t0"])
        for entry in model["log"]:
            sleep(min(2.0, max(0.0, (float(entry["at"]) - previous) / max(speed, 0.01))))
            previous = float(entry["at"])
            out.write(plain_line(model, entry, caps.unicode) + "\n")
            out.flush()
        out.write(status_line(model, float(model["t1"]), caps) + "\n")
        return 0
    phase = ""
    while True:
        now = settled(model, clock())
        for entry in model["log"]:
            key = entry_key(entry)
            if key not in printed and float(entry["at"]) <= now:
                printed.add(key)
                out.write(plain_line(model, entry, caps.unicode) + "\n")
        seen = view(model, now)
        if seen["phase"] != phase:
            phase = str(seen["phase"])
            out.write(status_line(model, now, caps) + "\n")
        out.flush()
        if model["terminal"] and not seen["in_flight"]:
            return 0
        sleep(poll)
        model = load() or model


# --- curses driver ------------------------------------------------------------------


class Player:
    """Clock + message animation state shared by the TUI frames."""

    LIFE = 1.4  # real seconds a message glyph takes to travel its path

    def __init__(self, model: Json, ui: Ui, now: Callable[[], float]) -> None:
        self.ui, self.now = ui, now
        self.model = model
        self.t = float(model["t0"]) if ui.mode == "replay" else settled(model, now())
        self.last = time.monotonic()
        self.cursor = self.t
        self.alive: dict[int, tuple[Json, float]] = {}

    def advance(self) -> float:
        real = time.monotonic()
        step, self.last = real - self.last, real
        if not self.ui.paused:
            if self.ui.mode == "replay":
                self.t = min(float(self.model["t1"]), self.t + step * self.ui.speed)
            else:
                self.t = settled(self.model, self.now())
        if self.t < self.cursor:
            self.alive.clear()
        for message in self.model["messages"]:
            if self.cursor < float(message["at"]) <= self.t and len(self.alive) < 48:
                self.alive[int(message["id"])] = (message, real)
        self.cursor = self.t
        return self.t

    def messages(self) -> list[tuple[Json, float]]:
        real = time.monotonic()
        result = []
        for key, (message, born) in list(self.alive.items()):
            progress = (real - born) / self.LIFE
            if progress >= 1:
                del self.alive[key]
            else:
                result.append((message, progress))
        return result

    def restart(self) -> None:
        self.t = self.cursor = float(self.model["t0"])
        self.alive.clear()


def selectable(model: Json) -> list[str]:
    return [node for col in columns(model) for node in col]


def move(model: Json, selected: str, key: str) -> str:
    cols = columns(model)
    order = [node for col in cols for node in col]
    if selected not in order:
        return order[0] if order else selected
    index = order.index(selected)
    if key in {"tab", "down"}:
        return order[(index + 1) % len(order)]
    if key in {"btab", "up"}:
        return order[(index - 1) % len(order)]
    where = next(i for i, col in enumerate(cols) if selected in col)
    target = where + (1 if key == "right" else -1)
    if not 0 <= target < len(cols):
        return selected
    row = cols[where].index(selected)
    return cols[target][min(row, len(cols[target]) - 1)]


def tui(load: Callable[[], Json | None], *, replay: bool, speed: float, ascii_only: bool,
        env: dict[str, str], encoding: str) -> int:
    """Run the curses UI; raises curses.error if the terminal cannot be initialised."""
    if curses is None:
        raise RuntimeError("this Python has no curses module")

    locale.setlocale(locale.LC_ALL, "")

    def main(screen: Any) -> int:
        has, colours, background = False, 0, -1
        try:
            if curses.has_colors():
                curses.start_color()
                try:
                    curses.use_default_colors()
                    background = -1
                except curses.error:
                    background = BLACK
                has, colours = True, curses.COLORS
        except curses.error:
            has = False
        caps = detect(env, encoding, colours, has, ascii_only)
        attrs = attributes(caps, background if has else -1)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        screen.keypad(True)
        screen.timeout(80)
        try:
            curses.set_escdelay(25)
        except (AttributeError, curses.error):
            pass
        model = load()
        ui = Ui(mode="replay" if replay else "live", speed=speed)
        player: Player | None = None
        last_poll = 0.0
        while True:
            if (model is None or not replay) and time.monotonic() - last_poll > 0.5:
                last_poll = time.monotonic()
                fresh = load()
                if fresh is not None:
                    model = fresh
                    if player is not None:
                        player.model = fresh
            if model is not None and player is None:
                player = Player(model, ui, time.time)
            t = player.advance() if player else time.time()
            if player is not None and replay and t >= float(player.model["t1"]):
                ui.message = "replay finished: r restarts"
            elif ui.paused:
                ui.message = "paused: p resumes" + (" live" if not replay else "")
            else:
                ui.message = ""
            height, width = screen.getmaxyx()
            grid = render(model, t, width, height, caps, ui, player.messages() if player else [])
            paint(screen, grid, attrs)
            key = screen.getch()
            if key == -1:
                continue
            name = {curses.KEY_UP: "up", curses.KEY_DOWN: "down", curses.KEY_LEFT: "left",
                    curses.KEY_RIGHT: "right", 9: "tab", curses.KEY_BTAB: "btab"}.get(key)
            if key in (ord("q"), ord("Q")):
                return 0
            if key == curses.KEY_RESIZE:
                screen.clear()
            elif name and model is not None and not ui.detail:
                ui.selected = move(model, ui.selected, name)
            elif key in (10, 13, curses.KEY_ENTER):
                ui.detail = not ui.detail
            elif key == 27:
                ui.detail = False
            elif key in (ord("p"), ord(" ")):
                ui.paused = not ui.paused
            elif key in (ord("+"), ord("=")):
                ui.speed = min(256.0, ui.speed * 2)
            elif key in (ord("-"), ord("_")):
                ui.speed = max(0.25, ui.speed / 2)
            elif key in (ord("r"), ord("R")) and player is not None and replay:
                player.restart()

    result: int = curses.wrapper(main)
    return result


def attributes(caps: Caps, background: int) -> dict[str, int]:
    names = {"bold": curses.A_BOLD, "dim": curses.A_DIM, "reverse": curses.A_REVERSE,
             "underline": curses.A_UNDERLINE}
    result: dict[str, int] = {}
    pairs = getattr(curses, "COLOR_PAIRS", 0)
    for index, (style, (colour, flags)) in enumerate(palette(caps).items(), start=1):
        value = 0
        for flag in flags:
            value |= names[flag]
        if colour >= 0 and index < pairs:
            try:
                curses.init_pair(index, colour, background)
                value |= curses.color_pair(index)
            except curses.error:
                pass
        result[style] = value
    return result


def paint(screen: Any, grid: Grid, attrs: dict[str, int]) -> None:
    screen.erase()
    for y in range(grid.height):
        for x, text, style in grid.runs(y):
            if y == grid.height - 1 and x + len(text) >= grid.width:
                text = text[: max(0, grid.width - x - 1)]  # the last cell scrolls; skip it
            if not text:
                continue
            try:
                screen.addstr(y, x, text, attrs.get(style, 0))
            except curses.error:
                pass
    screen.refresh()

"""The repository as an agent reads it: list, find, search, read — pinned to a revision.

No language is parsed and nothing is precomputed. A model gets a directory listing with
sizes and asks for what it needs by name (find), by content (search) or by path and line
range (read), the way a person with a terminal would; each answer is small, uniform text.
Everything comes from git objects at one revision, so a candidate's context is exactly the
tree its checks ran against, and nothing a builder wrote leaks into another's prompt.
"""

from __future__ import annotations

import fnmatch
import re
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestragent.live_workspace import safe_relative
from orchestragent.serialization import dumps
from orchestragent.workspace import Workspace

MAX_REQUESTS = 8  # per round
MAX_HITS = 40  # search results per query
CONTEXT_LINES = 2
LISTING_BUDGET = 12_000  # bytes of directory listing shown up front
READ_LIMIT = 60_000  # bytes of one file returned to a model
TRUNCATED = "\n... [truncated by orch: {omitted} more bytes; the file continues]\n"

# What a model may ask for; the engine answers every item of a round in one message.
REQUEST_SCHEMA_ITEMS: dict[str, object] = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "find": {"type": "string"},      # a path pattern: *.go, router/**, ledger
            "search": {"type": "string"},    # a word or regular expression in file contents
            "read": {"type": "string"},      # a tracked path, optionally path:start-end
        },
        "additionalProperties": False,
    },
}
REQUEST_RULES = (
    "To look at the repository before answering, return status NEED_CONTEXT with up to "
    f"{MAX_REQUESTS} requests and nothing else: {{\"find\": \"<path pattern>\"}} lists "
    "matching tracked paths; {\"search\": \"<word or regex>\"} shows matching lines with "
    "their files and neighbours; {\"read\": \"<path>\"} or {\"read\": \"<path>:<start>-<end>\"} "
    "returns a file or a line range. Everything is at the revision you are working on. "
    "Ask for what you would open or grep yourself, then answer."
)


@dataclass(frozen=True)
class Hit:
    path: str
    line: int
    text: str


class Repo:
    """Revision-pinned reads of a git repository, cached per revision."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.lock = threading.Lock()
        self._trees: dict[str, dict[str, str]] = {}

    # --- git objects ------------------------------------------------------------------
    def tree(self, revision: str) -> dict[str, str]:
        """path -> blob id of every file at `revision` (submodules excluded)."""
        with self.lock:
            if revision in self._trees:
                return self._trees[revision]
        raw = Workspace.git_bytes(self.repo, "ls-tree", "-r", "-z", "--full-tree", revision)
        entries: dict[str, str] = {}
        for item in raw.split(b"\0"):
            if not item:
                continue
            meta, _, name = item.partition(b"\t")
            _, kind, blob = meta.decode().split()
            if kind == "blob":
                entries[name.decode("utf-8", "surrogateescape")] = blob
        with self.lock:
            if len(self._trees) > 64:
                self._trees.clear()
            self._trees[revision] = entries
        return entries

    def blobs(self, ids: Iterable[str]) -> dict[str, bytes]:
        wanted = sorted(set(ids))
        if not wanted:
            return {}
        out = Workspace.git_bytes(
            self.repo, "cat-file", "--batch", stdin=("\n".join(wanted) + "\n").encode())
        found: dict[str, bytes] = {}
        position = 0
        while position < len(out):
            newline = out.index(b"\n", position)
            header = out[position:newline].decode().split()
            position = newline + 1
            if len(header) != 3:
                continue  # "<id> missing"
            size = int(header[2])
            found[header[0]] = out[position:position + size]
            position += size + 1
        return found

    def sizes(self, revision: str) -> dict[str, int]:
        raw = Workspace.git_bytes(self.repo, "ls-tree", "-r", "-l", "-z", "--full-tree",
                                  revision)
        out: dict[str, int] = {}
        for item in raw.split(b"\0"):
            if not item:
                continue
            meta, _, name = item.partition(b"\t")
            parts = meta.decode().split()
            if len(parts) == 4 and parts[1] == "blob" and parts[3].isdigit():
                out[name.decode("utf-8", "surrogateescape")] = int(parts[3])
        return out

    def read(self, revision: str, paths: Iterable[str] | None = None) -> dict[str, str]:
        """File contents at `revision` in one batch; missing paths are left out."""
        tree = self.tree(revision)
        names = [p for p in (tree if paths is None else paths) if p in tree]
        raw = self.blobs(tree[name] for name in names)
        return {name: raw[tree[name]].decode("utf-8", "replace") for name in sorted(names)}

    # --- what a model sees up front -----------------------------------------------------
    def listing(self, revision: str, budget: int = LISTING_BUDGET) -> str:
        """The tree as `path  size` lines, directories grouped; large directories are cut
        with a count so the shape is visible within the budget."""
        sizes = self.sizes(revision)
        by_dir: dict[str, list[tuple[str, int]]] = {}
        for path, size in sorted(sizes.items()):
            by_dir.setdefault(str(Path(path).parent), []).append((Path(path).name, size))
        lines: list[str] = [f"{len(sizes)} tracked files"]
        per_dir = max(8, budget // max(1, len(by_dir)) // 24)
        for folder in sorted(by_dir):
            entries = by_dir[folder]
            lines.append(f"{folder}/" if folder != "." else "./")
            for name, size in entries[:per_dir]:
                lines.append(f"  {name}  {size}")
            if len(entries) > per_dir:
                lines.append(f"  ... {len(entries) - per_dir} more files")
        text = "\n".join(lines)
        if len(text.encode()) > budget:
            text = text.encode()[:budget].decode("utf-8", "ignore") + "\n... (listing cut)"
        return text

    # --- requests ---------------------------------------------------------------------
    def find(self, revision: str, pattern: str) -> list[str]:
        """Tracked paths matching a glob (`*` within a name, `**` across directories) or
        containing the pattern as a substring."""
        paths = sorted(self.tree(revision))
        pattern = pattern.strip().lstrip("./")
        if any(ch in pattern for ch in "*?["):
            regex = re.compile(fnmatch.translate(pattern).replace(r"(?s:", "(?s:").replace(
                r"\*\*/", "(?:.*/)?"))  # fnmatch has no **; let it cross directories
            found = [p for p in paths if regex.match(p) or regex.match(Path(p).name)]
        else:
            found = [p for p in paths if pattern in p]
        return found[:200]

    def search(self, revision: str, query: str, *, limit: int = MAX_HITS,
               context: int = CONTEXT_LINES) -> str:
        """Lines matching `query` (a regular expression; a plain word works as itself) in
        the files at `revision`, with their neighbours, as `path:line: text` blocks."""
        query = query.strip()
        if not query:
            return "(empty query)"
        try:
            re.compile(query)
            flags = ["-E"]
        except re.error:
            flags = ["-F"]
        try:
            raw = Workspace.git(self.repo, "grep", "-n", "-I", "-i", *flags, "-e", query,
                                revision, "--", ".", strip=False)
        except Exception:  # noqa: BLE001 - no match exits 1; a bad pattern too
            return f"(no matches for {query!r})"
        hits: list[Hit] = []
        for line in raw.splitlines():
            # <revision>:<path>:<n>:<text>
            rest = line.split(":", 1)[1] if ":" in line else line
            path, _, tail = rest.partition(":")
            number, _, text = tail.partition(":")
            if number.isdigit():
                hits.append(Hit(path, int(number), text))
            if len(hits) >= limit:
                break
        if not hits:
            return f"(no matches for {query!r})"
        files = self.read(revision, {hit.path for hit in hits})
        blocks = []
        for hit in hits:
            lines = files.get(hit.path, "").splitlines()
            start, end = max(1, hit.line - context), min(len(lines), hit.line + context)
            body = "\n".join(f"{n:>5}{'>' if n == hit.line else ' '} {lines[n - 1]}"
                             for n in range(start, end + 1))
            blocks.append(f"{hit.path}:{hit.line}\n{body}")
        more = "" if len(hits) < limit else f"\n(first {limit} matches shown)"
        return "\n\n".join(blocks) + more

    def read_request(self, revision: str, spec: str) -> str:
        """`path` or `path:start-end` at `revision`, truncated for size."""
        path, _, span = spec.strip().partition(":")
        path = path.lstrip("./")
        if not safe_relative(path) or path not in self.tree(revision):
            return f"(no tracked file {path!r} at this revision)"
        text = self.read(revision, [path])[path]
        if span:
            match = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", span)
            if not match:
                return f"({spec!r}: a line range is start-end, e.g. {path}:10-80)"
            start, end = int(match.group(1)), int(match.group(2))
            lines = text.splitlines()
            chosen = lines[max(0, start - 1):end]
            text = "\n".join(f"{n:>5}  {line}" for n, line in enumerate(chosen, start))
        if len(text.encode()) > READ_LIMIT:
            text = text.encode()[:READ_LIMIT].decode("utf-8", "ignore") + TRUNCATED.format(
                omitted=len(text.encode()) - READ_LIMIT)
        return f"--- {spec.strip()} ---\n{text}"

    def answer(self, revision: str, requests: object) -> str:
        """Every request of one round, answered in order; malformed items are explained."""
        if not isinstance(requests, list) or not 1 <= len(requests) <= MAX_REQUESTS:
            raise ValueError(f"requests must be a list of 1 to {MAX_REQUESTS} items")
        parts = []
        for item in requests:
            if not isinstance(item, dict) or len(item) != 1:
                raise ValueError("each request is one of {find}, {search}, {read}")
            (kind, value), = item.items()
            if kind not in {"find", "search", "read"} or not isinstance(value, str) \
                    or not value.strip():
                raise ValueError(f"unknown request {dumps(item)}; use find, search or read")
            if kind == "find":
                found = self.find(revision, value)
                parts.append(f"$ find {value}\n" + ("\n".join(found) if found
                                                   else "(no tracked path matches)"))
            elif kind == "search":
                parts.append(f"$ search {value}\n" + self.search(revision, value))
            else:
                parts.append(f"$ read {value}\n" + self.read_request(revision, value))
        return "\n\n".join(parts)


def mentioned_paths(text: str, tree: Iterable[str]) -> list[str]:
    """Tracked paths a text names outright (a spec naming `api/ledger/parse.go`)."""
    paths = set(tree)
    words = set(re.findall(r"[\w./-]+\.[A-Za-z0-9]+", text)) | set(re.findall(r"[\w-]+/[\w./-]+",
                                                                                 text))
    found = {w.strip("./") for w in words if w.strip("./") in paths}
    found |= {p for p in paths if any(p.endswith("/" + w.strip("./")) for w in words)}
    return sorted(found)[:40]


def request_items(data: dict[str, Any]) -> object:
    return data.get("requests")

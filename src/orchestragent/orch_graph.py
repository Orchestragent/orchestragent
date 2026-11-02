"""Shared dependency reachability and replacement-output boundaries."""


def closure(graph: dict[str, list[str]], start: str) -> set[str]:
    """Every task `start` depends on, directly or transitively (cycles tolerated)."""
    seen: set[str] = set()
    stack = list(graph.get(start, []))
    while stack:
        task = stack.pop()
        if task not in seen and task in graph:
            seen.add(task)
            stack.extend(graph[task])
    return seen


def replacement_frontier(
    original: set[str], graph: dict[str, list[str]], owned: dict[str, list[str]],
) -> list[str]:
    """New helpers feed an original output owner; post-output chores are not code splits."""
    outputs = {path for paths in owned.values() for path in paths}
    errors = [f"replacement drops original owned files: {sorted(original - outputs)}"] if (
        original - outputs) else []
    anchors = {tid for tid, paths in owned.items() if original.intersection(paths)}
    contributes = anchors | set().union(*(closure(graph, tid) for tid in anchors))
    errors += [f"{tid}: replacement must contribute to an original output owner; "
               "test review and report-only work are controller operations"
               for tid in sorted(set(owned) - contributes)]
    return errors

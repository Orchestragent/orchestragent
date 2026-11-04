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

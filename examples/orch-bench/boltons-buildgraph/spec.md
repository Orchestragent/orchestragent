# Incremental, resource-aware build graphs in an existing Boltons checkout

Implement an experimental synchronous build-graph library in this pinned Boltons checkout.
It must compose existing utilities with new dependency, cache and execution algorithms.
Preserve upstream APIs, copyright headers and tests. No new runtime dependencies or services.
Threading during an explicit call is allowed; no persistent/background service is needed.
Choose cohesive tasks and their dependency graph. Reuse `cacheutils.LRU`,
`queueutils.HeapPriorityQueue`, and `fileutils.atomic_save` instead of copying their machinery.
Do not change packaging, collection hooks or existing tests. New tests must be root-level
stdlib unittest modules using the orchestrator's test_rN_* requirement naming convention.

R1. Add `dependency_layers(graph)` to `boltons.iterutils`. Input is a mapping from nonempty
string IDs to iterables of prerequisite IDs. Return a list of lists, each containing all
currently dependency-free IDs in original mapping insertion order. Dependencies must name
known IDs; duplicate prerequisite references are deduplicated. Empty graph returns [].
Reject malformed IDs, a string as a prerequisite iterable, missing prerequisites, self edges
and cycles with ValueError. Validate the whole graph and do not mutate caller data.

R2. Add `SingleFlightCache(max_size=128)` to `boltons.cacheutils`, backed by the existing LRU.
`get_or_compute(key, factory)` accepts hashable keys and a zero-argument callable. Return a
cached value or compute it. Concurrent misses for the same key share one in-flight result
and return the same object; different keys can compute concurrently. Factory exceptions reach
all current waiters, preserve exception type/message, are not cached, and permit later retry.
Do not hold a global cache lock while running user code or waiting. Recursive computation of
the same key in its owning thread raises RuntimeError instead of deadlocking. `invalidate(key)`
returns whether a cached or in-flight key existed; it prevents that in-flight result from
entering the cache, while its existing callers may still receive it. `clear()` invalidates
all cached and in-flight keys. Invalid max_size (bool, non-int, <1) raises ValueError.

R3. Add `BuildGraph(tasks)` to `boltons.buildgraphutils`. `tasks` is an insertion-ordered mapping
of nonempty string IDs to dictionaries with exactly `deps`, `version`, `resources`:
deps is a list/tuple of prerequisite IDs, version is a nonempty string, resources maps nonempty
string resource names to exact positive integers (bool rejected). Use dependency_layers to
validate the full graph. Snapshot caller data. Expose `ids` as a tuple in insertion order,
`layers()` as dependency layers, `spec(task_id)` as a defensive copy, and `affected(changed)`
as all changed IDs plus transitive dependents in graph insertion order; unknown IDs raise
ValueError. Do not expose mutable internal graph state.

`fingerprint(task_id, dependency_keys)` returns a lowercase SHA-256 hex digest. dependency_keys
must have exactly the node's deduplicated prerequisite IDs, each with a lowercase 64-hex key;
otherwise ValueError. Hash UTF-8 json.dumps of this exact object with sort_keys=True,
separators=(',', ':'), ensure_ascii=False, allow_nan=False:
`{"id": task_id, "version": version, "resources": resources,
  "dependencies": [[dep, dependency_keys[dep]], ...]}`
The dependencies list follows declared prerequisite order after first-occurrence deduplication.
Thus changing a version affects that node and descendants without invalidating unrelated nodes.

R4. Add `ResultStore(path)` to `boltons.buildcacheutils`, using fileutils.atomic_save.
A nonexistent file starts empty; an existing file must be a JSON object with exactly
`schema_version: 1` and `values`, a mapping of lowercase 64-hex keys to JSON values.
Reject malformed/unsupported state with ValueError and preserve the file. `get(key)` returns
a JSON round-trip copy or raises KeyError when absent; `put(key, value)` validates the key and
json.dumps(value, allow_nan=False) before changing memory, and stores a round-trip copy.
Invalid keys, nonserializable values and NaN/infinite values raise ValueError.
`invalidate(keys)` removes these keys and returns their removed count. `save()` atomically
writes the complete state as UTF-8 JSON. No implicit writes during load/put/invalidate.
Caller mutations must not change stored values. A failed validation/save preserves an existing
destination byte-for-byte and cleans up atomic-save temporary files.

R5. Add `BuildRunner(graph, functions, store=None, max_workers=4, capacities=None, retries=0)`
to `boltons.buildrunutils`. graph is a BuildGraph; functions maps exactly its IDs to callables.
max_workers is an exact positive integer, retries an exact nonnegative integer; bool rejected.
capacities defaults to {}, mapping nonempty resource names to exact positive integers.
Reject missing/invalid functions, undeclared resource capacities, or a node requiring more than
capacity before any callback. Each callback receives a fresh dict of its direct prerequisite
results and must return a value serializable by json.dumps(allow_nan=False). Results are
normalized by JSON round trip. Changes a callback makes to its input must not alter another
node's cached or returned result. Use HeapPriorityQueue for ready admission; ties follow graph
insertion order. Run independent eligible nodes concurrently, subject to max_workers and the
sum of active demands for each resource. Release resources even on callback/validation failure.

R6. `runner.run(targets=None)` synchronously returns a dict with exactly `results`, `states`,
`errors`, `attempts`, `max_active`. Default targets selects all nodes; an iterable of targets
selects just their prerequisite closure. Validate targets before callbacks; a string or unknown
ID is invalid (ValueError), duplicates are accepted. The four per-node dictionaries retain
selected graph insertion order. states is `done`, `cached`, `failed`, or `blocked`; results
contains successful selected nodes only, errors failed nodes only. attempts counts actual
callback invocations (zero for reused or blocked work). max_active is the observed peak number
of callbacks active during this run, zero if all selected work was reused. Empty selection
returns four empty dicts and max_active=0. Dependency results become visible only after their
callback completes successfully. A failed node blocks its transitive dependents; unrelated
components still finish. No spin/deadlock for blocked or resource-constrained graphs.

R7. A callback exception or non-JSON result retries up to `retries` additional times, releasing
resources between attempts; other nodes can proceed. Exhausted failures are reported as
`ClassName: message`, never cached. Use SingleFlightCache for memory reuse between sequential
calls on one runner; use ResultStore when supplied. Cache keys are derived with BuildGraph's
fingerprint from the current node version and direct prerequisite keys, including for reused
dependencies. No callback runs on a cache hit. Return defensive result copies on every run.
The runner need not support simultaneous calls to its own run method, but it must clean up all
work before returning and be reusable after a failed run.

R8. Successful results from all components, including a run with failed/blocked nodes, are put
into a supplied ResultStore and saved once at the end. Reopening the store and a new runner
must reuse those successes. Changing one node version must recompute exactly that node and its
selected descendants, retaining independent cached results. Store I/O failures may propagate
to the caller, with atomic-save guarantees preserved. Neither failures nor blocked nodes may
become successful cache entries. Expose accurate states/attempts/max_active rather than counting
scheduled futures as completed work. Preserve original upstream behavior and regression tests.

# Resumable JSONL task queue in an existing Boltons checkout

Implement a durable, deduplicating work queue for an append-only JSONL input file. This is an
experimental feature in this checkout, not an upstream-approved feature request. Preserve existing
Boltons behavior and tests. Use the existing queue, JSON and atomic file helpers instead of creating
parallel implementations. No third-party runtime dependencies, external services or background threads.

The existing repository is pinned separately in the evaluation manifest. The relevant existing APIs
are `boltons.queueutils.BasePriorityQueue`, `HeapPriorityQueue`, `SortedPriorityQueue`,
`boltons.jsonutils.JSONLIterator`, and `boltons.fileutils.atomic_save` / `AtomicSaver`.
`atomic_save(path)` is a context manager yielding a binary file by default; its exception path
protects an existing destination. Queue implementations share a stable priority/counter backend and
use tombstones for removed entries. Inspect actual code before changing it; preserve copyright headers.

Deliver these connected requirements. Choose the task decomposition and dependency graph yourself.
Modify the existing modules where specified, and integrate them in one new module. Do not add copies
of the upstream modules or change collection hooks, packaging configuration or pre-existing tests.
New frozen tests should be stdlib unittest classes in root-level `test_*.py` modules, using the
orchestrator's `test_rN_*` requirement naming convention. Existing upstream pytest tests also run.

R1. In `boltons/queueutils.py`, add `BasePriorityQueue.peek_many(count=None)` and `compact()`.
`peek_many` returns active tasks in precisely the order repeated `pop` would return them, without
mutating the queue or invoking the user's priority_key again. None means all; exact nonnegative int
means at most that many; bool, negative and other types raise ValueError. Removed entries and replaced
priorities must not appear. Stable FIFO ties and custom priority_key must work on both backends.
`compact` removes tombstones from the existing backend, returns the number removed, preserves active
ordering and backend type, and is idempotent. Existing add/remove/peek/pop semantics stay unchanged.

R2. In `boltons/jsonutils.py`, add `JSONLCheckpointIterator(file_obj, offset=0, ignore_errors=False)`.
It consumes a seekable binary stream forward from an absolute byte offset, yielding decoded JSON
values for complete newline-terminated UTF-8 records. CRLF is supported. Blank lines are skipped.
Its public `byte_offset` is the byte position after the last consumed complete line, including blank
lines or ignored malformed lines. An incomplete final line is left unconsumed: byte_offset remains at
that line's start. A new iterator at that checkpoint must read the line after bytes are appended.
Malformed UTF-8/JSON raises ValueError by default and does not advance byte_offset past that bad line;
ignore_errors=True skips it and advances. The supplied stream remains open. offset must be an exact
nonnegative int and at most the stream length, otherwise ValueError. At construction seek to offset;
starting offsets are caller-provided checkpoints, not heuristically rounded line positions.

R3. In `boltons/fileutils.py`, add `atomic_save_json(dest_path, value)`, reusing `atomic_save`.
Write one UTF-8 JSON document with deterministic sorted keys, compact separators, Unicode retained,
allow_nan=False, and one trailing newline. Return None. Invalid/nonserializable data or write failure
must leave an existing destination byte-for-byte unchanged and clean the temporary part file. Accept
str and pathlib.Path destinations. This helper is used by the integrated queue's save operation.

R4. Add `boltons/taskqueueutils.py` exporting `CheckpointQueue(path)`:
- Construction creates an empty in-memory queue when the checkpoint file does not exist; otherwise
  loads its JSON state. State has schema_version=1. Reject unsupported/malformed state with ValueError
  without modifying the checkpoint. No implicit save during construction or mutations.
- enqueue(task_id, payload, priority=0) returns True if accepted, False for an ID already pending,
  in-flight or completed. IDs are nonempty str; payload must be JSON-serializable with allow_nan=False;
  priority must be an exact int/float, finite, with bool rejected. Invalid input raises ValueError
  without changing state. Validation precedes duplicate detection. Larger priority goes first,
  with stable original insertion order for ties.
- peek(count=None) returns pending IDs without consuming, using the queueutils peek_many API.
- pop() moves the highest-priority pending item to in-flight and returns a dict with exactly
  id, payload, priority. Empty raises IndexError. ack(task_id) marks an in-flight ID completed;
  any other ID raises KeyError. Completed IDs remain deduplicated across saves and restarts.
- save() persists pending, in-flight, completed IDs, original insertion order and input offsets
  atomically using fileutils.atomic_save_json. Loading a saved queue recovers in-flight work as
  pending, maintaining original ordering for ties. Completed work must not be redelivered.
- ingest(file_obj, source_id) reads with JSONLCheckpointIterator from that source's saved byte offset.
  source_id is a nonempty str. Each record must be a dict with exactly id,payload,priority and follow
  enqueue validation. Return the count newly enqueued; duplicate valid IDs still advance the input
  offset. Complete records before a bad record remain accepted; a bad record raises ValueError and
  is not checkpointed, so it is retried next time. Blank lines can advance the offset; incomplete
  tails cannot. Different source IDs have independent offsets. save/restart must preserve offsets.

Compatibility matters: preserve unrelated APIs and pass the original upstream tests as well as
new feature tests. Do not hardcode acceptance examples. No shell/network access is needed by the feature.

# Meridian: a sharded, replicated key-value store in five languages

The repository holds five components, each in its own directory with its build files already
in place. Implement the requirements below and add tests in each component. Use only each
language's standard library plus what is already installed: Rust std only (no crates), Go std
only, Python std only, Java SE 21 std only (JUnit 5 is in `metrics/lib/junit.jar` for tests),
and in `client/` the installed `typescript`, `tsx`, `vitest` and `@types/node`.

- `node/`: Rust crate `mkv-node` (library `mkv_node` plus binary `mkv-node`), the storage node.
- `router/`: Go module `meridian/router`, the request router.
- `client/`: TypeScript package, the client library and command line.
- `planner/`: Python package `meridian_planner`, the rebalancing planner.
- `metrics/`: Java sources under `metrics/src` (package `meridian.metrics`), the metrics aggregator.

## Shared definitions

R1. Wire protocol MKV/1. Clients and servers talk over TCP on 127.0.0.1. Every request and
every response is one line of UTF-8 text ending in `\n` (a `\r` immediately before the `\n` is
ignored). A line holds tokens separated by single spaces. Every key, value, writer, node id or
error message inside a token is *encoded*: each byte of its UTF-8 form that is not in
`A-Z a-z 0-9 . _ ~ -` becomes `%` followed by two UPPERCASE hex digits; the empty string is
encoded as the single token `=` (which can never result from encoding, since `=` is always
`%3D`). Decoding is the exact inverse; a token with a `%` not followed by two hex digits (either
case), or that decodes to invalid UTF-8, is malformed. Versions and counts are base-10 integers
without sign or leading zeros (`0` itself allowed). Keys are 1 to 256 bytes (UTF-8); values are
0 to 65536 bytes. A request line longer than 1,048,576 bytes may be rejected with
`ERR TOOLARGE ...` and the connection closed. A request that is malformed in any way (unknown
verb, wrong token count, malformed token, bad number, key out of range) gets
`ERR BADREQ <encoded message>` and the connection stays open. Error lines are always
`ERR <CODE> <encoded message>` where CODE is one of `BADREQ`, `NOQUORUM`, `TOOLARGE`.

R2. Entries and ordering. A stored entry is either a value or a tombstone, with a version
(unsigned 64-bit) and a writer (a non-empty string). Entry A is *newer* than entry B when
A's version is greater, or the versions are equal and A's writer is greater in byte order.

R3. Placement. `score(node, key)` is the 64-bit FNV-1a hash (offset basis
0xcbf29ce484222325, prime 0x100000001b3, arithmetic modulo 2^64) of the UTF-8 bytes of
`node`, then one zero byte, then the UTF-8 bytes of `key`. `rank(key, nodes)` orders the node
ids by score descending, ties broken by node id in byte order ascending. The replicas of a key
are the first N ids of `rank(key, all node ids)` (all of them if there are fewer than N).

## Storage node (Rust, `node/`)

R4. Library API in crate `mkv_node`: `pub fn crc32(data: &[u8]) -> u32` computes CRC-32/IEEE
(reflected polynomial 0xEDB88320, initial value 0xFFFFFFFF, final XOR 0xFFFFFFFF; the check
value of `b"123456789"` is 0xCBF43926). Module `mkv_node::log` provides
`pub fn append(path: &std::path::Path, payload: &str) -> std::io::Result<()>` and
`pub fn replay(path: &std::path::Path) -> std::io::Result<Vec<String>>`. A log file is a
sequence of records: a 4-byte big-endian payload length, the payload bytes (UTF-8), and a
4-byte big-endian CRC-32 of the payload. `append` adds one record (creating the file if
needed) and flushes it to the OS before returning. `replay` returns the payloads of the records
in order (an absent file gives an empty list); at the first record that is incomplete or whose
CRC does not match, it truncates the file to the end of the last good record and returns the
good payloads.

R5. Node commands. The node serves these requests (all tokens encoded as in R1):
- `PUTV <key> <version> <writer> <value>`: store the value entry if it is newer (R2) than the
  current entry for the key (or there is none) and reply `OK <version>`; otherwise keep the
  current entry and reply `STALE <current version> <current writer>`.
- `DELV <key> <version> <writer>`: the same with a tombstone entry.
- `GETV <key>`: `VALUE <version> <writer> <value>`, `TOMB <version> <writer>`, or `NOTFOUND`.
- `SCAN <prefix> <limit>`: `KEYS <n> <key>...`, the keys with a value entry (not tombstones)
  that start with the prefix (`=` is the empty prefix), sorted by byte order, at most `limit`
  of them (1 to 1000; otherwise BADREQ).
- `STATS`: `STATS node=<id> keys=<n> puts=<n> dels=<n> gets=<n> scans=<n> lat=<c0>,<c1>,...,<c7>`
  in exactly this order: `keys` counts entries that are values, `puts`/`dels`/`gets`/`scans`
  count the PUTV/DELV/GETV/SCAN requests received since the node started (accepted, stale or
  not), and `lat` counts all requests other than STATS by the time from reading the request
  line to writing the response, in buckets with upper bounds 100, 250, 500, 1000, 2500, 5000
  and 10000 microseconds (inclusive) and a last bucket for longer ones.

R6. Durability. Every PUTV or DELV that changes the stored entry is appended to the log
`<dir>/log` (payload: the request line without its line ending) before the response is sent.
On start the node replays the log (R4) and applies the records in order with R5's rules, so
a node restarted on the same directory serves exactly the entries it had acknowledged, even
when the previous process was killed in the middle of writing a record.

R7. Binary `mkv-node --id <id> --port <port> --dir <dir>` (built by
`cargo build --release --offline` in `node/`; the binary is `node/target/release/mkv-node`).
It creates `<dir>` if needed, replays the log, listens on 127.0.0.1:`<port>` (`0` picks a free
port; a node killed and started again on the same port must be able to bind it at once, so the
listening socket allows address reuse), then prints exactly `READY <actual port>` on its own line on stdout and flushes it. It
serves many connections at once, each a sequence of request/response lines, until killed.

## Router (Go, `router/`)

R8. Package `meridian/router/hrw` (directory `router/hrw`) implements R3:
`func Score(node, key string) uint64` and `func Rank(key string, nodes []string) []string`
(returns a new slice; the input is not modified).

R9. Binary `mkv-router` (built by `go build -o bin/mkv-router ./cmd/mkv-router` in `router/`)
with flags `--id <router id> --port <port> --nodes <id>=<host>:<port>,... --replicas <N>
--write-quorum <W> --read-quorum <R>`. It listens on 127.0.0.1 (`0` picks a free port), prints
exactly `READY <actual port>` on stdout and flushes, and serves many client connections at once.
It talks to nodes with R5's commands over connections it may keep open and reuse (a reused
connection that turns out to be closed, for example because the node restarted, is replaced by
a fresh connection once before the node counts as failed); connecting to a node times out after
500 ms and each node request after 2 s; a node that fails or times out simply gives no response
for that request.

R10. Router commands for clients:
- `PUT <key> <value>`: send `GETV` to the key's N replicas; with fewer than R responses reply
  `ERR NOQUORUM ...`. Otherwise the new version is one more than the highest version among the
  responses (1 if there is none), the writer is the router id, and `PUTV` goes to all N
  replicas. Reply `OK <version>` when at least W replicas answered `OK`, else `ERR NOQUORUM ...`.
- `DEL <key>`: the same with `DELV`, replying `OK <version>`.
- `GET <key>`: send `GETV` to the N replicas; with fewer than R responses reply
  `ERR NOQUORUM ...`. Otherwise take the newest entry among the responses: reply
  `VALUE <version> <value>` for a value and `NOTFOUND` for a tombstone or when no replica has
  the key. Before replying, read repair: every responding replica whose entry is missing or
  older receives the newest entry (`PUTV` or `DELV`).
- `SCAN <prefix> <limit>`: send `SCAN` to every node; reply `KEYS ...` with the byte-ordered
  union of the keys from the nodes that answered, cut to `limit`; if no node answered,
  `ERR NOQUORUM ...`.
- `STATS`: `STATS router=<id> puts=<n> dels=<n> gets=<n> scans=<n>` (client requests received).
Requests violating R1 get `ERR BADREQ ...`.

## Client (TypeScript, `client/`)

R11. `client/src/protocol.ts` exports `encodeToken(s: string): string` and
`decodeToken(t: string): string` (R1; decoding a malformed token throws `ProtocolError`), the
class `ProtocolError extends Error`, and `parseResponse(line: string): Response` where
```ts
type Response =
  | { kind: "ok"; version: number }
  | { kind: "value"; version: number; value: string }
  | { kind: "notfound" }
  | { kind: "keys"; keys: string[] }
  | { kind: "stats"; fields: Record<string, string> }
  | { kind: "error"; code: string; message: string };
```
`parseResponse` accepts a line with or without its line ending; it decodes values, keys and
messages; `stats` maps each `name=value` token to its raw text. Anything else, including a
`KEYS` count that does not match the number of keys, throws `ProtocolError`.

R12. `client/src/client.ts` exports `class MkvClient` with
`constructor(options: { host: string; port: number; retries?: number; baseDelayMs?: number; timeoutMs?: number })`
(defaults: 3 retries, 50 ms, 2000 ms) and methods `put(key, value): Promise<number>` (the
version), `get(key): Promise<string | null>`, `del(key): Promise<number>`,
`scan(prefix, limit): Promise<string[]>` and `close(): Promise<void>`; and the error classes
`QuorumError`, `ServerError` (with a `code: string` property) and `ConnectionError`, all
extending `Error`. A request answered with `ERR NOQUORUM`, or one whose connection fails or
times out, is attempted again up to `retries` more times, waiting `baseDelayMs * 2^i`
milliseconds before retry i (0-based); when every attempt failed it rejects with
`QuorumError` or `ConnectionError` matching the last failure. Any other `ERR` rejects at once
with `ServerError`. A connection failure opens a new connection on the next attempt.

R13. Command line `npx tsx src/cli.ts --router <host>:<port> <command> ...` (run in
`client/`): `put <key> <value>` and `del <key>` print the version; `get <key>` prints the
value (exit status 1 and `not found` on stderr when absent); `scan <prefix> <limit>` prints
one key per line. Arguments are taken literally (no shell decoding); every line printed ends
with `\n`. Errors print the message on stderr with exit status 2.

## Planner (Python, `planner/`)

R14. `meridian_planner.hrw` provides `score(node: str, key: str) -> int` and
`rank(key: str, nodes: list[str]) -> list[str]` implementing R3 (the same results as R8).

R15. `meridian_planner.plan` provides
`plan(keys: list[str], old_nodes: list[str], new_nodes: list[str], replicas: int) -> list[dict]`.
For each distinct key whose replica set (R3, with N = `replicas`) differs between the old and
the new node lists, one move `{"key": k, "add": [...], "drop": [...], "source": s}`: `add` the
new replicas that were not old replicas, `drop` the old replicas that are not new replicas
(both sorted by byte order), and `source` the first old replica in rank order that is still in
`new_nodes`, or `None` when there is none. Moves are sorted by key in byte order (UTF-8).

R16. `python3 -m meridian_planner --replicas N --from a,b,c --to a,b,c,d` (run in `planner/`)
reads keys from stdin, one per line (a final line ending is optional; empty lines are
ignored), and prints each R15 move as one line of JSON with sorted keys and default
separators, then nothing else on stdout. Invalid arguments exit with status 2.

## Metrics (Java, `metrics/`)

R17. In package `meridian.metrics`: class `Stats` with
`public static java.util.Map<String, String> parse(String line)`, which reads an R5 or R10
STATS line (the first token is `STATS`, every other token is `name=value` with a non-empty
name; names are unique) into a map preserving token order, and throws
`IllegalArgumentException` otherwise. Class `PNCounter` with a public no-argument constructor,
`increment(String replica, long amount)`, `decrement(String replica, long amount)` (amounts
are non-negative, otherwise `IllegalArgumentException`), `merge(PNCounter other)` (keeps, per
replica, the larger increment total and the larger decrement total; merging is commutative,
associative and idempotent) and `long value()`.

R18. Class `Histogram` with a public no-argument constructor over R5's eight latency buckets,
`static Histogram fromCounts(long[] counts)` (exactly eight non-negative counts, otherwise
`IllegalArgumentException`), `merge(Histogram other)` (adds counts), `long total()` and
`long percentile(double p)` for 0 < p <= 100: the upper bound of the bucket holding the
nearest-rank sample (rank = ceil(p/100 * total), computed exactly rather than in floating point), `Long.MAX_VALUE` for the last bucket; with
no samples it throws `IllegalStateException`.

R19. `java -cp metrics/build meridian.metrics.Main <file>...` (classes compiled with
`javac -d metrics/build` from `metrics/src`): each file holds node STATS lines (R5), one per
line. It prints exactly one line:
`nodes=<distinct node ids> keys=<sum> puts=<sum> dels=<sum> gets=<sum> scans=<sum> p50_us=<v> p99_us=<v>`
where sums and percentiles use, per node id, its last line in the files (files in argument
order) and the percentiles come from the merged histograms (`inf` for the last bucket).

## The system works together

R20. With nodes started from `mkv-node`, a router from `mkv-router` over them, and the client
command line talking to the router: values written through any component are read back
exactly (any UTF-8, spaces, `=`, `%`, newlines inside values, empty values); each key's
entries sit exactly on the replicas R3 gives (as `meridian_planner.hrw.rank` computes them);
writes keep working while at most N - W replicas of a key are down, and reads while at most
N - R are; a node killed and restarted on the same directory keeps every acknowledged
write; a router GET after such a restart repairs the restarted node's stale entries; and the
metrics command reports the nodes' STATS correctly.

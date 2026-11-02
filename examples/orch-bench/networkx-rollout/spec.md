# A failure-domain-aware deployment control plane in NetworkX

Build an experimental deployment-planning and durable rollout library in this pinned
NetworkX checkout. This is an offline control plane: no network, subprocess, live service,
background daemon or new runtime dependency. Preserve existing APIs, packaging and tests.
Use NetworkX's DAG and minimum-cost-flow algorithms instead of implementing graph solvers
again. Choose cohesive tasks and internal helpers. The public modules below are required;
`networkx/deployment/__init__.py` must be empty. Other private modules are allowed.
Add root-level stdlib unittest tests named test_rN_behavior for RN.

R1. In `networkx.deployment.model`, implement `Model(services, hosts, network)` and
`canonical_json(value)`. IDs everywhere are nonempty strings, including Unicode and punctuation.
Exact integers below reject bool. services maps service IDs to dicts with exactly:
`replicas` (positive int), `requires` (list/tuple of prerequisite service IDs),
`allowed_zones` (None or list/tuple of zone IDs), and `costs` (all host IDs -> nonnegative ints).
hosts maps host IDs to dicts with exactly `zone` (nonempty str), `slots` (nonnegative int).
Deduplicate and sort requires/allowed_zones; reject unknown services/zones, self dependencies
and cycles. Empty services/hosts/network are valid together. Capacity infeasibility is checked
by placement, not Model. `network` must be a simple undirected nx.Graph without self-loops,
with string node IDs, containing every host. Each edge has an exact positive int `weight`.
Other node/edge/graph attributes are ignored in the normalized model.
Snapshot all input. `services`, `hosts`, `network` properties return defensive normalized copies.
`fingerprint` is SHA-256 of UTF-8 canonical_json of exactly
`{"services": normalized_services, "hosts": normalized_hosts,
  "network": {"nodes": sorted_node_ids, "edges": sorted_edges}}`,
where each edge is `[min_id,max_id,weight]`, lists sorted lexicographically.
canonical_json is json.dumps(sort_keys=True,separators=(',',':'),ensure_ascii=False,
allow_nan=False). Invalid model input raises ValueError, without changing caller data.

R2. `networkx.deployment.placement.place(model)` returns exactly
`{"placements": {service: sorted_host_list}, "cost": int}`. Place the requested replicas
with at most one replica of a service in each failure zone, respecting allowed_zones and
total host slots across all services. Minimize the sum of service/host costs globally using
nx.min_cost_flow (service -> service/zone -> host -> sink is one suitable formulation).
Every service appears, keys in lexical order. Any globally optimal assignment is valid
when tied. An infeasible model raises nx.NetworkXUnfeasible; do not silently use a partial
or greedy allocation. Empty model yields empty placements and zero cost. Do not mutate Model.

R3. `networkx.deployment.routing.route(model, source, target, k=1)` returns k simple,
pairwise undirected-edge-disjoint node paths minimizing total weight, sorted lexicographically.
Endpoints must be known network nodes; k is an exact positive integer. A local route
(source == target) returns [[source]], regardless of k, with zero cost. Unknown endpoints
or invalid k raise ValueError; insufficient paths raises nx.NetworkXUnfeasible. Use existing
NetworkX minimum-cost flow; all edge weights are positive so an optimal flow has no costful
cycles. Tied minimum-cost path sets need not match a particular tie-break. Preserve the model.

R4. `networkx.deployment.batches.rollout_batches(model, placements, max_parallel=4)` returns
ordered batches of service IDs. Validate a complete feasible placement (including replica
counts, distinct zones, allowed zones and host capacities) and exact positive max_parallel;
bad input raises ValueError before scheduling. A service's replicas move together, so reject
a service with more replicas than max_parallel. Start with no completed services. At each
batch, scan dependency-ready unfinished services in lexical order, admitting a service when
its replica count fits the remaining max_parallel and none of its hosts is already used in
this batch. Defer nonfitting services; do not block scanning later ready services. After the
batch all admitted services become completed. Only completed prerequisites unlock a later
batch. Return [] for an empty model; no input mutation or deadlock.

R5. `networkx.deployment.journal` provides `ConflictError`, `save_json(path,value)` and
`Journal(path)`. All values are normalized by strict JSON round trip; reject NaN/infinity and
nonserializable values with ValueError. save_json writes UTF-8 canonical_json atomically via
a same-directory temporary file and os.replace, cleaning temporary files on failure and
preserving an existing destination when serialization/write fails. Reuse this function
for all atomic JSON writes in this feature.
Journal.read() returns a defensive dict with exactly `schema:1, revision:N, events:[...]`.
A nonexistent journal is empty revision 0; read does not create the journal. Each record is
`{"revision": consecutive_int_starting_at_1, "event": {"id": nonempty_str,"payload": JSON_value}}`.
Reject malformed/unsupported files, duplicate IDs and nonconsecutive revisions with ValueError.
append(expected_revision,event) is serialized across independent Journal instances/processes
with an exclusive fcntl lock on a stable path+'.lock' sidecar. Same ID and normalized payload
is idempotent: return its ORIGINAL record revision, even if expected_revision is stale, with
no write. Same ID/different payload, or a new event with stale expected_revision, raises
ConflictError without changing the journal. A new event returns the new revision. Exact
nonnegative expected_revision rejects bool. Copy caller values; no lost updates or partial JSON.

R6. `networkx.deployment.planner.compile_plan(model, path_redundancy=1,max_parallel=4)` composes
R2-R4 and returns exactly `schema, id, deployment, dependencies, placements, cost, batches,
routes, path_redundancy, max_parallel`. schema=1, deployment=model.fingerprint; dependencies
maps each service to its sorted prerequisites. routes is a list of records with exactly
`producer,consumer,source,target,paths`, one for each direct dependency and every Cartesian
pair of their placed hosts. source is the producer host, target the consumer host; use R3,
including its local-route rule. Sort records by (producer,consumer,source,target).
id is SHA-256 of canonical_json of ALL other plan fields. Validate controls even for an empty
model. No mutation or I/O. Any placement/routing infeasibility propagates; never return a
partially compiled plan. Public intermediate results must compose without narrowing R1-R4.

R7. `networkx.deployment.controller.Controller(plan,journal,max_attempts=2)` accepts a compiled
plan and Journal; snapshot the plan and verify its id against all other fields. max_attempts
is an exact positive int. No constructor/status write. The journal's controller events use
ordinary R5 records; their payload schema is your choice. Pin plan id and max_attempts in
every persisted controller action; reopening with a different plan/config raises ValueError.
Implement claim(worker,now,ttl), ack(lease,success,now), snapshot(now):

- now is an exact nonnegative int; ttl exact positive int; worker nonempty str; success exact
  bool. Mutating actions with now older than the last persisted controller action raise
  ValueError. An already-recorded duplicate ack is allowed regardless of a later journal clock.
- claim returns None or exactly `{"service":id,"worker":str,"token":int,"expires":int}`.
  Grant one available service from the earliest batch with nonterminal services, lexical first,
  only after its prerequisites are done. Different workers can lease different services in
  one batch. A worker with an unexpired active lease receives that same lease without a write.
  token is the journal revision of this claim event; expires=now+ttl. Attempts count grants,
  not polls. No available work means None with no write.
- A lease is expired when now >= expires. Expired or failed work can be granted again while
  grants < max_attempts. Exhausted failure/expiry makes that service failed and transitive
  dependents blocked; those are terminal, allowing independent later batches to proceed.
  Successful work is done and never regranted. No background timers are required.
- ack validates the complete lease, ownership, token and unexpired deadline. Stale, forged,
  expired or conflicting decisions raise ConflictError without a write. First valid ack
  appends one event and returns True. Repeating the identical already-recorded lease/decision
  returns False, including after its expiration or a later retry; a different decision conflicts.
  A failed ack makes work pending or failed according to max_attempts.
- snapshot returns exactly `states,attempts,leases,revision`; states and attempts contain all
  services in lexical order; states are pending/leased/done/failed/blocked. leases contains
  active unexpired leases only. Returned values are defensive copies. Reconstruct from the
  entire journal after restart. Independent Controller objects must use CAS retry on races:
  four concurrent claimers cannot receive the same service/token. Do not spin indefinitely;
  bounded contention can surface ConflictError for the caller to retry.

R8. `networkx.deployment.cli.main(argv=None)` returns 0 on success, 2 on validation/conflict/
infeasibility/I/O error (print diagnostic to stderr); use argparse, no subprocess or network.
Support these commands and emit one strict JSON result to stdout:

- `plan INPUT OUTPUT`: INPUT JSON contains services,hosts,network (nodes list and [u,v,weight]
  edges), optional path_redundancy/max_parallel (defaults 1/4). Build Model, compile and atomically
  save the plan to OUTPUT with R5; emit the complete plan. Invalid input preserves OUTPUT.
- `status PLAN JOURNAL --now N [--max-attempts N]`: emit Controller.snapshot.
- `claim PLAN JOURNAL WORKER --now N --ttl N [--max-attempts N]`: emit a lease or null.
- `ack PLAN JOURNAL LEASE --now N (--success|--failure) [--max-attempts N]`: LEASE is a JSON
  lease file; emit true/false from ack. max-attempts defaults 2 for all controller commands.

Use the same implementations for CLI and Python callers. An import has no side effects.
The final delivery must preserve all upstream regression tests. No requirements files,
collection hooks, existing tests or packaging edits. Atomic files/locks are local test data;
this library must not install or deploy a service.

# Lower-bounded flow and temporal transshipment in the existing NetworkX checkout

Extend the pinned NetworkX repository. Preserve existing APIs, tests, decorators and packaging;
modify the existing flow/mincost.py for R2. All new modules below live in
networkx.algorithms.flow. No new runtime dependency, network, service or subprocess.
Use existing NetworkX flow algorithms, not capacity-value or complete-flow enumeration.
Choose cohesive ownership and reuse shared validation/serialization. Add root unittest tests
with test_rN_behavior methods for RN. Do not modify existing tests or flow/__init__.py.

R1. lowerbound.solve_bounded_flow(G,demand="demand",capacity="capacity",weight="weight",
lower="lower") returns (minimum_cost, flow_dict). G is DiGraph or MultiDiGraph, including
self-loops, reverse arcs and arbitrary hashable nodes/edge keys. All demands and weights
are exact ints (bool invalid), default 0. Each capacity is a required exact nonnegative int;
lower defaults 0 and must be an exact int between 0 and capacity. Attribute names must be
nonempty strings. Malformed input raises ValueError; infeasibility raises nx.NetworkXUnfeasible.
Flow satisfies inflow-outflow=demand at each node and each lower<=flow<=capacity, minimizing
sum(flow*weight), including finite negative-cost cycles. Use lower-bound reduction and existing
network_simplex. Return every original edge, including zero flows, in the usual NetworkX
dict shape (MultiDiGraph retains the key level). Empty graph returns (0,{}). Do not mutate G.

R2. Extend existing nx.min_cost_flow and nx.min_cost_flow_cost with keyword-only lower=None.
None retains existing behaviour including defaults; a nonempty attribute name delegates to
R1. Extend existing nx.max_flow_min_cost with keyword-only lower=None: None retains existing
behaviour; a name delegates to R3. Existing nx.cost_of_flow must additionally calculate
MultiDiGraph per-key cost, preserving DiGraph behaviour. Keep existing public entry points,
default parameters and backend decorators; do not add a competing re-export API.

R3. lowerbound_max.max_flow(G,s,t,capacity="capacity",weight="weight",lower="lower")
returns the keyed/unkeyed flow dict maximizing nonnegative net s-outflow, then minimizing
cost among such flows. s/t must be distinct existing nodes. Standard node demand attributes
must be exact zero ints. Validate using R1's numeric rules. No feasible nonnegative terminal
flow raises nx.NetworkXUnfeasible; malformed input raises ValueError. Incoming source arcs,
outgoing sink arcs, loops, parallel arcs, lower bounds and negative weights must work.
Use existing flow algorithms and polynomial reductions/binary search, never enumerate values
one by one. Preserve G, and support large integer capacities without capacity-proportional work.

R4. timeflow.expand(G,horizon,balances,storage=None) returns an nx.MultiDiGraph whose nodes
are (original_node,time) for every time 0..horizon inclusive. Here original node IDs and
MultiDiGraph keys are nonempty strings; DiGraph movement keys use None. horizon is an exact
nonnegative int. G must be directed; physical node attributes are ignored. Physical edges
use capacity/weight/lower as in R1 and exact positive transit (default 1), validated even
when they cannot arrive inside the horizon. For each possible departure add movement arc
with key ("move",u,v,key,departure), ending at departure+transit. balances is a dict mapping
(node,time) to exact signed demands, default 0, total 0; unknown nodes/times are invalid.
storage is None or a dict from known nodes to records with required capacity and optional
weight=0,lower=0, no other fields. It adds arcs (node,t)->(node,t+1), key ("hold",node,t),
with those bounds/costs at every tick. Set node demand and edge capacity/weight/lower;
set graph["horizon"]. No hold arcs for unspecified nodes. Invalid input raises ValueError.
No input mutation. Empty graph/horizon=0 is allowed.

R5. timeschedule.solve(G,horizon,balances,storage=None) composes R4/R1 and returns exactly
{"cost": int,"flow": [arc_records]}. Include only positive flows. Each record has exactly
kind ("move"/"hold"), source, target, key (original key or None for DiGraph/hold), depart,
arrive, amount. Sort by (depart,arrive,source,target,kind,key-or-empty-string).
Propagate validation and infeasibility; no partial result, I/O or mutation.

R6. timedecompose.paths(G,horizon,balances,schedule,storage=None) validates and decomposes
a schedule into shipments. First verify exact R5 shape, exact integer cost/amount/time,
positive record amounts, known arcs, no duplicate records, every capacity/lower bound,
all node balances and total cost. Missing arcs have zero flow. Invalid schedule raises
ValueError. Input record order may be arbitrary. Each output record is exactly
{amount,route,edges}; route is [[node,time],...],
edges is [{kind,key},...] for each route step. Decompose deterministically: choose supplies
in (time,node) order, follow positive residual arcs in R5 order, stop at the first node
with unfilled positive demand, take min(source remaining,destination remaining,path flow),
then repeat. A node with unfilled demand consumes before forwarding. Output records are
sorted by canonical JSON (R8 helper). Empty flow returns []. Account for every unit/arc,
including storage and parallel movements; do not narrow valid R4/R5 forms or mutate inputs.

R7. timeaudit.analyze(G,horizon,balances,scenarios,storage=None) computes baseline R5 cost
and each independent physical-edge outage. scenarios is a dict of nonempty string names
to lists of (u,v,key) triples (tuple or list); None is the only valid DiGraph key. Validate
all named edges against the original G; duplicate triples are harmless. Remove only those
physical edges in a private copy. Return exactly {baseline_cost,scenarios}; sorted scenario
names map to {feasible:bool,cost:int-or-None,delta:int-or-None}. Only scenario infeasibility
becomes False/None; baseline infeasibility propagates. Delta=cost-baseline and may be negative
when removing a lower-bound obligation. Other errors propagate. Never mutate G/scenarios.

R8. timeio.canonical_json(value) is strict canonical UTF-8 JSON (sorted keys, compact
separators, ensure_ascii=False, allow_nan=False); invalid JSON data raises ValueError.
timeio.save_json(path,value) uses that one encoder and a same-directory temp file + replace,
adds one trailing newline and cleans up on error, preserving old output on failure.
timecli.main(argv=None) supports `solve INPUT --output PATH` and `audit INPUT --output PATH`.
Input JSON has nodes,edges,horizon,balances and optional storage/scenarios, no other keys.
Reject nonfinite numbers anywhere in input, including unused scenarios.
nodes are unique nonempty strings. edges records require source,target,key,capacity and may
have weight/lower/transit, no other fields; endpoints must exist, keys nonempty strings,
(source,target,key) unique. balances is a list of exact {node,time,demand} records with unique
(node,time). Build a MultiDiGraph; storage passes to R4, scenarios to R7 (used only by audit).
solve writes exactly {schedule:R5,shipments:R6}; audit writes R7. Success returns 0, empty
stdout. Parse/validation/infeasibility/I/O errors print a stderr message, return 2, preserve
existing output. --help returns 0. Reuse timeio for all JSON output. No import-time I/O.

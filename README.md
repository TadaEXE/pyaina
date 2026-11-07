# Triaina

An implementation of **Triaina**, a declarative functional block-diagram
language for balanced ternary circuits, per the *Triaina — Language
Specification*. Gates define logic, blocks compose circuits, and connections
carry signals between ports. The compiler fixes circuit topology and widths;
the runtime evaluates affected computational nodes in discrete ticks.

## Usage

```bash
python3 -m triaina list  examples/counter.tri     # benches and tests in the import closure
python3 -m triaina test  examples/counter.tri     # run all tests
python3 -m triaina run   examples/counter.tri --bench count --ticks 101 --events ev.txt
python3 -m triaina repl  examples/counter.tri --bench count   # interactive terminal host
```

`ev.txt` holds tick-indexed replay records, one per line: `TICK KEY VALUE`
(e.g. `20 t +`). Output records print as `TICK LABEL: VALUE`, e.g.:

```
-1 Q: 0
25 Q: +
```

The first record is the tick `-1` initialization snapshot; tick `0` performs
the first evaluation.

## Language coverage

- Six-state trits (`- 0 + u e z`); adjacent symbols form vector literals
  (`+u0z`); longest-token lexing favors literals (`uez` is a literal, `use`
  is an identifier); `u`, `e`, `z` are reserved.
- Widths `:4`, `:?`, `:?n`, `:(n + 1)` as exact-rational numeric expressions;
  literal widths, port declarations, register initializers and named-connection
  annotations constrain inference; unresolved widths are compile errors; a
  shorter vector zero-extends on the left (added trits are driven `0`) when
  connected to a wider destination, longer ones require explicit selection.
- `a % b` concatenation (rightmost trit has index 0), `v[i]`, half-open
  `v[lo:hi]` (result index 0 = `v[lo]`), `v[:-1]` reversal.
- Gates `gate(N)` with truth tables over `- 0 + _` (wildcard), rows in source
  order, full coverage required, exhausted wildcard rows permitted, results
  may be `- 0 + z`. Positionwise lifting with zero-extension, `e` dominance,
  and independent per-position unknown enumeration (a unanimous result —
  including `z` — is emitted, else `u`). One stateless node and one
  computational tick per gate stage in a `>>` chain.
- Chains `>>` with flat member bundles; inline adapters `|$2, $0, +, @s.1[0:2]|`
  select/reorder/duplicate/omit/insert members and preserve driver identity;
  named connections `@name` (one declaration, arbitrary references, forward
  references allowed) with width annotations `@single:4`, `@bus:(1, 4, 2)`;
  `@name`, `@name.0`, `@name.0[2]`.
- Connections may have multiple output contributors, resolved trit-wise:
  `e`/conflict -> `e` (contention diagnostics name drivers and trit indices),
  else any `u` -> `u`, else agreeing actives, all-`z`/none -> `u`. Wiring
  statements sharing an endpoint join the participating connections. Resolution
  adds no tick.
- Blocks with ports, `with(Type name, ...)` instances (named references share
  the instance; anonymous occurrences are new instances), `instance.in.N` /
  `instance.out.N` / `instance.out`, no recursive instantiation. Block
  boundaries forward connections and preserve attached drivers. Classification:
  combinational / feedback / clocked (a block may be both clocked and
  feedback-containing); register outputs break combinational dependency paths.
- Tick scheduler: deduplicated current/next queues; tick `k` applies input
  events and clock transitions, evaluates each queued node once against the
  post-source snapshot, commits simultaneously, resolves and queues consumers
  of changed resolved values for `k+1`, then emits post-tick observations.
  Initialization is tick `-1` (inputs default `0`, clocks `-`, registers use
  their initializers, gate outputs `u`); every gate enters the first queue.
  Only changes to resolved five-state values schedule consumers.
- `clock(P, D)`: `H = P*D/100`, `L = P-H` positive integers, first rising edge
  at tick `L`, first falling at `P`. `register(init)` samples pre-commit data
  on rising `-`->`+` edges; its clock input must trace through transparent
  routing to a single clock generator.
- Benches declare their endpoints locally: `input('k')`, `input('k', +u0)`,
  `clock(50, 50)`, `output("Q")` / `output("Q", every_step)`. Keyboard records
  are the key plus exactly the source width's trits (`t+`); malformed records
  are diagnosed without changing the input. Multiple events on one tick: last
  in record order wins. `changes` emits the tick-`-1` snapshot plus changes;
  `every_step` also emits after every tick; records carry tick, name, value,
  declaration order.
- Tests: table tests (fresh instance per row, run to idle, combinational
  blocks only) and sequential tests (`until N`, `script`/`expect`, inclusive
  final tick, expectations exclude the tick-`-1` snapshot).
- Namespaces: `namespace logic { ... }`, `namespace outer::inner { ... }`,
  `logic::name`, `::logic::name`, reopenable, C++-style lookup through
  enclosing namespaces; `import "name.tri"` (acyclic, no re-export, duplicate
  qualified definitions and ambiguous references are diagnostics); benches and
  tests are discoverable throughout the import closure.
- Runtime API: `step()`, `advance(n)`, `inject(key, value)` (assigned to a
  later tick boundary), `outputs()`, `diagnostics()`, `subscribe(cb)`,
  `tick`, paced execution (`run_paced`, `Pacer` start/pause/stop; a requested
  interval is the minimum target between tick starts and every tick completes
  fully). The `repl` command is a minimal interactive terminal host.

## Tests

```bash
python3 -m unittest discover -s tests    # conformance suite
python3 -m triaina test examples/counter.tri examples/wiring.tri
```

## Layout

```
triaina/
  lexer.py      context-free lexer (numeric vs signal decided by the parser)
  parser.py     recursive-descent parser -> ast_nodes.py
  compiler.py   imports/namespaces, width solver, classification, clock-origin
                tracing, graph building
  runtime.py    connections, sources, nodes, the tick scheduler, emission, API
  host.py       bench runner, keyboard/tick-indexed input, test runner
  cli.py        list / run / test / repl
tests/          conformance suite
examples/       .tri files taken from the specification
```

# Triaina

An implementation of **Triaina**, a functional block-diagram language over
balanced ternary signals (`-`, `0`, `+`, plus `u` unknown, `e` fault, `z`
released), with a discrete **tick scheduler**: one tick is exactly one
scheduler step — apply sources, evaluate each queued node once against the
post-source/pre-commit snapshot, commit all outputs simultaneously, and
queue consumers of changed observations for the next tick. There is no
simulated physical time and no relaxation to a fixed point.

Implemented per the design draft "Triaina: Functional Block Diagram
Language" (revision 3, tick scheduler).

## Usage

```bash
python3 -m triaina list  examples/counter.tri     # list benches and tests
python3 -m triaina test  examples/counter.tri     # run all tests in the import closure
python3 -m triaina run   examples/counter.tri --bench count --ticks 101 --events ev.txt
```

`ev.txt` assigns input events to tick indices, one per line:

```
20 t +
45 t 0
```

Output records are printed as `TICK PHASE LABEL: VALUE`, e.g.:

```
0 init Q: 0
25 post Q: +
```

Run `python3 -m triaina run --help` for `--until-idle` / `--max-ticks`.

## Tests

```bash
python3 -m unittest discover -s tests    # 60 unit tests
python3 -m triaina test examples/counter.tri examples/wiring.tri  # doc examples
```

## Language coverage

- Six-state trits; literals concatenate (`++-0`, `+u0z`); longest-token
  lexing favors literals (`uez` is a literal, `use` is an identifier);
  `u`, `e`, `z` are reserved.
- Widths: `:4` fixed, `:?` inferred, `:?n` parametric, `:(n + m)` computed
  from exact rationals; anchored inference with backward anchoring at
  declared destinations; short values zero-extend on the left at
  fixed-width destinations (extension inserts driven `0`).
- Concatenation `%` (written order), indexing `v[i]`, slicing `v[lo:hi]`
  (index 0 = rightmost), reversal `v[:-1]`.
- Gates: truth tables with `?` wildcards and ordered exhaustion, positionwise
  lifting with zero-extension, `e` dominance, independent per-position
  unknown enumeration (unanimous result wins, else `u`); gates never emit `z`.
- Chains routed with `>>`; inline adapters `|$1, $0, +|`, `$0[1:3]`,
  `@ctrl.0`; named connections `@name` (forward references, fan-out, width
  annotations `@w:1`); single-driver rule (short circuits are compile
  errors).
- `merge`: the only meeting point of contributions and the only raw-`z`
  reader — fault, unknown, agreement, floating rules, contention reported
  as `e` with provenance.
- Blocks (the only component form), `with(...)` named instances, anonymous
  occurrences, positional calls of combinational components, `match`
  (first match, `?`, one `_` per pattern, mandatory final `_` arm, arms may
  drive `z`), `fold(g)`.
- Derived block classification (combinational / feedback / clocked),
  transitive; table tests only for combinational blocks; calls restricted
  to combinational components.
- Cyclic and cross-coupled circuits are first class; connections retain
  their last committed value; no settling, no oscillation detector.
- Registers `register(init)` with `data`/`clk`/`out`, rising-edge sampling
  of the pre-commit snapshot; clock generators `clock(`P`, `D`)` in integer
  ticks (rise at `L = P - P*D/100`, fall at `P`, repeat); clock eligibility
  by origin tracing through transparent routing only.
- Benches and endpoints: `input('k')`, `input('k', +u0z)`, `output("Q")`,
  `output("Q", every_step)`; emission modes `changes` (default) and
  `every_step`; records carry tick index, phase (`init`/`post`) and
  sequence.
- Tests: table tests (fresh network per row, run to idle) and sequential
  tests (`script`/`expect`/`until N`, tick-indexed).
- Files and `import "name.tri";`: acyclic, no re-export, name-collision
  diagnostics, entrypoint discovery over the whole import closure.
- Diagnostics with provenance: `e` carries tick-indexed origins (literal,
  external, contention with contributing ports); provenance propagates
  through routing/gates/registers without inventing faults and affects
  queueing but not signal equality or `changes` emission.

### Known limitations of this first implementation

- `using` (compile-time component parameters) is parsed but rejected with a
  diagnostic — the design draft marks it open for the first implementation.
- Forward references to *multi-member* named chains are not supported (use
  `@name.N` member access).
- `run until idle` is available as a host command with a mandatory tick
  bound (`--until-idle --max-ticks N`), as the draft requires.

## Layout

```
triaina/
  lexer.py      six-state lexer with numeric (backtick/:/[]) contexts
  parser.py     recursive-descent parser -> ast_nodes.py
  compiler.py   imports, name resolution, width solver, classification,
                clock-origin tracing, graph building
  runtime.py    sources, nets, nodes, the tick scheduler, emission
  host.py       bench runner, table/sequential test runner
  cli.py        list / run / test
tests/          unittest suite
examples/       .tri files taken from the design draft
```

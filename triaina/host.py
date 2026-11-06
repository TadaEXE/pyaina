"""Host: runs benches and the test runner (table + sequential tests)."""

from .compiler import Program, Compiler, Diag
from .values import Value, make_literal, E


def build_bench_rt(prog, bench_name):
    bench = None
    for b in prog.benches:
        if b.name == bench_name:
            bench = b
            break
    if bench is None:
        raise Diag('no bench named %r (available: %s)'
                   % (bench_name, ', '.join(b.name for b in prog.benches) or 'none'))
    comp = Compiler(prog)
    rt = comp.build_entry(bench.with_items, bench.body)
    return rt, comp


def value_event(trits, tick, key):
    prov = ()
    if E in trits:
        prov = ((tick, 'external', 'input %r' % key),)
    return Value(trits, prov)


def run_bench(prog, bench_name, ticks=None, until_idle=False, max_ticks=100000, events=None):
    """events: {tick: {key: trits-string}}. Returns the runtime after running."""
    rt, comp = build_bench_rt(prog, bench_name)
    events = events or {}

    def ev_for(tick):
        out = {}
        for key, trits in events.get(tick, {}).items():
            net = rt.input_by_key.get(key)
            if net is None:
                raise Diag('no input with key %r' % key)
            if len(trits) != net.width:
                raise Diag('input %r expects width %d, got %r' % (key, net.width, trits))
            out[net] = value_event(trits, tick, key)
        return out

    if until_idle:
        n = 0
        while n < max_ticks:
            if not rt.cur_queue and not any(t >= rt.tick for t in events):
                break
            rt.run_tick(ev_for(rt.tick))
            n += 1
    else:
        n = ticks if ticks is not None else 0
        for _ in range(n):
            rt.run_tick(ev_for(rt.tick))
    return rt


def run_tests(prog, only=None):
    """Returns list of (test_name, ok, message)."""
    results = []
    for tdef in prog.tests:
        if only and tdef.name != only:
            continue
        try:
            if tdef.target is not None:
                comp = Compiler(prog)
                for row in tdef.rows:
                    actual, expected = comp.build_table_row(tdef, row)
                    if actual != expected:
                        raise Diag('row mismatch: expected %s, got %s'
                                   % ([''.join(e) for e in expected],
                                      [''.join(a) for a in actual]), row.line)
                results.append((tdef.name, True, 'ok (%d rows)' % len(tdef.rows)))
            else:
                msg = _run_sequential(prog, tdef)
                results.append((tdef.name, True, msg))
        except Diag as d:
            results.append((tdef.name, False, str(d)))
    return results


def _run_sequential(prog, tdef):
    comp = Compiler(prog)
    script_inputs = {}
    events = {}
    for rec in tdef.script:
        tick = comp.const_int(rec.tick, rec.line, tdef.file, 'script tick')
        if rec.key in script_inputs:
            if script_inputs[rec.key][1] != len(rec.value):
                raise Diag('script input %r has conflicting widths' % rec.key, rec.line)
        else:
            script_inputs[rec.key] = (rec.key, len(rec.value))
        events.setdefault(tick, {})[rec.key] = rec.value
    until = comp.const_int(tdef.until, tdef.line, tdef.file, 'until')
    rt = comp.build_entry(tdef.with_items, tdef.body, script_inputs=script_inputs)

    def ev_for(tick):
        out = {}
        for key, trits in events.get(tick, {}).items():
            net = rt.input_by_key.get(key)
            if net is None:
                raise Diag('no script input %r' % key)
            out[net] = value_event(trits, tick, key)
        return out

    for t in range(until + 1):
        rt.run_tick(ev_for(t))
    emitted = [(r.tick, r.phase, r.label, r.trits) for r in rt.records]
    expected = []
    for rec in tdef.expect:
        tick = comp.const_int(rec.tick, rec.line, tdef.file, 'expect tick')
        phase = rec.phase
        if phase is None:
            if any(t == tick and p == 'init' and l == rec.label for t, p, l, _ in emitted):
                phase = 'init'
            else:
                phase = 'post'
        expected.append((tick, phase, rec.label, tuple(rec.value)))
    if emitted != expected:
        exp_s = ['%d %s %s: %s' % (t, p, l, ''.join(v)) for t, p, l, v in expected]
        got_s = ['%d %s %s: %s' % (t, p, l, ''.join(v)) for t, p, l, v in emitted]
        raise Diag('expectation mismatch\n  expected:\n    %s\n  actual:\n    %s'
                   % ('\n    '.join(exp_s) or '(none)', '\n    '.join(got_s) or '(none)'),
                   tdef.line)
    return 'ok (ticks 0..%d)' % until


def parse_events_file(path):
    events = {}
    with open(path) as f:
        for lineno, line in enumerate(f, 1):
            line = line.split('#', 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) != 3:
                raise Diag('bad event line %d: %r (want: TICK KEY VALUE)' % (lineno, line))
            tick = int(parts[0])
            key, value = parts[1], parts[2]
            events.setdefault(tick, {})[key] = value
    return events

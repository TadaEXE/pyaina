"""Host: runs benches and the test runner (table + sequential tests)."""

from .compiler import Program, Compiler, Diag
from .values import Value, ALL_STATES, E


def build_bench_rt(prog, bench_name):
    bench = prog.find_bench(bench_name)
    comp = Compiler(prog)
    rt = comp.build_entry(bench.with_items, bench.body, bench.file_id, bench.ns)
    return rt, comp


def run_bench(prog, bench_name, ticks=None, until_idle=False, max_ticks=100000,
              events=None):
    """events: iterable of (tick, key, trits-string) in source-record order;
    when several target one input on a tick, the last wins."""
    rt, comp = build_bench_rt(prog, bench_name)
    for tick, key, trits in (events or []):
        rt.inject(key, tuple(trits), tick=tick)
    if until_idle:
        n = 0
        while n < max_ticks and (rt.cur_queue or rt.has_pending()):
            rt.run_tick()
            n += 1
    else:
        for _ in range(ticks if ticks is not None else 0):
            rt.run_tick()
    return rt


def parse_events_file(path):
    """Tick-indexed replay records: lines of `TICK KEY VALUE` (e.g. `20 t +`)."""
    events = []
    with open(path) as f:
        for lineno, line in enumerate(f, 1):
            line = line.split('#', 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) != 3:
                raise Diag('bad event line %d: %r (want: TICK KEY VALUE)' % (lineno, line))
            events.append((int(parts[0]), parts[1], parts[2]))
    return events


def parse_keyboard_record(line, rt):
    """A keyboard record is the key followed by exactly the source width's
    trit symbols, e.g. `t+`. Malformed records produce a diagnostic."""
    line = line.rstrip('\n')
    for key in sorted(rt.input_by_key, key=len, reverse=True):
        if line.startswith(key):
            val = line[len(key):]
            net = rt.input_by_key[key]
            if any(c not in ALL_STATES for c in val):
                raise Diag('malformed record %r: %r is not a trit vector' % (line, val))
            if len(val) != net.conn.width:
                raise Diag('malformed record %r: input %r expects width %d'
                           % (line, key, net.conn.width))
            return key, tuple(val)
    raise Diag('malformed record %r: unknown input key' % line)


def run_tests(prog, only=None):
    """Returns list of (test_name, ok, message)."""
    results = []
    for _fi, tdef in prog.tests:
        if only and tdef.name != only and '::'.join(tdef.qname) != only:
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
    events = []
    for rec in tdef.script:
        tick = comp.const_int(rec.tick, rec.line, tdef.file, 'script tick')
        if any(c not in ALL_STATES for c in rec.value):
            raise Diag('bad script value %r' % rec.value, rec.line, tdef.file)
        if rec.key in script_inputs:
            if script_inputs[rec.key] != len(rec.value):
                raise Diag('script input %r has conflicting widths' % rec.key, rec.line)
        else:
            script_inputs[rec.key] = len(rec.value)
        events.append((tick, rec.key, rec.value))
    until = comp.const_int(tdef.until, tdef.line, tdef.file, 'until')
    rt = comp.build_entry(tdef.with_items, tdef.body, tdef.file_id, tdef.ns,
                          script_inputs=script_inputs)
    for tick, key, trits in events:
        rt.inject(key, tuple(trits), tick=tick)
    for _ in range(until + 1):  # inclusive final tick
        rt.run_tick()
    emitted = [(r.tick, r.label, r.trits) for r in rt.records if r.tick >= 0]
    expected = []
    for rec in tdef.expect:
        tick = comp.const_int(rec.tick, rec.line, tdef.file, 'expect tick')
        if any(c not in ALL_STATES for c in rec.value):
            raise Diag('bad expected value %r' % rec.value, rec.line, tdef.file)
        expected.append((tick, rec.label, tuple(rec.value)))
    if emitted != expected:
        exp_s = ['%d %s: %s' % (t, l, ''.join(v)) for t, l, v in expected]
        got_s = ['%d %s: %s' % (t, l, ''.join(v)) for t, l, v in emitted]
        raise Diag('expectation mismatch\n  expected:\n    %s\n  actual:\n    %s'
                   % ('\n    '.join(exp_s) or '(none)', '\n    '.join(got_s) or '(none)'),
                   tdef.line, tdef.file)
    return 'ok (ticks 0..%d)' % until

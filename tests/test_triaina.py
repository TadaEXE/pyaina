"""Conformance suite for the Triaina language specification."""

import os
import shutil
import tempfile
import unittest

from triaina import host
from triaina.compiler import Program, Compiler, Diag, build_gate_table
from triaina.lexer import lex, LexError
from triaina.parser import parse_text, ParseError
from triaina.runtime import apply_gate_table, resolve_conn, Runtime, NetSrc, ConstSrc
from triaina.values import Value, make_literal, pad_left, unknown


# ---- helpers ----

def kinds(text):
    return [(t.kind, t.value) for t in lex(text) if t.kind != 'EOF']


class Src:
    """Write sources into a temp dir and load a Program."""

    def __init__(self, main, extra=None):
        self.dir = tempfile.mkdtemp(prefix='tri-test-')
        self.main = os.path.join(self.dir, 'main.tri')
        with open(self.main, 'w') as f:
            f.write(main)
        for name, src in (extra or {}).items():
            with open(os.path.join(self.dir, name), 'w') as f:
                f.write(src)

    def prog(self):
        return Program(self.main)

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)


def run(src, bench='b', ticks=0, events=None, extra=None):
    s = Src(src, extra)
    try:
        prog = s.prog()
        return host.run_bench(prog, bench, ticks=ticks, events=events or [])
    finally:
        s.cleanup()


def tests_of(src, only=None, extra=None):
    s = Src(src, extra)
    try:
        return host.run_tests(s.prog(), only=only)
    finally:
        s.cleanup()


def compile_error(src, extra=None):
    s = Src(src, extra)
    try:
        host.run_tests(s.prog())
        return None
    except Diag as d:
        return str(d)
    finally:
        s.cleanup()


def records_of(rt):
    return [(r.tick, r.label, ''.join(r.trits)) for r in rt.records]


# ---- lexer / values ----

class TestLexer(unittest.TestCase):
    def test_longest_token_favors_literals(self):
        self.assertIn(('LITERAL', 'uez'), kinds('uez'))
        self.assertIn(('IDENT', 'use'), kinds('use'))
        self.assertIn(('LITERAL', '+u0z'), kinds('+u0z'))
        self.assertIn(('LITERAL', 'u0'), kinds('u0'))
        self.assertIn(('IDENT', 'u0x'), kinds('u0x'))

    def test_numeric_vs_signal_tokens(self):
        self.assertIn(('NUM', 25), kinds('25'))
        self.assertIn(('LITERAL', '0'), kinds('0'))       # signal context: trit
        self.assertIn(('LITERAL', '0'), kinds('0:'))      # script tick is read as 0 by the parser
        self.assertIn(('NUM', 100), kinds('100'))
        self.assertIn(('LITERAL', '0+'), kinds('0+'))

    def test_symbols(self):
        k = kinds('@bus:(1, 4, 2); $2 >> x; a::b; ::c::d;')
        self.assertIn(('AT', 'bus'), k)
        self.assertIn(('USD', 2), k)
        self.assertIn(('DCOLON', '::'), k)
        self.assertIn(('RARROW', '>>'), k)

    def test_concat_and_pad(self):
        a = Value(('+', '0'))
        b = Value(('-',))
        from triaina.values import concat_values
        self.assertEqual(concat_values([a, b]).trits, ('+', '0', '-'))
        self.assertEqual(pad_left(Value(('+',)), 3).trits, ('0', '0', '+'))
        self.assertEqual(Value(('z',)).observed(raw=False), (('u',)))


# ---- gates ----

HS_ROWS = [([('lit', '+'), ('lit', '+')], '-'), ([('lit', '-'), ('lit', '-')], '+'),
           ([('lit', '+'), ('lit', '-')], '0'), ([('lit', '-'), ('lit', '+')], '0'),
           ([('lit', '+'), ('lit', '0')], '+'), ([('lit', '-'), ('lit', '0')], '-'),
           ([('lit', '0'), ('lit', '+')], '+'), ([('lit', '0'), ('lit', '-')], '-'),
           ([('any',), ('any',)], '0')]


class TestGates(unittest.TestCase):
    def test_truth_table(self):
        table, warns = build_gate_table(HS_ROWS, 2, 'hs')
        self.assertEqual(warns, [])
        self.assertEqual(table[('+', '+')], '-')
        self.assertEqual(table[('0', '0')], '0')

    def test_incomplete_table_is_error(self):
        with self.assertRaises(Diag):
            build_gate_table(HS_ROWS[:3], 2, 'hs')

    def test_duplicate_explicit_row_is_error(self):
        rows = HS_ROWS + [([('lit', '+'), ('lit', '+')], '0')]
        with self.assertRaises(Diag):
            build_gate_table(rows, 2, 'hs')

    def test_exhausted_wildcard_is_permitted(self):
        rows = HS_ROWS + [([('any',), ('any',)], '+')]  # all combos already assigned
        table, warns = build_gate_table(rows, 2, 'hs')
        self.assertEqual(len(warns), 1)
        self.assertEqual(table[('+', '+')], '-')

    def test_unknown_lifting_unanimous_and_non(self):
        table, _ = build_gate_table(HS_ROWS, 2, 'hs')
        self.assertEqual(apply_gate_table(table, [Value(('u',)), Value(('+',))], 1).trits,
                         ('u',))  # hs(u,+): {0,+,-} non-unanimous
        latch_rows = [([('lit', '+'), ('any',)], '+'), ([('lit', '-'), ('any',)], '-'),
                      ([('lit', '0'), ('lit', '-')], '-'), ([('lit', '0'), ('lit', '0')], '0'),
                      ([('lit', '0'), ('lit', '+')], '+')]
        lt, _ = build_gate_table(latch_rows, 2, 'latch_logic')
        self.assertEqual(apply_gate_table(lt, [Value(('+',)), Value(('u',))], 1).trits,
                         ('+',))  # (+,_) unanimous

    def test_unknown_lifting_unanimous_z(self):
        rows = [([('lit', c)], 'z') for c in '-0+']
        table, _ = build_gate_table(rows, 1, 'rel')
        self.assertEqual(apply_gate_table(table, [Value(('u',))], 1).trits, ('z',))
        # pull_low: u -> {z, z, 0} -> u
        pl = [([('lit', '+')], '0'), ([('any',)], 'z')]
        table2, _ = build_gate_table(pl, 1, 'pull_low')
        self.assertEqual(apply_gate_table(table2, [Value(('u',))], 1).trits, ('u',))

    def test_fault_input_yields_fault_with_origin(self):
        table, _ = build_gate_table(HS_ROWS, 2, 'hs')
        v = Value(('e',), ((3, 'literal', 'literal e'),))
        out = apply_gate_table(table, [v, Value(('+',))], 1)
        self.assertEqual(out.trits, ('e',))
        self.assertIn((3, 'literal', 'literal e'), out.prov)

    def test_positionwise_with_zero_extension(self):
        rows = [([('lit', c)], {'-': '0', '0': '+', '+': '-'}[c]) for c in '-0+']
        table, _ = build_gate_table(rows, 1, 'rot')
        out = apply_gate_table(table, [Value(('+', '0', '-'))], 3)
        self.assertEqual(out.trits, ('-', '+', '0'))
        out = apply_gate_table(table, [Value(('+',))], 3)  # zero-extended to width 3: 000+
        self.assertEqual(out.trits, ('+', '+', '-'))

    def test_gate_z_result_reaches_connection_as_release(self):
        src = """
        gate(1) pull_low { (+) >> 0; (_) >> z; }
        bench("b") {
          input('a') a;
          output("O") o;
          a >> pull_low >> o;
        }
        """
        rt = run(src, ticks=2, events=[(0, 'a', '0')])
        self.assertEqual(records_of(rt), [(-1, 'O', 'u'), ])  # released -> u
        rt = run(src, ticks=2, events=[(0, 'a', '+')])
        self.assertEqual(records_of(rt), [(-1, 'O', 'u'), (0, 'O', '0')])


# ---- width inference ----

class TestWidths(unittest.TestCase):
    def test_zero_extend_at_wider_destination(self):
        src = """
        block(in v:1, out w:4) f { v >> w; }
        test("t") f { (|+|) >> (|000+|); (|u|) >> (|000u|); }
        """
        res = tests_of(src)
        self.assertEqual(res[0][1], True, res[0][2])

    def test_too_wide_is_error(self):
        src = """
        block(in v:4, out w:1) f { v >> w; }
        test("t") f { (|+0-u|) >> (|0|); }
        """
        res = tests_of(src)
        self.assertFalse(res[0][1])
        self.assertIn('exceeds destination width', res[0][2])

    def test_param_widths_and_computed_width(self):
        src = """
        block(in v:?n, out w:(2 * n)) d { v % v >> w; }
        test("t") d { (|++|) >> (|++++|); (|-0|) >> (|-0-0|); }
        """
        res = tests_of(src)
        self.assertEqual(res[0][1], True, res[0][2])

    def test_unresolved_width_is_error(self):
        src = """
        block(in v:?, out w:?) f { v >> w; }
        bench("b") { output("O") o; f >> o; }
        """
        with self.assertRaises(Diag) as cm:
            run(src, ticks=0)
        self.assertIn('unresolved width', str(cm.exception))

    def test_conflicting_annotation_is_error(self):
        src = """
        block(in v:3, out w:3) f { v >> @s:2 >> w; }
        bench("b") with(f inst) { input('a') a; output("O") o; +0- >> inst.in.0; inst.out >> o; }
        """
        with self.assertRaises(Diag) as cm:
            run(src, ticks=0)
        self.assertIn('conflicting widths', str(cm.exception))

    def test_slices_index_and_reverse(self):
        src = """
        block(in v:4, out lo:2, out hi:2, out one:1, out rev:4) s {
          v[0:2] >> lo; v[2:4] >> hi; v[3] >> one; v[:-1] >> rev;
        }
        test("t") s { (|+0-u|) >> (|-u, +0, +, u-0+|); }
        """
        res = tests_of(src)
        self.assertEqual(res[0][1], True, res[0][2])

    def test_invalid_slice_is_error(self):
        src = """
        block(in v:2, out o:1) s { v[3] >> o; }
        bench("b") with(s inst) { input('a') a; output("O") o; ++ >> inst.in.0; inst.out >> o; }
        """
        with self.assertRaises(Diag) as cm:
            run(src, ticks=0)
        self.assertIn('invalid slice', str(cm.exception))


# ---- chains, adapters, named connections ----

class TestChains(unittest.TestCase):
    def test_adapter_reorder_dup_omit_insert(self):
        src = """
        gate(2) hs {
          (+, +) >> -; (-, -) >> +; (+, -) >> 0; (-, +) >> 0;
          (+, 0) >> +; (-, 0) >> -; (0, +) >> +; (0, -) >> -; (_, _) >> 0;
        }
        block(in a:1, in b:1, in c:1, out x:1, out y:1) d {
          |a, b, c| >> @s:(1, 1, 1);
          @s >> |$2, $0| >> hs >> x;
          @s >> |@s.1, $0| >> hs >> y;
        }
        test("t") d {
          (|+, 0, -|) >> (|0, +|);   // x = hs(c, a) = hs(-, +) = 0; y = hs(b, a) = hs(0, +) = +
        }
        """
        res = tests_of(src)
        self.assertEqual(res[0][1], True, res[0][2])

    def test_adapter_usd_out_of_range(self):
        src = """
        block(in a:1, out x:1) d { |a| >> |$1| >> x; }
        bench("b") with(d inst) { input('a') a; output("O") o; a >> inst.in.0; inst.out >> o; }
        """
        with self.assertRaises(Diag) as cm:
            run(src, ticks=0)
        self.assertIn('$1 out of range', str(cm.exception))

    def test_named_member_trit_index_and_forward_ref(self):
        src = """
        gate(1) rot { (-) >> 0; (0) >> +; (+) >> -; }
        gate(2) hs {
          (+, +) >> -; (-, -) >> +; (+, -) >> 0; (-, +) >> 0;
          (+, 0) >> +; (-, 0) >> -; (0, +) >> +; (0, -) >> -; (_, _) >> 0;
        }
        block(in a:1, in v:4, out x:1, out y:1, out t:1) d {
          |@fwd.0, a| >> hs >> y;
          a >> rot >> @fwd >> x;
          v >> @bus:4;
          @bus.0[2] >> t;
        }
        test("t") d {
          (|+, +0-u|) >> (|- , 0, 0|);
        }
        """
        res = tests_of(src)
        self.assertEqual(res[0][1], True, res[0][2])

    def test_named_double_declaration_and_undeclared(self):
        src = """
        gate(1) rot { (-) >> 0; (0) >> +; (+) >> -; }
        block(in a:1, out x:1) d {
          a >> rot >> @s >> x;
          a >> @s >> x;
        }
        bench("b") with(d inst) { input('a') a; output("O") o; a >> inst.in.0; inst.out >> o; }
        """
        with self.assertRaises(Diag) as cm:
            run(src, ticks=0)
        self.assertIn('declared twice', str(cm.exception))
        src2 = """
        block(in a:1, out x:1) d { @nope >> x; }
        bench("b") with(d inst) { input('a') a; output("O") o; a >> inst.in.0; inst.out >> o; }
        """
        with self.assertRaises(Diag) as cm2:
            run(src2, ticks=0)
        self.assertIn('never declared', str(cm2.exception))

    def test_fanout_and_arity(self):
        src = """
        block(in a:1, out x:1, out y:1) f { a >> x; a >> y; }
        test("t") f { (|+|) >> (|+, +|); (|-|) >> (|-, -|); }
        """
        res = tests_of(src)
        self.assertEqual(res[0][1], True, res[0][2])
        src2 = """
        gate(2) hs {
          (+, +) >> -; (-, -) >> +; (+, -) >> 0; (-, +) >> 0;
          (+, 0) >> +; (-, 0) >> -; (0, +) >> +; (0, -) >> -; (_, _) >> 0;
        }
        block(in a:1, out x:1) d { |a| >> hs >> x; }
        bench("b") with(d inst) { input('a') a; output("O") o; a >> inst.in.0; inst.out >> o; }
        """
        with self.assertRaises(Diag) as cm:
            run(src2, ticks=0)
        self.assertIn('expects 2 input(s)', str(cm.exception))


# ---- connection resolution ----

class TestResolution(unittest.TestCase):
    def test_spec_driver_example(self):
        src = """
        bench("b") {
          output("C") c;
          +0-z >> c;
          z0+- >> c;
        }
        """
        rt = run(src, ticks=1)
        self.assertEqual(records_of(rt), [(-1, 'C', '+0e-')])

    def test_resolution_rules(self):
        rt = Runtime()
        from triaina.compiler import WSolver
        w = WSolver().new()
        w.expr = ('const', __import__('fractions').Fraction(1))
        rt.conns.clear()
        conn = rt.conns[0] if rt.conns else None
        # build a connection manually
        from triaina.runtime import Connection
        c = Connection(rt, 'test', w)
        c.width = 1
        c.contributors = [('src', ConstSrc(w, make_literal('z')), 'A'),
                          ('src', ConstSrc(w, make_literal('z')), 'B')]
        self.assertEqual(resolve_conn(rt, c).trits, ('z',))   # all released
        c.contributors = [('src', ConstSrc(w, make_literal('+')), 'A'),
                          ('src', ConstSrc(w, make_literal('z')), 'B')]
        self.assertEqual(resolve_conn(rt, c).trits, ('+',))   # agreeing active wins over z
        c.contributors = [('src', ConstSrc(w, make_literal('+')), 'A'),
                          ('src', ConstSrc(w, make_literal('-')), 'B')]
        v = resolve_conn(rt, c)
        self.assertEqual(v.trits, ('e',))                      # contention
        self.assertTrue(any(p[1] == 'contention' for p in v.prov))
        c.contributors = [('src', ConstSrc(w, make_literal('u')), 'A'),
                          ('src', ConstSrc(w, make_literal('+')), 'B')]
        self.assertEqual(resolve_conn(rt, c).trits, ('u',))    # u dominates agreement
        c.contributors = [('src', ConstSrc(w, make_literal('e')), 'A'),
                          ('src', ConstSrc(w, make_literal('+')), 'B')]
        self.assertEqual(resolve_conn(rt, c).trits, ('e',))    # fault dominates

    def test_contention_diagnostics_name_drivers_and_index(self):
        src = """
        bench("b") {
          input('a') a;
          input('b') b;
          output("C") c;
          a >> c;
          b >> c;
        }
        """
        rt = run(src, ticks=1, events=[(0, 'a', '+'), (0, 'b', '-')])
        self.assertEqual(records_of(rt), [(-1, 'C', '0'), (0, 'C', 'e')])
        ds = rt.diagnostics()
        self.assertTrue(any('contention' in d and '0' in d for d in ds), ds)

    def test_shared_pull_low_semantics(self):
        src = """
        gate(1) pull_low { (+) >> 0; (_) >> z; }
        block(in enable_a:1, in enable_b:1, out bus:1) sp {
          enable_a >> pull_low >> bus;
          enable_b >> pull_low >> bus;
        }
        bench("b") with(sp inst) {
          input('a') a;
          input('b') b;
          output("B") o;
          a >> inst.in.0;
          b >> inst.in.1;
          inst.out >> o;
        }
        """
        rt = run(src, ticks=4, events=[(0, 'a', '+'), (1, 'a', '0'), (2, 'b', '+'), (3, 'b', '0')])
        self.assertEqual(records_of(rt),
                         [(-1, 'B', 'u'), (0, 'B', '0'), (1, 'B', 'u'),
                          (2, 'B', '0'), (3, 'B', 'u')])


# ---- blocks, classification, feedback ----

LATCH = """
gate(2) latch_logic {
  (+, _) >> +; (-, _) >> -; (0, -) >> -; (0, 0) >> 0; (0, +) >> +;
}
block(in s:1, out q:1) latch { |s, q| >> latch_logic >> q; }
"""


class TestBlocks(unittest.TestCase):
    def test_feedback_latch(self):
        src = LATCH + """
        test("latch") until 12 with(latch l) {
          output("Q") q_out;
          script { 0: s +; 5: s 0; 8: s -; }
          s >> l.in.0;
          l.out >> q_out;
          expect { 0: Q +; 8: Q -; }
        }
        """
        res = tests_of(src)
        self.assertEqual(res[0][1], True, res[0][2])

    def test_classification(self):
        src = LATCH + """
        gate(1) rot { (-) >> 0; (0) >> +; (+) >> -; }
        block(in a:1, out x:1) comb { a >> rot >> x; }
        block(in t:1, in clk:1, out q:1) cnt with(register(0) st) {
          |t, st.out| >> latch_logic >> st.in.0;
          clk >> st.in.1;
          st.out >> q;
        }
        """
        s = Src(src)
        try:
            prog = s.prog()
            comp = Compiler(prog)
            comb = prog.lookup_def(['comb'], False, (), 0)
            latch = prog.lookup_def(['latch'], False, (), 0)
            cnt = prog.lookup_def(['cnt'], False, (), 0)
            self.assertEqual(comp.classify(comb), 'combinational')
            self.assertEqual(comp.classify(latch), 'feedback')
            self.assertEqual(comp.classify(cnt), 'clocked')
        finally:
            s.cleanup()

    def test_table_test_rejects_feedback_block(self):
        src = LATCH + """
        test("bad") latch { (|+|) >> (|+|); }
        """
        res = tests_of(src)
        self.assertFalse(res[0][1])
        self.assertIn('combinational', res[0][2])

    def test_recursion_rejected(self):
        src = """
        block(in a:1, out x:1) r { a >> r2 >> x; }
        block(in a:1, out x:1) r2 { a >> r >> x; }
        bench("b") with(r inst) { input('a') a; output("O") o; a >> inst.in.0; inst.out >> o; }
        """
        with self.assertRaises(Diag) as cm:
            run(src, ticks=0)
        self.assertIn('recursion', str(cm.exception))

    def test_named_instance_shares_state(self):
        src = """
        gate(1) rot { (-) >> 0; (0) >> +; (+) >> -; }
        block(in a:1, out x:1, out y:1) two with(rot r) {
          a >> r >> x;
          r.out >> y;
        }
        test("t") two { (|+|) >> (|-, -|); }
        """
        res = tests_of(src)
        self.assertEqual(res[0][1], True, res[0][2])


# ---- scheduler ----

class TestScheduler(unittest.TestCase):
    def test_one_tick_per_gate_stage(self):
        src = """
        gate(1) rot { (-) >> 0; (0) >> +; (+) >> -; }
        bench("b") { input('a') a; output("O") o; a >> rot >> rot >> o; }
        """
        rt = run(src, ticks=3, events=[(0, 'a', '+')])
        # rot(+) = - at tick 0, rot(-) = 0 at tick 1
        self.assertEqual(records_of(rt), [(-1, 'O', 'u'), (1, 'O', '0')])

    def test_tick_minus_one_init_and_first_eval_at_zero(self):
        src = """
        gate(1) rot { (-) >> 0; (0) >> +; (+) >> -; }
        bench("b") { input('a') a; output("O") o; a >> rot >> o; }
        """
        rt = run(src, ticks=2, events=[(0, 'a', '+')])
        self.assertEqual(rt.tick, 1)
        self.assertEqual(records_of(rt), [(-1, 'O', 'u'), (0, 'O', '-')])

    def test_idle_queue_stays_idle(self):
        src = """
        gate(1) rot { (-) >> 0; (0) >> +; (+) >> -; }
        bench("b") { input('a') a; output("O") o; a >> rot >> o; }
        """
        rt = run(src, ticks=0)
        self.assertTrue(rt.cur_queue)  # first evaluation queue pending
        rt.run_until_idle(10)
        self.assertFalse(rt.cur_queue)

    def test_deterministic_replay(self):
        src = """
        gate(2) hs {
          (+, +) >> -; (-, -) >> +; (+, -) >> 0; (-, +) >> 0;
          (+, 0) >> +; (-, 0) >> -; (0, +) >> +; (0, -) >> -; (_, _) >> 0;
        }
        bench("b") { input('a') a; input('b') b; output("O") o; |a, b| >> hs >> o; }
        """
        ev = [(0, 'a', '+'), (1, 'b', '-'), (2, 'a', '0')]
        r1 = records_of(run(src, ticks=5, events=ev))
        r2 = records_of(run(src, ticks=5, events=ev))
        self.assertEqual(r1, r2)
        self.assertEqual(r1, [(-1, 'O', 'u'), (0, 'O', '+'), (1, 'O', '0'), (2, 'O', '-')])

    def test_register_samples_pre_commit_data(self):
        src = """
        import "lib.tri";
        test("precommit") until 100 with(counter ff) {
          clock(50, 50) sys_clk;
          output("Q") q_out;
          script { 25: t +; }
          t >> ff.in.0;
          sys_clk >> ff.in.1;
          ff.out >> q_out;
          expect { 75: Q +; }
        }
        """
        extra = {'lib.tri': open(os.path.join(os.path.dirname(__file__), '..',
                                               'examples', 'lib.tri')).read()}
        res = tests_of(src, extra=extra)
        self.assertEqual(res[0][1], True, res[0][2])


# ---- clocks and registers ----

class TestClocksRegisters(unittest.TestCase):
    def test_clock_edges(self):
        src = """
        bench("b") { clock(10, 30) c; output("C") o; c >> o; }
        """
        rt = run(src, ticks=21)
        self.assertEqual(records_of(rt),
                         [(-1, 'C', '-'), (7, 'C', '+'), (10, 'C', '-'),
                          (17, 'C', '+'), (20, 'C', '-')])

    def test_clock_duty_validation(self):
        for duty in ('0', '100', '25'):
            src = 'bench("b") { clock(10, %s) c; output("C") o; c >> o; }' % duty
            with self.assertRaises(Diag):
                run(src, ticks=0)

    def test_register_rising_edge_and_hold(self):
        src = """
        bench("b") {
          input('d') d;
          clock(10, 50) c;
          output("Q") q;
          with(register(0) r) { d >> r.in.0; c >> r.in.1; r.out >> q; }
        }
        """
        # hmm: with() must be on the bench; rewrite below
        src = """
        bench("b") with(register(0) r) {
          input('d') d;
          clock(10, 50) c;
          output("Q") q;
          d >> r.in.0;
          c >> r.in.1;
          r.out >> q;
        }
        """
        rt = run(src, ticks=16, events=[(0, 'd', '+'), (7, 'd', '-')])
        # rising edges at 5 and 15; d=+ sampled at 5, d=- sampled at 15
        self.assertEqual(records_of(rt),
                         [(-1, 'Q', '0'), (5, 'Q', '+'), (15, 'Q', '-')])

    def test_register_initializer_z_observed_as_u(self):
        src = """
        bench("b") with(register(z) r) {
          input('d') d;
          clock(4, 50) c;
          output("Q") q;
          d >> r.in.0;
          c >> r.in.1;
          r.out >> q;
        }
        """
        rt = run(src, ticks=2, events=[(0, 'd', '+')])
        self.assertEqual(records_of(rt), [(-1, 'Q', 'u')])  # rise at tick 2, not yet
        rt2 = run(src, ticks=3, events=[(0, 'd', '+')])
        self.assertEqual(records_of(rt2), [(-1, 'Q', 'u'), (2, 'Q', '+')])

    def test_clock_through_gate_rejected(self):
        src = """
        gate(1) rot { (-) >> 0; (0) >> +; (+) >> -; }
        bench("b") with(register(0) r) {
          input('d') d;
          clock(10, 50) c;
          output("Q") q;
          d >> r.in.0;
          c >> rot >> r.in.1;
          r.out >> q;
        }
        """
        with self.assertRaises(Diag) as cm:
            run(src, ticks=0)
        self.assertIn('clock', str(cm.exception))

    def test_clock_with_multiple_contributors_ineligible(self):
        src = """
        bench("b") with(register(0) r) {
          input('d') d;
          clock(10, 50) c1;
          clock(10, 50) c2;
          output("Q") q;
          d >> r.in.0;
          c1 >> r.in.1;
          c2 >> r.in.1;
          r.out >> q;
        }
        """
        with self.assertRaises(Diag) as cm:
            run(src, ticks=0)
        self.assertIn('clock', str(cm.exception))


# ---- benches, endpoints, I/O ----

class TestBenches(unittest.TestCase):
    def test_bench_local_endpoints_and_init_values(self):
        src = """
        bench("b") {
          input('a') a;
          input('w', +u0) wide;
          output("A") oa;
          output("W") ow;
          a >> oa;
          wide >> ow;
        }
        """
        rt = run(src, ticks=0)
        self.assertEqual(records_of(rt), [(-1, 'A', '0'), (-1, 'W', '+u0')])
        self.assertEqual(rt.outputs(), {'A': '0', 'W': '+u0'})

    def test_input_z_and_keyboard_records(self):
        src = """
        bench("b") { input('t') t; output("O") o; t >> o; }
        """
        rt = run(src, ticks=2, events=[(0, 't', 'z')])
        self.assertEqual(records_of(rt), [(-1, 'O', '0'), (0, 'O', 'u')])
        key, val = host.parse_keyboard_record('t+', rt)
        self.assertEqual((key, val), ('t', ('+',)))
        with self.assertRaises(Diag):
            host.parse_keyboard_record('t++', rt)   # wrong width
        with self.assertRaises(Diag):
            host.parse_keyboard_record('q+', rt)    # unknown key

    def test_last_event_on_tick_wins(self):
        src = """
        bench("b") { input('t') t; output("O") o; t >> o; }
        """
        rt = run(src, ticks=1, events=[(0, 't', '+'), (0, 't', '-')])
        self.assertEqual(records_of(rt), [(-1, 'O', '0'), (0, 'O', '-')])

    def test_inject_during_tick_goes_to_later_boundary(self):
        src = """
        bench("b") { input('t') t; output("O") o; t >> o; }
        """
        rt = run(src, ticks=0)
        tick = rt.inject('t', ('+',))   # during tick -1 -> assigned to tick 0
        self.assertEqual(tick, 0)
        rt.run_tick()
        self.assertEqual(records_of(rt), [(-1, 'O', '0'), (0, 'O', '+')])
        with self.assertRaises(ValueError):
            rt.inject('t', ('+', '+'))   # wrong width

    def test_changes_vs_every_step(self):
        src = """
        gate(1) rot { (-) >> 0; (0) >> +; (+) >> -; }
        bench("b") {
          input('a') a;
          output("C") c;
          output("E", every_step) ev;
          a >> rot >> c;
          a >> rot >> ev;
        }
        """
        rt = run(src, ticks=3, events=[(1, 'a', '+')])
        recs = records_of(rt)
        self.assertEqual([r for r in recs if r[1] == 'C'], [(-1, 'C', 'u'), (0, 'C', '+'), (1, 'C', '-')])
        self.assertEqual([r for r in recs if r[1] == 'E'],
                         [(-1, 'E', 'u'), (0, 'E', '+'), (1, 'E', '-'), (2, 'E', '-')])
        # declaration order: C records precede E records at the same tick
        self.assertLess(recs.index((1, 'C', '-')), recs.index((1, 'E', '-')))
        self.assertLess(recs.index((0, 'C', '+')), recs.index((0, 'E', '+')))

    def test_output_width_inferred(self):
        src = """
        block(in a:1, in b:1, out r:2) j { a % b >> r; }
        bench("b") { input('a') a; input('b') b; output("R") o; |a, b| >> j >> o; }
        """
        rt = run(src, ticks=1, events=[(0, 'a', '+'), (0, 'b', '-')])
        self.assertEqual(records_of(rt), [(-1, 'R', '00'), (0, 'R', '+-')])


# ---- tests construct ----

class TestTestsConstruct(unittest.TestCase):
    def test_table_test_pass_and_mismatch_diag(self):
        src = """
        gate(1) rot { (-) >> 0; (0) >> +; (+) >> -; }
        block(in a:1, out x:1) f { a >> rot >> x; }
        test("ok") f { (|+|) >> (|-|); (|0|) >> (|+|); }
        """
        res = tests_of(src)
        self.assertEqual(res, [('ok', True, 'ok (2 rows)')])
        src2 = """
        gate(1) rot { (-) >> 0; (0) >> +; (+) >> -; }
        block(in a:1, out x:1) f { a >> rot >> x; }
        test("bad") f { (|+|) >> (|+|); }
        """
        res2 = tests_of(src2)
        self.assertFalse(res2[0][1])
        self.assertIn('expected', res2[0][2])
        self.assertIn('-', res2[0][2])

    def test_sequential_expect_excludes_minus_one(self):
        src = """
        gate(1) rot { (-) >> 0; (0) >> +; (+) >> -; }
        test("s") until 3 with(rot r) {
          output("O") o;
          script { 0: a +; 2: a -; }
          a >> r.in.0;
          r.out >> o;
          expect { 0: O -; 2: O 0; }
        }
        """
        res = tests_of(src)
        self.assertEqual(res[0][1], True, res[0][2])

    def test_sequential_mismatch_identifies_endpoint_tick_values(self):
        src = """
        gate(1) rot { (-) >> 0; (0) >> +; (+) >> -; }
        test("s") until 2 with(rot r) {
          output("O") o;
          script { 0: a +; }
          a >> r.in.0;
          r.out >> o;
          expect { 0: O +; }
        }
        """
        res = tests_of(src)
        self.assertFalse(res[0][1])
        self.assertIn('O', res[0][2])
        self.assertIn('0', res[0][2])


# ---- namespaces and imports ----

class TestNamespaces(unittest.TestCase):
    def test_qualified_and_absolute_refs(self):
        lib = """
        namespace logic {
          gate(1) neg { (-) >> +; (0) >> 0; (+) >> -; }
        }
        namespace circuits::demo {
          block(in value:1, out result:1) invert { value >> ::logic::neg >> result; }
          block(in value:1, out result:1) invert2 { value >> logic::neg >> result; }
        }
        """
        main = """
        import "lib.tri";
        test("t1") circuits::demo::invert { (|+|) >> (|-|); }
        test("t2") circuits::demo::invert2 { (|-|) >> (|+|); }
        """
        res = tests_of(main, extra={'lib.tri': lib})
        self.assertTrue(all(r[1] for r in res), res)

    def test_namespace_reopen_and_unqualified_lookup(self):
        src = """
        namespace a { gate(1) g1 { (-) >> 0; (0) >> +; (+) >> -; } }
        namespace a { gate(1) g2 { (-) >> -; (0) >> 0; (+) >> +; } }
        namespace a::b {
          block(in x:1, out y:1, out w:1) blk { x >> g1 >> y; x >> g2 >> w; }
        }
        test("t") a::b::blk { (|+|) >> (|-, +|); }
        """
        res = tests_of(src)
        self.assertEqual(res[0][1], True, res[0][2])

    def test_duplicate_qualified_definition(self):
        src = """
        gate(1) g { (-) >> 0; (0) >> +; (+) >> -; }
        gate(1) g { (-) >> 0; (0) >> +; (+) >> -; }
        """
        with self.assertRaises(Diag) as cm:
            tests_of(src)
        self.assertIn('duplicate', str(cm.exception))

    def test_import_cycle(self):
        a = 'import "b.tri";\ngate(1) ga { (-) >> 0; (0) >> +; (+) >> -; }'
        b = 'import "a.tri";\ngate(1) gb { (-) >> 0; (0) >> +; (+) >> -; }'
        with self.assertRaises(Diag) as cm:
            tests_of('import "a.tri";', extra={'a.tri': a, 'b.tri': b})
        self.assertIn('cycle', str(cm.exception))

    def test_no_reexport_but_entrypoints_discoverable(self):
        a = """
        gate(1) ga { (-) >> 0; (0) >> +; (+) >> -; }
        bench("in_a") { input('x') x; output("O") o; x >> ga >> o; }
        """
        b = """
        import "a.tri";
        block(in x:1, out y:1) use_a { x >> ga >> y; }
        """
        c = """
        import "b.tri";
        bench("in_c") { input('x') x; output("O") o; x >> use_a >> o; }
        """
        s = Src(c, extra={'a.tri': a, 'b.tri': b})
        try:
            prog = s.prog()
            names = [b.name for _, b in prog.benches]
            self.assertIn('in_a', names)  # discoverable through the closure
            self.assertIn('in_c', names)
            rt = host.run_bench(prog, 'in_c', ticks=1, events=[(0, 'x', '+')])
            self.assertEqual(records_of(rt), [(-1, 'O', 'u'), (0, 'O', '-')])
            # but ga is not re-exported into c.tri's scope
            with self.assertRaises(Diag) as cm:
                prog.lookup_def(['ga'], False, (), 0)  # main.tri's scope: main + b.tri
            self.assertIn('unknown name', str(cm.exception))
        finally:
            s.cleanup()

    def test_ambiguous_reference(self):
        a = 'gate(1) g { (-) >> 0; (0) >> +; (+) >> -; }'
        b = 'gate(1) g { (-) >> -; (0) >> 0; (+) >> +; }'
        main = """
        import "a.tri";
        import "b.tri";
        bench("b") { input('x') x; output("O") o; x >> g >> o; }
        """
        with self.assertRaises(Diag) as cm:
            run(main, extra={'a.tri': a, 'b.tri': b})
        self.assertIn('ambiguous', str(cm.exception))


# ---- runtime API ----

class TestRuntimeAPI(unittest.TestCase):
    SRC = """
    gate(1) rot { (-) >> 0; (0) >> +; (+) >> -; }
    bench("b") { input('a') a; output("O") o; a >> rot >> o; }
    """

    def test_step_advance_outputs_subscribe_diagnostics(self):
        s = Src(self.SRC)
        try:
            prog = s.prog()
            rt, _ = host.build_bench_rt(prog, 'b')
            seen = []
            rt.subscribe(seen.append)
            rt.inject('a', ('+',), tick=0)
            rt.step()
            self.assertEqual(rt.tick, 0)
            self.assertEqual(rt.outputs(), {'O': '-'})
            self.assertEqual([(r.tick, r.label, ''.join(r.trits)) for r in seen],
                             [(0, 'O', '-')])  # init record predates subscribe
            rt.advance(3)
            self.assertEqual(rt.tick, 3)
            self.assertEqual(rt.diagnostics(), [])
        finally:
            s.cleanup()

    def test_fault_diagnostics_carry_origin(self):
        src = """
        bench("b") { input('a') a; output("O") o; a >> o; }
        """
        rt = run(src, ticks=1, events=[(0, 'a', 'e')])
        self.assertEqual(records_of(rt), [(-1, 'O', '0'), (0, 'O', 'e')])
        ds = rt.diagnostics()
        self.assertTrue(any('external' in d and 'tick 0' in d for d in ds), ds)

    def test_paced_execution_completes_every_tick(self):
        s = Src(self.SRC)
        try:
            prog = s.prog()
            rt, _ = host.build_bench_rt(prog, 'b')
            rt.run_paced(5, 0.01)
            self.assertEqual(rt.tick, 4)
        finally:
            s.cleanup()


if __name__ == '__main__':
    unittest.main()

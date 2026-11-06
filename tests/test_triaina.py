"""Conformance-style tests for the Triaina implementation."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from triaina.compiler import Program, Compiler, Diag
from triaina import host
from triaina.lexer import lex
from triaina.values import make_literal, pad_left


def write(dirpath, name, text):
    path = os.path.join(dirpath, name)
    with open(path, 'w') as f:
        f.write(text)
    return path


class Prog:
    """Compile one or more in-memory sources into a Program."""

    def __init__(self, files):
        self.dir = tempfile.mkdtemp(prefix='triaina-test-')
        self.main = write(self.dir, 'main.tri', files['main.tri'])
        for name, text in files.items():
            if name != 'main.tri':
                write(self.dir, name, text)
        self.prog = Program(self.main)

    def run(self, bench, ticks=0, events=None, until_idle=False, max_ticks=100000):
        return host.run_bench(self.prog, bench, ticks=ticks, events=events,
                              until_idle=until_idle, max_ticks=max_ticks)

    def tests(self, only=None):
        return host.run_tests(self.prog, only=only)

    def compile_check(self):
        """Force full compilation of all defs, benches and tests; Diag propagates."""
        comp = Compiler(self.prog)
        for name in self.prog.defs:
            comp.classify(name)
        for b in self.prog.benches:
            host.run_bench(self.prog, b.name, ticks=0)
        for t in self.prog.tests:
            if t.target is not None:
                c = Compiler(self.prog)
                for row in t.rows:
                    c.build_table_row(t, row)
            else:
                host._run_sequential(self.prog, t)


def records_str(rt):
    return [str(r) for r in rt.records]


class TestLexer(unittest.TestCase):
    def kinds(self, text):
        return [(t.kind, t.value) for t in lex(text) if t.kind != 'EOF']

    def test_longest_token_favors_literals(self):
        toks = self.kinds('use uez u e z u0z')
        self.assertEqual(toks[0], ('IDENT', 'use'))
        self.assertEqual(toks[1], ('LITERAL', 'uez'))
        self.assertEqual(toks[2], ('LITERAL', 'u'))
        self.assertEqual(toks[3], ('LITERAL', 'e'))
        self.assertEqual(toks[4], ('LITERAL', 'z'))
        self.assertEqual(toks[5], ('LITERAL', 'u0z'))

    def test_concat_literal(self):
        self.assertEqual(self.kinds('++-0'), [('LITERAL', '++-0')])


class TestValues(unittest.TestCase):
    def test_pad_inserts_driven_zero(self):
        v = make_literal('+u')
        w = pad_left(v, 5)
        self.assertEqual(''.join(w.trits), '000+u')

    def test_observed_maps_z(self):
        v = make_literal('z+')
        self.assertEqual(v.observed(raw=False), ('u', '+'))
        self.assertEqual(v.observed(raw=True), ('z', '+'))


GATES = """
gate(`2`) hs {
  (+, +) >> -;
  (-, -) >> +;
  (+, -) >> 0;
  (-, +) >> 0;
  (+, 0) >> +;
  (-, 0) >> -;
  (0, +) >> +;
  (0, -) >> -;
  (?, ?) >> 0;
}
gate(`1`) rot { (-) >> 0; (0) >> +; (+) >> -; }
gate(`1`) neg { (+) >> -; (0) >> 0; (-) >> +; }
gate(`3`) carry {
  (+, +, +) >> +; (+, +, 0) >> +; (+, 0, +) >> +; (0, +, +) >> +;
  (-, -, -) >> -; (-, -, 0) >> -; (-, 0, -) >> -; (0, -, -) >> -;
  (?, ?, ?) >> 0;
}
"""


class TestGates(unittest.TestCase):
    def test_hs_truth_table(self):
        prog = Prog({'main.tri': GATES + """
block(in a:1, in b:1, out r:1)
dut { |a, b| >> hs >> r; }

test("t") dut {
  (|+, +|) >> (| - |);
  (|-, -|) >> (| + |);
  (|+, -|) >> (| 0 |);
  (|-, +|) >> (| 0 |);
  (|+, 0|) >> (| + |);
  (|-, 0|) >> (| - |);
  (|0, +|) >> (| + |);
  (|0, -|) >> (| - |);
  (|0, 0|) >> (| 0 |);
}
"""})
        res = prog.tests()
        self.assertEqual([ok for _, ok, _ in res], [True], res)

    def test_unknown_enumeration_unanimous(self):
        prog = Prog({'main.tri': GATES + """
gate(`1`) c { (-) >> 0; (0) >> 0; (+) >> 0; }
block(in a:1, in b:1, out r:1)
dut { |a, b| >> hs >> r; }
block(in a:1, out r:1)
dut2 { a >> c >> r; }

test("t") dut2 {
  (|u|) >> (| 0 |);
  (|e|) >> (| e |);
}
test("t2") dut {
  (|u, 0|) >> (| u |);
}
"""})
        res = prog.tests()
        self.assertEqual([ok for _, ok, _ in res], [True, True], res)

    def test_unknown_enumeration_non_unanimous(self):
        prog = Prog({'main.tri': GATES + """
block(in a:1, in b:1, out r:1)
dut { |a, b| >> hs >> r; }

test("t") dut {
  (|u, +|) >> (| u |);
  (|u, u|) >> (| u |);
}
"""})
        res = prog.tests()
        self.assertEqual([ok for _, ok, _ in res], [True], res)

    def test_fault_propagates(self):
        prog = Prog({'main.tri': GATES + """
block(in a:1, in b:1, out r:1)
dut { |a, b| >> hs >> r; }

test("t") dut {
  (|e, +|) >> (| e |);
  (|+, e|) >> (| e |);
}
"""})
        res = prog.tests()
        self.assertEqual([ok for _, ok, _ in res], [True], res)

    def test_gate_never_outputs_z(self):
        prog = Prog({'main.tri': GATES + """
block(in a:1, in b:1, out r:1)
dut { |a, b| >> hs >> r; }

test("t") dut {
  (|z, z|) >> (| u |);
}
"""})
        res = prog.tests()
        self.assertEqual([ok for _, ok, _ in res], [True], res)

    def test_duplicate_exact_row_is_error(self):
        prog = Prog({'main.tri': """
gate(`1`) bad { (+) >> -; (+) >> +; }
block(in a:1, out r:1) d { a >> bad >> r; }
input('k') k_key;
output("O") o_out;
bench("b") with(d x) { k_key >> x.in.0; x.out >> o_out; }
"""})
        with self.assertRaises(Diag):
            prog.compile_check()

    def test_incomplete_table_is_error(self):
        prog = Prog({'main.tri': """
gate(`1`) bad { (+) >> -; }
block(in a:1, out r:1) d { a >> bad >> r; }
input('k') k_key;
output("O") o_out;
bench("b") with(d x) { k_key >> x.in.0; x.out >> o_out; }
"""})
        with self.assertRaises(Diag):
            prog.compile_check()

    def test_exhausted_wildcard_warns_but_compiles(self):
        prog = Prog({'main.tri': """
gate(`1`) g { (+) >> -; (0) >> +; (-) >> -; (?,) >> 0; }
block(in a:1, out r:1)
dut { a >> g >> r; }
test("t") dut {
  (|+|) >> (| - |);
  (|0|) >> (| + |);
  (|-|) >> (| - |);
}
"""})
        res = prog.tests()
        self.assertEqual([ok for _, ok, _ in res], [True], res)

    def test_gate_lifts_positionwise_with_extension(self):
        # b="+" zero-extends on the left to "0+"; hi: hs(+,0)=+, lo: hs(+,+)=- -> "+-"
        prog = Prog({'main.tri': GATES + """
block(in a:2, in b:1, out r:2)
dut { |a, b| >> hs >> r; }

test("t") dut {
  (|++, +|) >> (| +- |);
}
"""})
        res = prog.tests()
        self.assertEqual([ok for _, ok, _ in res], [True], res)


class TestMerge(unittest.TestCase):
    def test_merge_rules(self):
        prog = Prog({'main.tri': """
block(in a:1, in b:1, in c:1, out s:1)
m { |a, b, c| >> merge >> s; }

test("t") m {
  (|0, 0, 0|) >> (| 0 |);
  (|+, +, +|) >> (| + |);
  (|0, 0, +|) >> (| e |);
  (|u, +, 0|) >> (| u |);
}
"""})
        res = prog.tests()
        self.assertEqual([ok for _, ok, _ in res], [True], res)

    def test_merge_raw_z_from_endpoints(self):
        prog = Prog({'main.tri': """
input('a') a_key;
input('b') b_key;
input('c') c_key;
output("S") s_out;
bench("b") {
  |a_key, b_key, c_key| >> merge >> s_out;
}
"""})
        rt = prog.run('b', ticks=9, events={
            1: {'a': '0', 'b': '0', 'c': 'z'},   # -> 0
            3: {'a': 'z', 'b': '+', 'c': 'z'},   # -> +
            5: {'a': 'z', 'b': 'z', 'c': 'z'},   # -> u
            7: {'a': '0', 'b': '0', 'c': '+'},   # -> e (contention)
        })
        posts = [(r.tick, ''.join(r.trits)) for r in rt.records if r.phase == 'post']
        self.assertEqual(posts, [(0, '0'), (3, '+'), (5, 'u'), (7, 'e')])

    def test_single_contribution_passthrough_and_lone_z(self):
        prog = Prog({'main.tri': """
block(in a:1, out s:1)
m { |a| >> merge >> s; }

test("t") m {
  (|+|) >> (| + |);
  (|z|) >> (| u |);
  (|u|) >> (| u |);
}
"""})
        res = prog.tests()
        self.assertEqual([ok for _, ok, _ in res], [True], res)

    def test_unequal_widths_diagnosed(self):
        prog = Prog({'main.tri': """
block(in a:1, in b:2, out s:1)
m { |a, b| >> merge >> s; }
input('k', 0) ka;
input('j', 00) kb;
output("O") o_out;
bench("b") with(m x) { ka >> x.in.0; kb >> x.in.1; x.out >> o_out; }
"""})
        with self.assertRaises(Diag):
            prog.compile_check()

    def test_contention_provenance(self):
        prog = Prog({'main.tri': """
block(in a:1, in b:1, out s:1)
m { |a, b| >> merge >> s; }

input('a') a_key;
input('b') b_key;
output("S") s_out;
bench("b") with(m mm) {
  a_key >> mm.in.0;
  b_key >> mm.in.1;
  mm.out >> s_out;
}
"""})
        rt = prog.run('b', ticks=3, events={1: {'a': '+', 'b': '-'}})
        recs = [r for r in rt.records if r.phase == 'post']
        self.assertTrue(recs)
        self.assertEqual(recs[-1].trits, ('e',))
        s_net = [n for n in rt.nets if n.name == 'output.s_out'][0]
        prov = s_net.value.prov
        self.assertTrue(any(p[1] == 'contention' for p in prov), prov)
        self.assertTrue(any(p[0] == 1 for p in prov), prov)  # tick index of contention
        detail = [p for p in prov if p[1] == 'contention'][0][2]
        self.assertEqual(sorted(s for _, s in detail), ['+', '-'])


class TestZWiring(unittest.TestCase):
    def test_z_read_as_u_by_outputs_but_raw_by_merge(self):
        prog = Prog({'main.tri': """
block(in a:1, out raw:1, out obs:1)
ztest {
  |a, a| >> merge >> raw;
  a >> obs;
}

input('k') k_key;
output("RAW") raw_out;
output("OBS") obs_out;
bench("b") with(ztest zt) {
  k_key >> zt.in.0;
  zt.out.0 >> raw_out;
  zt.out.1 >> obs_out;
}
"""})
        rt = prog.run('b', ticks=3, events={1: {'k': 'z'}})
        recs = {(r.label, r.phase): ''.join(r.trits) for r in rt.records}
        self.assertEqual(recs[('RAW', 'post')], 'u')   # merge(z, z) = u
        self.assertEqual(recs[('OBS', 'post')], 'u')   # non-merge read of z = u

    def test_merge_sees_raw_z_from_literal(self):
        prog = Prog({'main.tri': """
block(in a:1, out s:1)
zmerge { |a, z| >> merge >> s; }

test("t") zmerge {
  (|+|) >> (| + |);
  (|0|) >> (| 0 |);
}
"""})
        res = prog.tests()
        self.assertEqual([ok for _, ok, _ in res], [True], res)


class TestWidths(unittest.TestCase):
    def test_zero_extend_at_fixed_width_destination(self):
        prog = Prog({'main.tri': """
block(in a:2, out r:4)
w { a >> r; }

test("t") w {
  (|+u|) >> (|00+u|);
}
"""})
        res = prog.tests()
        self.assertEqual([ok for _, ok, _ in res], [True], res)

    def test_too_wide_is_error(self):
        prog = Prog({'main.tri': """
block(in a:2, out r:1)
w { a >> r; }
input('k', 00) k_key;
output("O") o_out;
bench("b") with(w x) { k_key >> x.in.0; x.out >> o_out; }
"""})
        with self.assertRaises(Diag):
            prog.compile_check()

    def test_param_widths_cat(self):
        prog = Prog({'main.tri': """
block(in left:?n, in right:?m, out res:(n + m))
cat { left % right >> res; }

test("t") cat {
  (|+, 00|) >> (| +00 |);
  (|-u, +0z|) >> (| -u+0z |);
}
"""})
        res = prog.tests()
        self.assertEqual([ok for _, ok, _ in res], [True], res)

    def test_unresolved_width_cycle_is_error(self):
        prog = Prog({'main.tri': GATES + """
block(out q:?, out nq:?)
ring
with(neg g1, neg g2) {
  @wq >> g1;
  g1 >> @nq >> g2;
  g2 >> @wq >> q;
  @nq >> nq;
}
output("Q") q_out;
bench("b") with(ring r) { r.out.0 >> q_out; }
"""})
        with self.assertRaises(Diag):
            prog.compile_check()

    def test_computed_width_arithmetic(self):
        prog = Prog({'main.tri': """
block(in a:2, out r:(2 * 2))
w { a % a >> r; }

test("t") w {
  (|+-|) >> (|+-+-|);
}
"""})
        res = prog.tests()
        self.assertEqual([ok for _, ok, _ in res], [True], res)


class TestSlicingConcat(unittest.TestCase):
    def test_slices_and_reversal(self):
        # v = "+0-u": index 0 is the rightmost trit ('u')
        # v[1] = '-', v[1:3] has index0=orig1='-', index1=orig2='0' -> written "0-"
        # v[:-1] reverses -> "u-0+"
        prog = Prog({'main.tri': """
block(in v:4, out a:1, out b:2, out c:4)
s {
  v[1] >> a;
  v[1:3] >> b;
  v[:-1] >> c;
}

test("t") s {
  (|+0-u|) >> (| -, 0-, u-0+ |);
}
"""})
        res = prog.tests()
        self.assertEqual([ok for _, ok, _ in res], [True], res)

    def test_invalid_slice_bounds(self):
        prog = Prog({'main.tri': """
block(in v:2, out a:1)
s { v[2:3] >> a; }
input('k', 00) k_key;
output("O") o_out;
bench("b") with(s x) { k_key >> x.in.0; x.out >> o_out; }
"""})
        with self.assertRaises(Diag):
            prog.compile_check()


class TestChainsAdapters(unittest.TestCase):
    def test_adapter_reorder_insert_dup(self):
        prog = Prog({'main.tri': GATES + """
block(in a:1, in b:1, in c:1, out r:1)
reorder { |a, b, c| >> |$2, $0, +| >> carry >> r; }
block(in a:1, in b:1, out r:1)
dup { |a, b| >> |$0, $0| >> hs >> r; }

test("t1") reorder {
  (|+, 0, -|) >> (| 0 |);   // carry(c, a, +) = carry(-, +, +) = 0
}
test("t2") dup {
  (|+, 0|) >> (| - |);      // hs(a, a) = hs(+, +) = -
}
"""})
        res = prog.tests()
        self.assertEqual([ok for _, ok, _ in res], [True, True], res)

    def test_named_connection_and_forward_feedback(self):
        prog = Prog({'main.tri': GATES + """
block(out q:1, out nq:1)
ring
with(neg g1, neg g2) {
  @wq >> g1;
  g1 >> @nq >> g2;
  g2 >> @wq >> q;
  @nq >> nq;
}

output("Q") q_out;
bench("b") with(ring r) {
  r.out.0 >> q_out;
}
"""})
        rt = prog.run('b', until_idle=True)
        self.assertEqual(records_str(rt), ['0 init Q: u'])
        self.assertFalse(rt.cur_queue)

    def test_multiple_drivers_rejected(self):
        prog = Prog({'main.tri': GATES + """
block(in a:1, in b:1, out r:1)
d { a >> r; b >> r; }
"""})
        with self.assertRaises(Diag):
            prog.compile_check()

    def test_undriven_output_rejected(self):
        prog = Prog({'main.tri': GATES + """
block(in a:1, out r:1, out s:1)
d { a >> r; }
"""})
        with self.assertRaises(Diag):
            prog.compile_check()

    def test_undriven_instance_input_rejected(self):
        prog = Prog({'main.tri': GATES + """
block(in a:1, out r:1)
d with(hs h) { a >> h.in.0; h >> r; }
input('k') k_key;
output("O") o_out;
bench("b") with(d x) { k_key >> x.in.0; x.out >> o_out; }
"""})
        with self.assertRaises(Diag):
            prog.compile_check()

    def test_arity_mismatch_rejected(self):
        prog = Prog({'main.tri': GATES + """
block(in a:1, out r:1)
d { |a| >> hs >> r; }
"""})
        with self.assertRaises(Diag):
            prog.compile_check()


class TestScheduler(unittest.TestCase):
    def test_one_node_per_tick_along_path(self):
        prog = Prog({'main.tri': GATES + """
input('a') a_key;
output("O") o_out;
bench("b") with(neg n1, neg n2) {
  a_key >> n1;
  n1 >> n2;
  n2 >> o_out;
}
"""})
        rt = prog.run('b', ticks=6, events={0: {'a': '+'}})
        recs = [(r.tick, r.phase, ''.join(r.trits)) for r in rt.records]
        self.assertEqual(recs[0], (0, 'init', 'u'))
        # a=+ applied at tick 0: n1 commits end of tick 0, n2 end of tick 1,
        # so the output cannot reflect both gates before tick 1
        posts = [r for r in recs if r[1] == 'post']
        self.assertEqual(posts, [(1, 'post', '+')])  # neg(neg(+)) = +

    def test_simultaneous_commit_and_snapshot(self):
        prog = Prog({'main.tri': GATES + """
block(in a:1, out x:1, out y:1)
d with(neg n) { a >> n >> x; n >> y; }

test("t") d {
  (|+|) >> (| -, - |);
}
"""})
        res = prog.tests()
        self.assertEqual([ok for _, ok, _ in res], [True], res)

    def test_queue_dedup(self):
        prog = Prog({'main.tri': GATES + """
input('a') a_key;
output("O") o_out;
bench("b") with(hs h) {
  |a_key, a_key| >> h;
  h >> o_out;
}
"""})
        rt = prog.run('b', ticks=4, events={0: {'a': '+'}})
        posts = [r for r in rt.records if r.phase == 'post']
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0].trits, ('-',))  # hs(+,+) = -


class TestClocksRegisters(unittest.TestCase):
    def test_clock_edges_by_tick_index(self):
        prog = Prog({'main.tri': """
clock(`50`, `50`) clk;
output("C") c_out;
bench("b") { clk >> c_out; }
"""})
        rt = prog.run('b', ticks=130)
        recs = [(r.tick, ''.join(r.trits)) for r in rt.records]
        for edge in [(25, '+'), (50, '-'), (75, '+'), (100, '-'), (125, '+')]:
            self.assertIn(edge, recs)

    def test_clock_config_rational(self):
        prog = Prog({'main.tri': """
clock(`1000 / 20`, `50`) clk;
output("C") c_out;
bench("b") { clk >> c_out; }
"""})
        rt = prog.run('b', ticks=60)
        recs = [(r.tick, ''.join(r.trits)) for r in rt.records]
        self.assertIn((25, '+'), recs)
        self.assertIn((50, '-'), recs)

    def test_bad_duty_rejected(self):
        prog = Prog({'main.tri': """
clock(`3`, `50`) clk;
output("C") c_out;
bench("b") { clk >> c_out; }
"""})
        with self.assertRaises(Diag):
            prog.compile_check()

    def test_register_samples_on_rising_edge_only(self):
        prog = Prog({'main.tri': GATES + """
block(in d:1, in clk:1, out q:1)
rg with(register(+) r) {
  d >> r.in.0;
  clk >> r.in.1;
  r.out >> q;
}

input('d') d_key;
clock(`4`, `50`) clk;
output("Q") q_out;
bench("b") with(rg r) {
  d_key >> r.in.0;
  clk >> r.in.1;
  r.out >> q_out;
}
"""})
        # period 4 duty 50 -> H=2, L=2: rises at ticks 2, 6, 10...
        rt = prog.run('b', ticks=8, events={0: {'d': '-'}})
        recs = [(r.tick, r.phase, ''.join(r.trits)) for r in rt.records]
        self.assertEqual(recs[0], (0, 'init', '+'))
        self.assertIn((2, 'post', '-'), recs)

    def test_register_holds_without_edge(self):
        prog = Prog({'main.tri': GATES + """
block(in d:1, in clk:1, out q:1)
rg with(register(0) r) {
  d >> r.in.0;
  clk >> r.in.1;
  r.out >> q;
}

input('d') d_key;
clock(`4`, `25`) clk;
output("Q") q_out;
bench("b") with(rg r) {
  d_key >> r.in.0;
  clk >> r.in.1;
  r.out >> q_out;
}
"""})
        # period 4 duty 25 -> H=1, L=3: rises at 3, falls at 4, rises at 7
        rt = prog.run('b', ticks=8, events={1: {'d': '+'}})
        recs = [(r.tick, r.phase, ''.join(r.trits)) for r in rt.records]
        self.assertIn((3, 'post', '+'), recs)
        posts_at_4 = [r for r in recs if r[1] == 'post' and r[0] == 4]
        self.assertEqual(posts_at_4, [])

    def test_clock_through_gate_rejected(self):
        prog = Prog({'main.tri': GATES + """
block(in d:1, in clk:1, out q:1)
rg with(register(0) r, neg n) {
  d >> r.in.0;
  clk >> n;
  n >> r.in.1;
  r.out >> q;
}

input('d') d_key;
clock(`4`, `50`) clk;
output("Q") q_out;
bench("b") with(rg r) {
  d_key >> r.in.0;
  clk >> r.in.1;
  r.out >> q_out;
}
"""})
        with self.assertRaises(Diag):
            prog.compile_check()

    def test_register_samples_pre_commit_data(self):
        # d reaches the register through one gate; a change applied on the same
        # tick as the rising edge must NOT be captured (pre-commit snapshot)
        prog = Prog({'main.tri': GATES + """
block(in a:1, in clk:1, out q:1)
rg with(register(+) r, neg n) {
  a >> n;
  n >> r.in.0;
  clk >> r.in.1;
  r.out >> q;
}

input('a') a_key;
clock(`4`, `50`) clk;
output("Q") q_out;
bench("b") with(rg r) {
  a_key >> r.in.0;
  clk >> r.in.1;
  r.out >> q_out;
}
"""})
        # clock rises at ticks 2, 6; a goes + at tick 2
        rt = prog.run('b', ticks=8, events={2: {'a': '+'}})
        recs = [(r.tick, r.phase, ''.join(r.trits)) for r in rt.records]
        self.assertEqual(recs[0], (0, 'init', '+'))
        # at tick 2 the register samples neg.out before this tick's commit: still '0'
        self.assertIn((2, 'post', '0'), recs)
        # at tick 6 it samples the committed '-' 
        self.assertIn((6, 'post', '-'), recs)

    def test_register_initializer_z_observed_u(self):
        prog = Prog({'main.tri': """
block(in clk:1, out obs:1)
rg with(register(z) r) {
  0 >> r.in.0;
  clk >> r.in.1;
  r.out >> obs;
}

clock(`2`, `50`) clk;
output("OBS") obs_out;
bench("b") with(rg r) {
  clk >> r.in.0;
  r.out >> obs_out;
}
"""})
        rt = prog.run('b', ticks=1)
        recs = {r.label: ''.join(r.trits) for r in rt.records}
        self.assertEqual(recs['OBS'], 'u')


class TestClassification(unittest.TestCase):
    def test_feedback_block_rejects_table_test(self):
        prog = Prog({'main.tri': GATES + """
block(in s:1, out q:1)
latch {
  match s {
    + >> +;
    - >> -;
    _ >> q;
  } >> q;
}

test("t") latch {
  (|+|) >> (| + |);
}
"""})
        res = prog.tests()
        self.assertFalse(res[0][1])
        self.assertIn('combinational', res[0][2])

    def test_inline_call_of_feedback_rejected(self):
        prog = Prog({'main.tri': GATES + """
block(in s:1, out q:1)
latch {
  match s {
    + >> +;
    - >> -;
    _ >> q;
  } >> q;
}
block(in s:1, out r:1)
d { latch(s) >> r; }
"""})
        with self.assertRaises(Diag):
            prog.compile_check()

    def test_latch_sequential_hold(self):
        prog = Prog({'main.tri': GATES + """
block(in s:1, out q:1)
latch {
  match s {
    + >> +;
    - >> -;
    _ >> q;
  } >> q;
}

input('s') s_key;
output("Q") q_out;

test("hold") until `10` with(latch l) {
  script {
    `0`: s +;
    `4`: s u;
  }
  s >> l.in.0;
  l.out >> q_out;
  expect {
    `0`: init Q u;
    `0`: post Q +;
  }
}
"""})
        res = prog.tests()
        self.assertEqual([ok for _, ok, _ in res], [True], res)


class TestMatchFold(unittest.TestCase):
    def test_match_first_match_and_wildcard(self):
        prog = Prog({'main.tri': GATES + """
block(in v:2, out r:1)
sel {
  match v {
    +_ >> +;
    -? >> -;
    _  >> 0;
  } >> r;
}

test("t") sel {
  (|+0|) >> (| + |);
  (|++|) >> (| + |);
  (|-+|) >> (| - |);
  (|-0|) >> (| - |);
  (|0u|) >> (| 0 |);
}
"""})
        res = prog.tests()
        self.assertEqual([ok for _, ok, _ in res], [True], res)

    def test_final_underscore_required(self):
        prog = Prog({'main.tri': """
block(in v:1, out r:1)
sel { match v { + >> +; } >> r; }
"""})
        with self.assertRaises(Diag):
            prog.compile_check()

    def test_z_not_a_pattern(self):
        prog = Prog({'main.tri': """
block(in v:1, out r:1)
sel { match v { z >> +; _ >> 0; } >> r; }
"""})
        with self.assertRaises(Diag):
            prog.compile_check()

    def test_fold(self):
        prog = Prog({'main.tri': GATES + """
block(in a:1, in b:1, in c:1, out r:1)
f { |a, b, c| >> fold(hs) >> r; }

test("t") f {
  (|+, +, +|) >> (| 0 |);
  (|+, +, 0|) >> (| - |);
  (|+, 0, 0|) >> (| + |);
}
"""})
        res = prog.tests()
        self.assertEqual([ok for _, ok, _ in res], [True], res)


class TestImports(unittest.TestCase):
    def test_import_and_closure_discovery(self):
        prog = Prog({
            'main.tri': """
import "lib.tri";
input('a') a_key;
output("R") r_out;
bench("b") with(rot r) { a_key >> r; r >> r_out; }
test("imp test") dut2 {
  (|+|) >> (| - |);
}
""",
            'lib.tri': GATES + """
block(in a:1, out r:1)
dut2 { a >> neg >> r; }
""",
        })
        self.assertIn('dut2', prog.prog.defs)
        self.assertEqual([t.name for t in prog.prog.tests], ['imp test'])
        res = prog.tests()
        self.assertEqual([ok for _, ok, _ in res], [True], res)

    def test_import_cycle_rejected(self):
        with self.assertRaises(Diag):
            Prog({
                'main.tri': 'import "b.tri";\n',
                'b.tri': 'import "main.tri";\n',
            })

    def test_name_collision_rejected(self):
        with self.assertRaises(Diag):
            Prog({
                'main.tri': 'import "b.tri";\ngate(`1`) rot { (+) >> -; (0) >> 0; (-) >> +; }\n',
                'b.tri': GATES,
            })

    def test_duplicate_key_rejected(self):
        with self.assertRaises(Diag):
            Prog({'main.tri': """
input('a') x;
input('a') y;
"""})


class TestEmission(unittest.TestCase):
    def test_changes_vs_every_step(self):
        prog = Prog({'main.tri': GATES + """
input('a') a_key;
output("O", every_step) o_out;
bench("b") with(neg n) { a_key >> n; n >> o_out; }
"""})
        rt = prog.run('b', ticks=4, events={1: {'a': '+'}})
        posts = [r for r in rt.records if r.phase == 'post']
        self.assertEqual(len(posts), 4)
        self.assertEqual([r.tick for r in posts], [0, 1, 2, 3])

    def test_changes_only_on_change(self):
        prog = Prog({'main.tri': GATES + """
input('a') a_key;
output("O") o_out;
bench("b") with(neg n) { a_key >> n; n >> o_out; }
"""})
        rt = prog.run('b', ticks=4, events={1: {'a': '+'}})
        posts = [(r.tick, ''.join(r.trits)) for r in rt.records if r.phase == 'post']
        self.assertEqual(posts, [(0, '0'), (1, '-')])


class TestSequentialTests(unittest.TestCase):
    def test_expect_mismatch_reports_values(self):
        prog = Prog({'main.tri': GATES + """
input('a') a_key;
output("O") o_out;

test("t") until `5` with(neg n) {
  script { `1`: a +; }
  a >> n;
  n >> o_out;
  expect {
    `0`: init O u;
    `3`: post O 0;
  }
}
"""})
        res = prog.tests()
        self.assertFalse(res[0][1])
        self.assertIn('expected', res[0][2])

    def test_input_z_keyboard_record(self):
        prog = Prog({'main.tri': """
input('k', z) k_key;
output("O") o_out;
bench("b") { k_key >> o_out; }
"""})
        rt = prog.run('b', ticks=2)
        self.assertEqual(records_str(rt), ['0 init O: u'])


class TestProvenance(unittest.TestCase):
    def test_literal_e_origin(self):
        prog = Prog({'main.tri': GATES + """
input('a') a_key;
output("O") o_out;
bench("b") with(neg n) { a_key >> n; n >> o_out; }
"""})
        rt = prog.run('b', ticks=2, events={1: {'a': 'e'}})
        o = [n for n in rt.nets if n.name == 'output.o_out'][0]
        self.assertIn('e', o.value.trits)
        self.assertTrue(any(p[1] == 'external' for p in o.value.prov), o.value.prov)


if __name__ == '__main__':
    unittest.main()

"""Compiler: loads files/imports, resolves names, infers widths, classifies
blocks, traces clock origins, and builds the runtime graph."""

import os
from fractions import Fraction
from itertools import product

from . import ast_nodes as A
from .parser import parse_file
from .runtime import (Runtime, NetSrc, ConstSrc, SliceSrc, ConcatSrc, LateSrc,
                      GateNode, MatchNode, MergeNode, FoldNode, RegisterNode,
                      Endpoint)
from .values import (NEG, ZERO, POS, U, E, Z, LOGICAL, ALL_STATES, Value,
                     make_literal)


class Diag(Exception):
    def __init__(self, msg, line=None, file=None):
        self.msg = msg
        self.line = line
        self.file = file
        super().__init__(self._fmt())

    def _fmt(self):
        loc = ''
        if self.file:
            loc = self.file + ':'
            if self.line:
                loc += str(self.line) + ':'
        return ('%s %s' % (loc, self.msg)) if loc else self.msg


# ---- numeric expressions ----

def eval_num(e, env, line=0, file=None, what='numeric expression'):
    if isinstance(e, A.NumLit):
        return e.value
    if isinstance(e, A.NumParam):
        if e.name not in env:
            raise Diag('unresolved width parameter %s in %s' % (e.name, what), line, file)
        return env[e.name]
    if isinstance(e, A.NumUnary):
        v = eval_num(e.operand, env, line, file, what)
        return -v if e.op == '-' else v
    if isinstance(e, A.NumBinary):
        l = eval_num(e.left, env, line, file, what)
        r = eval_num(e.right, env, line, file, what)
        if e.op == '+':
            return l + r
        if e.op == '-':
            return l - r
        if e.op == '*':
            return l * r
        if r == 0:
            raise Diag('division by zero in %s' % what, line, file)
        return l / r
    raise Diag('bad numeric expression', line, file)


# ---- width solver: union-find over width expressions ----

class WNode:
    __slots__ = ('parent', 'expr', 'value')

    def __init__(self):
        self.parent = self
        self.expr = None
        self.value = None


def wfind(w):
    root = w
    while root.parent is not root:
        root = root.parent
    while w.parent is not root:
        w.parent, w = root, w.parent
    return root


def free_params(e, out=None):
    if out is None:
        out = set()
    if e is None:
        return out
    if isinstance(e, WNode):
        e = ('wnode', e)
    if e[0] == 'param':
        out.add((e[1], e[2]))
    elif e[0] == 'wnode':
        free_params(wfind(e[1]).expr, out)
    elif e[0] == 'un':
        free_params(e[2], out)
    elif e[0] == 'bin':
        free_params(e[2], out)
        free_params(e[3], out)
    elif e[0] in ('sum', 'max'):
        for x in e[1]:
            free_params(x, out)
    return out


class WSolver:
    def __init__(self):
        self.track = []
        self.bindings = {}
        self.le = []       # (WNode, Fraction, what, line)
        self.pending = []  # (expr, expr)

    def new(self):
        w = WNode()
        self.track.append(w)
        return w

    def const_node(self, frac):
        w = self.new()
        w.expr = ('const', frac)
        return w

    def subst(self, e, seen=None):
        if e is None:
            return None
        if isinstance(e, WNode):
            e = ('wnode', e)
        if e[0] == 'param':
            key = (e[1], e[2])
            if key in self.bindings:
                return self.subst(self.bindings[key])
            return e
        if e[0] == 'wnode':
            r = wfind(e[1])
            if seen is None:
                seen = set()
            if id(r) in seen:
                return e
            seen.add(id(r))
            return self.subst(r.expr, seen)
        if e[0] in ('const',):
            return e
        if e[0] == 'un':
            return ('un', e[1], self.subst(e[2]))
        if e[0] == 'bin':
            return ('bin', e[1], self.subst(e[2]), self.subst(e[3]))
        if e[0] in ('sum', 'max'):
            return (e[0], tuple(self.subst(x) for x in e[1]))
        return e

    def norm(self, e):
        e = self.subst(e)
        if e is None:
            return None
        if e[0] == 'wnode':
            return self.norm(wfind(e[1]).expr)
        if e[0] == 'un':
            return ('un', e[1], self.norm(e[2]))
        if e[0] == 'bin':
            return ('bin', e[1], self.norm(e[2]), self.norm(e[3]))
        if e[0] in ('sum', 'max'):
            return (e[0], tuple(self.norm(x) for x in e[1]))
        return e

    def combine(self, e1, e2, line=0):
        e1 = self.subst(e1)
        e2 = self.subst(e2)
        if e1 is None:
            return e2
        if e2 is None:
            return e1
        if e1[0] == 'const' and e2[0] == 'const':
            if e1[1] != e2[1]:
                raise Diag('conflicting widths %s and %s' % (e1[1], e2[1]), line)
            return e1
        for e, other in ((e1, e2), (e2, e1)):
            if e[0] == 'param':
                key = (e[1], e[2])
                if key not in self.bindings and key not in free_params(other):
                    self.bindings[key] = other
                    return other
        # a constant meeting a structural expression keeps the structure;
        # the constant is enforced through <= constraints recorded by set_driver
        if e1[0] == 'const' and e2[0] != 'const':
            v = self.eval(e2)
            if v is not None and v != e1[1]:
                raise Diag('conflicting widths %s and %s' % (e1[1], v), line)
            self.pending.append((e1, e2))
            return e2
        if e2[0] == 'const' and e1[0] != 'const':
            v = self.eval(e1)
            if v is not None and v != e2[1]:
                raise Diag('conflicting widths %s and %s' % (v, e2[1]), line)
            self.pending.append((e1, e2))
            return e1
        n1, n2 = self.norm(e1), self.norm(e2)
        if n1 == n2:
            return e1
        self.pending.append((e1, e2))
        return e1

    def union(self, a, b, line=0):
        ra, rb = wfind(a), wfind(b)
        if ra is rb:
            return
        rb.parent = ra
        ra.expr = self.combine(ra.expr, rb.expr, line)

    def eval(self, e, seen=None):
        e = self.subst(e)
        if e is None:
            return None
        if seen is None:
            seen = set()
        if e[0] == 'const':
            return e[1]
        if e[0] == 'param':
            return None
        if e[0] == 'wnode':
            r = wfind(e[1])
            if id(r) in seen:
                return None
            seen.add(id(r))
            return self.eval(r.expr, seen)
        if e[0] == 'un':
            v = self.eval(e[2], seen)
            return None if v is None else -v
        if e[0] == 'bin':
            l = self.eval(e[2], seen)
            r = self.eval(e[3], seen)
            if l is None or r is None:
                return None
            if e[1] == '+':
                return l + r
            if e[1] == '-':
                return l - r
            if e[1] == '*':
                return l * r
            if r == 0:
                raise Diag('division by zero in width expression')
            return l / r
        if e[0] == 'sum':
            total = Fraction(0)
            for x in e[1]:
                v = self.eval(x, seen)
                if v is None:
                    return None
                total += v
            return total
        if e[0] == 'max':
            vals = [self.eval(x, seen) for x in e[1]]
            if any(v is None for v in vals):
                return None
            return max(vals)
        return None

    def solve(self):
        for _ in range(len(self.track) + 2):
            if not self.pending:
                break
            rest = []
            progressed = False
            for e1, e2 in self.pending:
                n1, n2 = self.norm(e1), self.norm(e2)
                v1, v2 = self.eval(e1), self.eval(e2)
                if v1 is not None and v2 is not None:
                    if v1 != v2:
                        raise Diag('conflicting widths %s and %s' % (v1, v2))
                    progressed = True
                    continue
                if n1 is None or n2 is None:
                    rest.append((e1, e2))
                    continue
                if n1[0] == 'const' and n2[0] == 'const':
                    if n1[1] != n2[1]:
                        raise Diag('conflicting widths %s and %s' % (n1[1], n2[1]))
                    progressed = True
                    continue
                bound = False
                for e, other in ((n1, n2), (n2, n1)):
                    if e[0] == 'param':
                        key = (e[1], e[2])
                        if key not in self.bindings and key not in free_params(other):
                            self.bindings[key] = other
                            progressed = True
                            bound = True
                            break
                if bound:
                    continue
                if n1 == n2:
                    progressed = True
                    continue
                rest.append((e1, e2))
            self.pending = rest
            if not progressed:
                break
        for w in self.track:
            r = wfind(w)
            if r.value is None:
                r.value = self.eval(r.expr)


# ---- program loading ----

class Program:
    def __init__(self, main_path):
        self.main_path = os.path.normpath(main_path)
        self.loaded = []
        self.defs = {}
        self.inputs = {}
        self.outputs = {}
        self.clocks = {}
        self.keys = {}
        self.benches = []
        self.tests = []
        self._load(self.main_path, [], set())

    def _load(self, path, stack, done):
        if path in stack:
            raise Diag('import cycle: %s' % ' -> '.join(stack + [path]))
        if path in done:
            return
        defs = parse_file(path)
        done.add(path)
        stack.append(path)
        for d in defs:
            if isinstance(d, A.Import):
                self._load(os.path.normpath(os.path.join(os.path.dirname(path), d.path)),
                           stack, done)
        stack.pop()
        self.loaded.append((path, defs))
        for d in defs:
            d.file = path
            if isinstance(d, (A.GateDef, A.BlockDef)):
                if d.name in self.defs:
                    raise Diag('name collision on %r' % d.name, d.line, path)
                self.defs[d.name] = d
            elif isinstance(d, A.InputDef):
                if d.name in self.inputs or d.name in self.outputs or d.name in self.clocks:
                    raise Diag('name collision on %r' % d.name, d.line, path)
                if d.key in self.keys:
                    raise Diag('duplicate input key %r' % d.key, d.line, path)
                self.inputs[d.name] = d
                self.keys[d.key] = d
            elif isinstance(d, A.OutputDef):
                if d.name in self.inputs or d.name in self.outputs or d.name in self.clocks:
                    raise Diag('name collision on %r' % d.name, d.line, path)
                self.outputs[d.name] = d
            elif isinstance(d, A.ClockDef):
                if d.name in self.inputs or d.name in self.outputs or d.name in self.clocks:
                    raise Diag('name collision on %r' % d.name, d.line, path)
                self.clocks[d.name] = d
            elif isinstance(d, A.BenchDef):
                self.benches.append(d)
            elif isinstance(d, A.TestDef):
                self.tests.append(d)


# ---- gate tables ----

def build_gate_table(rows, arity, name, file=None, line=0):
    assigned = {}
    warnings = []
    for pat, res in rows:
        if any(it[0] == 'seq' for it in pat):
            raise Diag('gate %s: _ is not allowed in gate rows' % name, line, file)
        if len(pat) != arity:
            raise Diag('gate %s: row has %d positions, arity is %d' % (name, len(pat), arity),
                       line, file)
        if not any(it[0] == 'any' for it in pat):
            tup = tuple(it[1] for it in pat)
            if tup in assigned:
                raise Diag('gate %s: exact row %s designates an already assigned tuple'
                           % (name, tup), line, file)
            assigned[tup] = res
        else:
            matched = 0
            for tup in product(LOGICAL, repeat=arity):
                if tup in assigned:
                    continue
                if all(it[0] == 'any' or it[1] == t for it, t in zip(pat, tup)):
                    assigned[tup] = res
                    matched += 1
            if matched == 0:
                warnings.append('gate %s: exhausted wildcard row' % name)
    total = 3 ** arity
    if len(assigned) != total:
        missing = [t for t in product(LOGICAL, repeat=arity) if t not in assigned]
        raise Diag('gate %s: truth table does not cover all %d tuples (missing %d, e.g. %s)'
                   % (name, total, len(missing), missing[0]), line, file)
    return assigned, warnings


# ---- instances and scopes ----

class Inst:
    def __init__(self, key, name, defn):
        self.key = key
        self.name = name
        self.defn = defn
        self.in_nets = []
        self.out_nets = []
        self.node = None


class Ctx:
    def __init__(self, comp, inputs, outputs, def_stack, inst_key=None):
        self.comp = comp
        self.inputs = inputs
        self.outputs = outputs
        self.insts = {}
        self.named = {}
        self.pending_named = {}
        self.def_stack = def_stack
        self.inst_key = inst_key


class Compiler:
    def __init__(self, prog):
        self.prog = prog
        self.solver = WSolver()
        self.rt = None
        self.class_memo = {}
        self._classifying = []
        self.inst_counter = 0
        self.all_insts = []
        self.warnings = []

    # ---- width helpers ----

    def num_to_wexpr(self, e, inst_key, line=0, file=None):
        if isinstance(e, A.NumLit):
            return ('const', e.value)
        if isinstance(e, A.NumParam):
            return ('param', inst_key, e.name)
        if isinstance(e, A.NumUnary):
            return ('un', e.op, self.num_to_wexpr(e.operand, inst_key, line, file))
        if isinstance(e, A.NumBinary):
            return ('bin', e.op, self.num_to_wexpr(e.left, inst_key, line, file),
                    self.num_to_wexpr(e.right, inst_key, line, file))
        raise Diag('bad width expression', line, file)

    def const_int(self, e, line=0, file=None, what='value'):
        v = eval_num(e, {}, line, file, what)
        if v.denominator != 1:
            raise Diag('%s must be integral, got %s' % (what, v), line, file)
        return int(v)

    # ---- instantiation ----

    def instantiate(self, type_name, inst_name, ctx, init=None, line=0):
        self.inst_counter += 1
        key = self.inst_counter
        rt = self.rt
        if type_name == 'register':
            if init is None:
                raise Diag('register needs an initializer', line)
            if not init or any(c not in ALL_STATES for c in init):
                raise Diag('bad register initializer %r' % init, line)
            val = make_literal(init)
            data_w = self.solver.new()
            clk_w = self.solver.new()
            out_w = self.solver.new()
            out_w.expr = ('const', Fraction(len(init)))
            data_net = rt.new_net('%s.data' % inst_name, data_w)
            clk_net = rt.new_net('%s.clk' % inst_name, clk_w)
            out_net = rt.new_net('%s.out' % inst_name, out_w)
            node = RegisterNode(rt, inst_name, val)
            node.outs = [out_net]
            out_net.driver = ('node', node)
            node.add_input(NetSrc(data_net), 'normal', '%s.data' % inst_name)
            node.add_input(NetSrc(clk_net), 'normal', '%s.clk' % inst_name)
            inst = Inst(key, inst_name, None)
            inst.in_nets = [data_net, clk_net]
            inst.out_nets = [out_net]
            inst.node = node
            self.all_insts.append(inst)
            if inst_name is not None:
                ctx.insts[inst_name] = inst
            return inst
        defn = self.prog.defs.get(type_name)
        if defn is None:
            raise Diag('unknown component %r' % type_name, line)
        if isinstance(defn, A.GateDef):
            arity = self.const_int(defn.arity, defn.line, defn.file, 'gate arity')
            if arity < 1:
                raise Diag('gate arity must be positive', defn.line, defn.file)
            table, warns = build_gate_table(defn.rows, arity, type_name, defn.file, defn.line)
            self.warnings.extend(warns)
            in_nets = []
            in_ws = []
            for i in range(arity):
                w = self.solver.new()
                in_ws.append(w)
                in_nets.append(rt.new_net('%s.in.%d' % (inst_name, i), w))
            out_w = self.solver.new()
            out_w.expr = ('max', tuple(in_ws))
            out_net = rt.new_net('%s.out' % inst_name, out_w)
            node = GateNode(rt, inst_name, table, arity)
            node.outs = [out_net]
            out_net.driver = ('node', node)
            for i, n in enumerate(in_nets):
                node.add_input(NetSrc(n), 'normal', '%s.in.%d' % (inst_name, i))
            inst = Inst(key, inst_name, defn)
            inst.in_nets = in_nets
            inst.out_nets = [out_net]
            inst.node = node
            self.all_insts.append(inst)
            if inst_name is not None:
                ctx.insts[inst_name] = inst
            return inst
        # block def
        if defn.using:
            raise Diag('using is not supported in this implementation', defn.line, defn.file)
        if defn.name in ctx.def_stack:
            raise Diag('component %r instantiates itself (recursion is forbidden)'
                       % defn.name, line)
        if not any(p.direction == 'out' for p in defn.ports):
            raise Diag('block %r requires at least one output' % defn.name, defn.line, defn.file)
        in_nets, out_nets = [], []
        in_map, out_map = {}, {}
        for p in defn.ports:
            w = self.solver.new()
            if p.width.kind == 'const':
                w.expr = self.num_to_wexpr(p.width.expr, key, p.line, defn.file)
            elif p.width.kind == 'param':
                w.expr = ('param', key, p.width.name)
            net = rt.new_net('%s.%s' % (inst_name, p.name), w,
                             boundary=(p.direction == 'in'))
            if p.direction == 'in':
                in_nets.append(net)
                in_map[p.name] = net
            else:
                out_nets.append(net)
                out_map[p.name] = net
        inst = Inst(key, inst_name, defn)
        inst.in_nets = in_nets
        inst.out_nets = out_nets
        self.all_insts.append(inst)
        sub_ctx = Ctx(self, in_map, out_map, ctx.def_stack + [defn.name], inst_key=key)
        if inst_name is not None:
            ctx.insts[inst_name] = inst
        for item in defn.with_items:
            self.instantiate(item.type_name, item.inst_name, sub_ctx, init=item.init,
                             line=item.line)
        self.compile_body(defn.body, sub_ctx)
        return inst

    # ---- body / chain compilation ----

    def compile_body(self, stmts, ctx):
        for st in stmts:
            self.compile_chain(st, ctx)
        for name, lates in ctx.pending_named.items():
            if name not in ctx.named:
                raise Diag('named connection @%s never declared' % name)
            parts = ctx.named[name]
            for late in lates:
                late.parts = parts
                if late.member is not None:
                    if late.member >= len(parts):
                        raise Diag('@%s.%d out of range' % (name, late.member))
                    eff = parts[late.member]
                else:
                    if len(parts) != 1:
                        raise Diag('@%s is a multi-member chain and cannot be used as a single '
                                   'value' % name)
                    eff = parts[0]
                if late.wnode is not None:
                    self.solver.union(late.wnode, eff.wnode)
        for name, net in ctx.outputs.items():
            if net.driver is None:
                raise Diag('output %r is not driven' % name)

    def compile_chain(self, conn, ctx):
        segs = conn.segments
        chain = None
        for i, seg in enumerate(segs):
            last = (i == len(segs) - 1)
            if isinstance(seg, A.NamedSeg):
                if i == 0 or chain is None:
                    chain = self.named_chain_ref(seg.name, ctx, seg.line)
                else:
                    if seg.name in ctx.named:
                        raise Diag('named connection @%s declared twice' % seg.name, seg.line)
                    ctx.named[seg.name] = chain
                    if seg.width is not None:
                        if len(chain) != 1:
                            raise Diag('@%s width annotation requires a single-member chain'
                                       % seg.name, seg.line)
                        if seg.width.kind != 'const':
                            raise Diag('width annotation must be a constant', seg.line)
                        w = self.const_int(seg.width.expr, seg.line, what='width annotation')
                        self.solver.union(chain[0].wnode, self.solver.const_node(Fraction(w)),
                                          seg.line)
                continue
            if isinstance(seg, A.AdapterSeg):
                if chain is None:
                    chain = []
                chain = self.apply_adapter(seg, chain, ctx)
                continue
            if isinstance(seg, A.MergeSeg):
                if not chain:
                    raise Diag('merge needs an incoming chain', seg.line)
                rt = self.rt
                out_w = self.solver.new()
                out = rt.new_net('merge@%d.out' % seg.line, out_w)
                node = MergeNode(rt, 'merge@%d' % seg.line)
                node.outs = [out]
                out.driver = ('node', node)
                for j, src in enumerate(chain):
                    node.add_input(src, 'raw', 'merge@%d.in.%d' % (seg.line, j))
                    self.solver.union(out_w, src.wnode, seg.line)
                chain = [NetSrc(out)]
                continue
            if isinstance(seg, A.FoldSeg):
                if not chain:
                    raise Diag('fold needs an incoming chain', seg.line)
                gdef = self.prog.defs.get(seg.gate_name)
                if not isinstance(gdef, A.GateDef):
                    raise Diag('fold expects a gate, got %r' % seg.gate_name, seg.line)
                arity = self.const_int(gdef.arity, gdef.line, gdef.file, 'gate arity')
                if arity != 2:
                    raise Diag('fold expects a binary gate', seg.line)
                table, warns = build_gate_table(gdef.rows, 2, seg.gate_name, gdef.file, gdef.line)
                self.warnings.extend(warns)
                rt = self.rt
                out_w = self.solver.new()
                out_w.expr = ('max', tuple(s.wnode for s in chain))
                out = rt.new_net('fold@%d.out' % seg.line, out_w)
                node = FoldNode(rt, 'fold@%d' % seg.line, table)
                node.outs = [out]
                out.driver = ('node', node)
                for j, src in enumerate(chain):
                    node.add_input(src, 'normal', 'fold@%d.in.%d' % (seg.line, j))
                chain = [NetSrc(out)]
                continue
            if isinstance(seg, A.BareSeg):
                name = seg.name
                if name in ctx.insts or name in self.prog.defs:
                    inst = ctx.insts.get(name)
                    if inst is None:
                        inst = self.instantiate(name, 'anon%d' % self.inst_counter, ctx,
                                                 line=seg.line)
                    if chain is not None:
                        self.drive_inputs(inst, chain, ctx, seg.line)
                    if last:
                        chain = None
                    else:
                        chain = [NetSrc(n) for n in inst.out_nets]
                    continue
                if name in ctx.inputs:
                    if last:
                        raise Diag('cannot drive input %r' % name, seg.line)
                    chain = [NetSrc(ctx.inputs[name])]
                    continue
                if name in ctx.outputs:
                    if not last:
                        raise Diag('%r is an output port and cannot be a chain source' % name,
                                   seg.line)
                    if chain is None or len(chain) != 1:
                        raise Diag('output %r needs exactly one chain member' % name, seg.line)
                    self.set_driver(ctx.outputs[name], chain[0], name, seg.line)
                    chain = None
                    continue
                raise Diag('unknown name %r' % name, seg.line)
            if isinstance(seg, A.QualSeg):
                net = self.resolve_port_net(seg, ctx)
                if last:
                    if seg.field != 'in':
                        raise Diag('cannot drive %s.%s.%s here' % (seg.name, seg.field, seg.index),
                                   seg.line)
                    if chain is None or len(chain) != 1:
                        raise Diag('port %s.in.%s needs exactly one chain member'
                                   % (seg.name, seg.index), seg.line)
                    self.set_driver(net, chain[0], '%s.in.%s' % (seg.name, seg.index), seg.line)
                    chain = None
                else:
                    chain = [NetSrc(net)]
                continue
            if isinstance(seg, A.ExprSeg):
                srcs = self.compile_expr(seg.expr, ctx)
                if last:
                    raise Diag('an expression cannot terminate a chain (nothing to drive)',
                               seg.line)
                chain = srcs
                continue
            raise Diag('unsupported chain segment', seg.line)

    def named_chain_ref(self, name, ctx, line):
        if name in ctx.named:
            return list(ctx.named[name])
        late = LateSrc(name)
        late.wnode = self.solver.new()
        late.member = None
        ctx.pending_named.setdefault(name, []).append(late)
        return [late]

    def named_member(self, name, member, ctx, line):
        if name in ctx.named:
            parts = ctx.named[name]
            if member is not None:
                if member >= len(parts):
                    raise Diag('@%s.%d out of range' % (name, member), line)
                return parts[member]
            if len(parts) != 1:
                raise Diag('@%s is a multi-member chain; use @%s.N' % (name, name), line)
            return parts[0]
        late = LateSrc(name)
        late.wnode = self.solver.new()
        late.member = member
        ctx.pending_named.setdefault(name, []).append(late)
        return late

    def apply_adapter(self, seg, chain, ctx):
        out = []
        for m in seg.members:
            base = m.base
            if base[0] == 'lit':
                if any(c not in ALL_STATES for c in base[1]):
                    raise Diag('bad literal %r' % base[1], m.line)
                w = self.solver.new()
                w.expr = ('const', Fraction(len(base[1])))
                src = ConstSrc(w, make_literal(base[1]))
            elif base[0] == 'usd':
                n = base[1]
                if n >= len(chain):
                    raise Diag('$%d out of range (chain has %d members)' % (n, len(chain)),
                               m.line)
                src = chain[n]
            elif base[0] == 'named':
                src = self.named_member(base[1], base[2], ctx, m.line)
            elif base[0] == 'ref':
                src = self.resolve_ref(base[1], ctx, m.line)
            else:
                raise Diag('bad adapter member', m.line)
            if m.slice is not None:
                src = self.make_slice(src, m.slice, ctx, m.line)
            out.append(src)
        return out

    def make_slice(self, src, spec, ctx, line):
        if spec.lo is None and spec.hi is not None:
            hi = eval_num(spec.hi, {}, line, what='slice bound')
            if hi == -1:
                s = SliceSrc(src, None, None, rev=True)
                s.wnode = self.solver.new()
                s.wnode.expr = ('wnode', src.wnode)
                s.lo_expr = s.hi_expr = None
                s.inst_key = None
                return s
            raise Diag('only v[:-1] reversal is supported', line)
        lo_e = spec.lo if spec.lo is not None else A.NumLit(Fraction(0), line)
        s = SliceSrc(src, None, None, rev=False)
        s.wnode = self.solver.new()
        lo_w = self.num_to_wexpr(lo_e, ctx.inst_key, line)
        hi_w = self.num_to_wexpr(spec.hi, ctx.inst_key, line) if spec.hi is not None \
            else ('wnode', src.wnode)
        s.wnode.expr = ('bin', '-', hi_w, lo_w)
        s.lo_expr = lo_e
        s.hi_expr = spec.hi
        s.inst_key = ctx.inst_key
        return s

    def resolve_port_net(self, seg, ctx):
        inst = ctx.insts.get(seg.name)
        if inst is None:
            raise Diag('unknown instance %r' % seg.name, seg.line)
        if seg.field == 'out':
            idx = seg.index if seg.index is not None else 0
            if seg.index is None and len(inst.out_nets) != 1:
                raise Diag('%s.out shorthand requires a single output' % seg.name, seg.line)
            if idx >= len(inst.out_nets):
                raise Diag('%s.out.%d out of range' % (seg.name, idx), seg.line)
            return inst.out_nets[idx]
        else:
            if seg.index is None:
                raise Diag('%s.in needs an index' % seg.name, seg.line)
            if seg.index >= len(inst.in_nets):
                raise Diag('%s.in.%d out of range' % (seg.name, seg.index), seg.line)
            return inst.in_nets[seg.index]

    def drive_inputs(self, inst, chain, ctx, line):
        if chain is None:
            chain = []
        if len(chain) != len(inst.in_nets):
            raise Diag('%s expects %d input(s), chain has %d member(s)'
                       % (inst.name, len(inst.in_nets), len(chain)), line)
        for net, src in zip(inst.in_nets, chain):
            self.set_driver(net, src, '%s input port' % inst.name, line)

    def set_driver(self, net, src, what, line):
        if net.driver is not None:
            raise Diag('multiple drivers on %s: short circuit is a compile error' % what, line)
        net.driver = ('src', src)
        root = wfind(net.wnode)
        e = self.solver.subst(root.expr)
        if e is not None and e[0] == 'const':
            # fixed-width destination: the source may be shorter (zero-extend),
            # a free source variable is anchored by the declared port
            self.solver.le.append((src.wnode, e[1], what, line))
            sroot = wfind(src.wnode)
            if self.solver.subst(sroot.expr) is None:
                sroot.expr = ('const', e[1])
        else:
            self.solver.union(net.wnode, src.wnode, line)

    # ---- expressions ----

    def compile_expr(self, e, ctx):
        if isinstance(e, A.LitExpr):
            if any(c not in ALL_STATES for c in e.text):
                raise Diag('bad literal %r' % e.text, e.line)
            w = self.solver.new()
            w.expr = ('const', Fraction(len(e.text)))
            src = ConstSrc(w, make_literal(e.text))
            if e.slice is not None:
                src = self.make_slice(src, e.slice, ctx, e.line)
            return [src]
        if isinstance(e, A.RefExpr):
            src = self.resolve_ref(e.ref, ctx, e.line)
            if e.slice is not None:
                src = self.make_slice(src, e.slice, ctx, e.line)
            return [src]
        if isinstance(e, A.ConcatExpr):
            parts = []
            for p in e.parts:
                ps = self.compile_expr(p, ctx)
                if len(ps) != 1:
                    raise Diag('concatenation parts must be single vectors', p.line)
                parts.append(ps[0])
            if len(parts) == 1:
                return parts
            w = self.solver.new()
            w.expr = ('sum', tuple(p.wnode for p in parts))
            src = ConcatSrc(parts)
            src.wnode = w
            return [src]
        if isinstance(e, A.CallExpr):
            defn = self.prog.defs.get(e.name)
            if defn is None:
                raise Diag('unknown component %r' % e.name, e.line)
            cls = self.classify(e.name)
            if cls != 'combinational':
                raise Diag('inline call of %r requires a combinational component (class: %s)'
                           % (e.name, cls), e.line)
            self.inst_counter += 1
            inst = self.instantiate(e.name, 'call%d' % self.inst_counter, ctx, line=e.line)
            args = []
            for a in e.args:
                ps = self.compile_expr(a, ctx)
                if len(ps) != 1:
                    raise Diag('call arguments must be single vectors', a.line)
                args.append(ps[0])
            self.drive_inputs(inst, args, ctx, e.line)
            if len(inst.out_nets) != 1:
                raise Diag('called component must have exactly one output', e.line)
            return [NetSrc(inst.out_nets[0])]
        if isinstance(e, A.MatchExpr):
            sel = self.compile_expr(e.selector, ctx)
            if len(sel) != 1:
                raise Diag('match selector must be a single vector', e.line)
            rt = self.rt
            out_w = self.solver.new()
            out = rt.new_net('match@%d.out' % e.line, out_w)
            node = MatchNode(rt, 'match@%d' % e.line, [], [])
            node.outs = [out]
            out.driver = ('node', node)
            node.add_input(sel[0], 'normal', 'match@%d.sel' % e.line)
            patterns = []
            arms = []
            for pat, arm_expr in e.arms:
                if sum(1 for it in pat if it[0] == 'seq') > 1:
                    raise Diag('at most one _ per pattern', e.line)
                for it in pat:
                    if it[0] == 'lit' and it[1] not in ALL_STATES:
                        raise Diag('bad pattern state %r' % it[1], e.line)
                    if it[0] == 'lit' and it[1] == Z:
                        raise Diag('z is not a pattern: selectors never yield z', e.line)
                patterns.append(pat)
                arms_src = self.compile_expr(arm_expr, ctx)
                if len(arms_src) != 1:
                    raise Diag('match arm must produce a single vector', arm_expr.line)
                arms.append(arms_src[0])
                self.solver.union(out_w, arms_src[0].wnode, e.line)
            if not patterns or patterns[-1] != [('seq',)]:
                raise Diag('match requires a final _ arm', e.line)
            node.patterns = patterns
            node.arms = arms
            return [NetSrc(out)]
        raise Diag('unsupported expression', getattr(e, 'line', 0))

    def resolve_ref(self, ref, ctx, line):
        if ref.kind == 'bare':
            if ref.name in ctx.inputs:
                return NetSrc(ctx.inputs[ref.name])
            if ref.name in ctx.outputs:
                return NetSrc(ctx.outputs[ref.name])
            if ref.name in ctx.insts:
                raise Diag('instance %r is not a value; use %s.out' % (ref.name, ref.name), line)
            raise Diag('unknown name %r' % ref.name, line)
        if ref.kind == 'qual':
            class S:
                pass
            seg = S()
            seg.name = ref.name
            seg.field = ref.field
            seg.index = ref.index
            seg.line = line
            return NetSrc(self.resolve_port_net(seg, ctx))
        if ref.kind == 'named':
            return self.named_member(ref.name, ref.member, ctx, line)
        raise Diag('bad reference', line)

    # ---- classification ----

    def classify(self, name):
        if name in self.class_memo:
            return self.class_memo[name]
        defn = self.prog.defs[name]
        if isinstance(defn, A.GateDef):
            self.class_memo[name] = 'combinational'
            return 'combinational'
        if name in self._classifying:
            raise Diag('component %r instantiates itself transitively' % name)
        self._classifying.append(name)
        saved_rt = self.rt
        scratch = Runtime()
        self.rt = scratch
        try:
            ctx = Ctx(self, {}, {}, [])
            self.instantiate(name, 'scratch', ctx)
        finally:
            self.rt = saved_rt
            self._classifying.pop()
        nodes = set(n.nid for n in scratch.nodes)
        has_reg = any(n.kind == 'register' for n in scratch.nodes)
        arcs = {n.nid: set() for n in scratch.nodes}
        for node in scratch.nodes:
            srcs = [s for s, _, _ in node.input_specs]
            if isinstance(node, MatchNode):
                srcs.extend(node.arms)
            for src in srcs:
                for dn in self._driver_nodes(src, scratch):
                    if dn.nid in nodes:
                        arcs[node.nid].add(dn.nid)
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {nid: WHITE for nid in nodes}

        def dfs(u):
            color[u] = GRAY
            for v in arcs[u]:
                if color[v] == GRAY:
                    return True
                if color[v] == WHITE and dfs(v):
                    return True
            color[u] = BLACK
            return False

        cyclic = any(color[n] == WHITE and dfs(n) for n in nodes)
        if has_reg:
            cls = 'clocked+feedback' if cyclic else 'clocked'
        elif cyclic:
            cls = 'feedback'
        else:
            cls = 'combinational'
        self.class_memo[name] = cls
        return cls

    def _driver_nodes(self, src, rt):
        if isinstance(src, NetSrc):
            net = src.net
            d = net.driver
            if d is None:
                return []
            if d[0] == 'node':
                node = d[1]
                if node.kind == 'register':
                    return []
                return [node]
            return self._driver_nodes(d[1], rt)
        if isinstance(src, SliceSrc):
            return self._driver_nodes(src.inner, rt)
        if isinstance(src, ConcatSrc):
            out = []
            for p in src.parts:
                out.extend(self._driver_nodes(p, rt))
            return out
        if isinstance(src, LateSrc):
            out = []
            for p in (src.parts or []):
                out.extend(self._driver_nodes(p, rt))
            return out
        return []

    # ---- entrypoints ----

    def build_entry(self, with_items, body, script_inputs=None):
        rt = Runtime()
        self.rt = rt
        self.solver = WSolver()
        self.all_insts = []
        ctx = Ctx(self, {}, {}, [])
        for name, idef in self.prog.inputs.items():
            w = self.solver.new()
            if idef.init is not None:
                if any(c not in ALL_STATES for c in idef.init):
                    raise Diag('bad input initializer %r' % idef.init, idef.line, idef.file)
                val = make_literal(idef.init)
                w.expr = ('const', Fraction(len(idef.init)))
            else:
                val = Value((ZERO,))
                w.expr = ('const', Fraction(1))
            net = rt.new_net('input.%s' % name, w)
            net.value = val
            rt.input_by_name[name] = net
            rt.input_by_key[idef.key] = net
            ctx.inputs[name] = net
        for key, (name, width) in (script_inputs or {}).items():
            w = self.solver.new()
            w.expr = ('const', Fraction(width))
            net = rt.new_net('script.%s' % name, w)
            net.value = Value((ZERO,) * width)
            rt.input_by_name[name] = net
            rt.input_by_key[key] = net
            ctx.inputs[name] = net
        for name, odef in self.prog.outputs.items():
            w = self.solver.new()
            net = rt.new_net('output.%s' % name, w)
            ctx.outputs[name] = net
            rt.out_endpoints.append(Endpoint(odef.label, NetSrc(net), odef.mode))
        for name, cdef in self.prog.clocks.items():
            period = self.const_int(cdef.period, cdef.line, cdef.file, 'clock period')
            if period < 1:
                raise Diag('clock period must be a positive integer number of ticks',
                           cdef.line, cdef.file)
            duty = eval_num(cdef.duty, {}, cdef.line, cdef.file, 'clock duty')
            if not (0 < duty < 100):
                raise Diag('clock duty must be strictly between 0 and 100', cdef.line, cdef.file)
            h = period * duty / 100
            if h.denominator != 1 or not (0 < h < period):
                raise Diag('clock duty must give an integral high portion strictly inside '
                           'the period (got %s ticks high of %s)' % (h, period),
                           cdef.line, cdef.file)
            w = self.solver.new()
            w.expr = ('const', Fraction(1))
            net = rt.new_net('clock.%s' % name, w)
            net.value = Value((NEG,))
            rt.add_clock(net, period, duty)
            ctx.inputs[name] = net
        for item in with_items:
            self.instantiate(item.type_name, item.inst_name, ctx, init=item.init,
                             line=item.line)
        self.compile_body(body, ctx)
        self.finalize(rt)
        return rt

    def finalize(self, rt):
        def fix(src):
            if isinstance(src, LateSrc):
                if src.parts is None:
                    raise Diag('named connection @%s never declared' % src.name)
                if src.member is not None:
                    return src.parts[src.member]
                if len(src.parts) != 1:
                    raise Diag('@%s is a multi-member chain used as a single value' % src.name)
                return src.parts[0]
            if isinstance(src, SliceSrc):
                src.inner = fix(src.inner)
                return src
            if isinstance(src, ConcatSrc):
                src.parts = [fix(p) for p in src.parts]
                return src
            return src
        for net in rt.nets:
            if net.driver is not None and net.driver[0] == 'src':
                net.driver = ('src', fix(net.driver[1]))
        for node in rt.nodes:
            node.input_specs = [(fix(s), k, l) for s, k, l in node.input_specs]
        self.solver.solve()
        le_by_root = {}
        for wnode, c, what, line in self.solver.le:
            le_by_root.setdefault(id(wfind(wnode)), []).append((c, what, line))
        for net in rt.nets:
            r = wfind(net.wnode)
            v = r.value
            if v is None and id(r) in le_by_root:
                v = min(c for c, _, _ in le_by_root[id(r)])
                r.value = v
            if v is None:
                raise Diag('unresolved width for %s (unanchored cycle)' % net.name)
            if v.denominator != 1 or v <= 0:
                raise Diag('width of %s must be a positive integer, got %s' % (net.name, v))
            net.width = int(v)
        for wnode, c, what, line in self.solver.le:
            v = wfind(wnode).value
            if v is None:
                raise Diag('unresolved width for %s' % what, line)
            if v > c:
                raise Diag('value of width %d too wide for %s of width %d; slice explicitly'
                           % (int(v), what, int(c)), line)
        for node in rt.nodes:
            for src, kind, label in node.input_specs:
                self._finalize_src(src)
        for net in rt.nets:
            if net.driver is not None and net.driver[0] == 'src':
                self._finalize_src(net.driver[1])
        for inst in self.all_insts:
            for net in inst.in_nets:
                if net.driver is None:
                    raise Diag('input port of %s is not driven' % inst.name)
        for node in rt.nodes:
            if node.kind == 'register':
                src = node.input_specs[1][0]
                if not isinstance(src, NetSrc) or self._trace_clock(src.net) is None:
                    raise Diag('register %s: clk input is not a transparent route to a clock '
                               'generator%s' % (node.name, self._clock_breaker(src)))
                node.clk_net = src.net
        rt.finalize_reads()
        rt.initialize([])

    def _finalize_src(self, src, seen=None):
        if seen is None:
            seen = set()
        if id(src) in seen:
            return
        seen.add(id(src))
        if isinstance(src, SliceSrc):
            self._finalize_src(src.inner, seen)
            if src.rev:
                src.width = src.inner.width
                return
            env = {}
            if src.inst_key is not None:
                for (k, n), e in self.solver.bindings.items():
                    if k == src.inst_key:
                        v = self.solver.eval(e)
                        if v is not None:
                            env[n] = v
            lo = int(eval_num(src.lo_expr, env, what='slice bound'))
            hi = int(eval_num(src.hi_expr, env, what='slice bound')) if src.hi_expr is not None \
                else src.inner.width
            if not (0 <= lo < hi <= src.inner.width):
                raise Diag('invalid slice [%s:%s] on width %d' % (lo, hi, src.inner.width))
            src.lo, src.hi = lo, hi
            src.width = hi - lo
        elif isinstance(src, ConcatSrc):
            for p in src.parts:
                self._finalize_src(p, seen)
            src.width = sum(p.width for p in src.parts)
        elif isinstance(src, NetSrc):
            src.width = src.net.width
        elif isinstance(src, ConstSrc):
            src.width = len(src.value.trits)
        elif isinstance(src, LateSrc):
            for p in (src.parts or []):
                self._finalize_src(p, seen)
            src.width = src.parts[0].width if src.parts else None

    def _trace_clock(self, net, seen=None):
        if seen is None:
            seen = set()
        if net.nid in seen:
            return None
        seen.add(net.nid)
        if net.is_clock:
            return net
        d = net.driver
        if d is None:
            return None
        if d[0] == 'node':
            return None
        src = d[1]
        if isinstance(src, NetSrc):
            return self._trace_clock(src.net, seen)
        return None

    def _clock_breaker(self, src):
        seen = set()
        while True:
            if isinstance(src, NetSrc):
                net = src.net
                if net.nid in seen:
                    return ''
                seen.add(net.nid)
                d = net.driver
                if d is None:
                    return ' (stops at %s)' % net.name
                if d[0] == 'node':
                    return ' (broken by computation %r)' % d[1].name
                src = d[1]
            else:
                return ' (broken by non-transparent routing)'

    # ---- table tests ----

    def build_table_row(self, test_def, row):
        tdef = self.prog.defs.get(test_def.target)
        if not isinstance(tdef, A.BlockDef):
            raise Diag('table test target %r must be a block' % test_def.target, test_def.line)
        cls = self.classify(test_def.target)
        if cls != 'combinational':
            raise Diag('table tests target combinational blocks only (%r is %s)'
                       % (test_def.target, cls), test_def.line)
        rt = Runtime()
        self.rt = rt
        self.solver = WSolver()
        self.all_insts = []
        ctx = Ctx(self, {}, {}, [])
        inst = self.instantiate(tdef.name, 'dut', ctx, line=test_def.line)
        if len(row.inputs) != 1:
            raise Diag('table row input must be one chain', row.line)
        in_srcs = self.row_chain(row.inputs[0], ctx)
        self.drive_inputs(inst, in_srcs, ctx, row.line)
        if len(row.outputs) != 1:
            raise Diag('table row output must be one chain', row.line)
        exp_srcs = self.row_chain(row.outputs[0], ctx)
        if len(exp_srcs) != len(inst.out_nets):
            raise Diag('row expects %d output(s), block has %d'
                       % (len(exp_srcs), len(inst.out_nets)), row.line)
        self.compile_body([], ctx)
        self.finalize(rt)
        rt.run_until_idle(100000)
        if rt.cur_queue:
            raise Diag('combinational network did not reach idle (compiler bug)')
        actual = [NetSrc(net).read(rt, 'normal').trits for net in inst.out_nets]
        expected = [s.read(rt, 'normal').trits for s in exp_srcs]
        return actual, expected

    def row_chain(self, seg, ctx):
        if isinstance(seg, A.AdapterSeg):
            return self.apply_adapter(seg, [], ctx)
        if isinstance(seg, A.ExprSeg):
            return self.compile_expr(seg.expr, ctx)
        raise Diag('table row chains must be adapters or expressions', seg.line)

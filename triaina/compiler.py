"""Compiler: loads files/imports/namespaces, resolves names, infers widths,
classifies blocks, traces clock origins, and builds the runtime graph.

Graph model: every net owns a connection. Driving a destination with a chain
member appends the member as a contributor of the destination's connection
(wiring statements sharing an endpoint join the participating connections),
so a connection may have multiple output contributors resolved trit-wise.
Block boundaries forward connections and preserve their attached drivers.
"""

import os
from fractions import Fraction
from itertools import product

from . import ast_nodes as A
from .parser import parse_file
from .runtime import (Runtime, NetSrc, ConstSrc, SliceSrc, ConcatSrc, LateSrc,
                      GateNode, RegisterNode, Endpoint)
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


# ---- width solver ----

def _free_params(e, out=None):
    if out is None:
        out = set()
    if e is None:
        return out
    if isinstance(e, WNode):
        return _free_params(e.expr, out)
    if e[0] == 'param':
        out.add((e[1], e[2]))
    elif e[0] == 'wnode':
        _free_params(e[1].expr, out)
    elif e[0] == 'un':
        _free_params(e[2], out)
    elif e[0] == 'bin':
        _free_params(e[2], out)
        _free_params(e[3], out)
    elif e[0] in ('sum', 'max'):
        for x in e[1]:
            _free_params(x, out)
    return out


class WNode:
    __slots__ = ('expr', 'value')

    def __init__(self):
        self.expr = None
        self.value = None


class WSolver:
    """Width expressions over exact rationals. A wnode is either anchored by
    an expr (const/param/expression tree) or free; free wnodes take the
    maximum of the lower bounds imposed by driver->destination constraints
    (a shorter vector zero-extends on the left at a wider destination)."""

    def __init__(self):
        self.track = []
        self.bindings = {}  # (inst_key, param name) -> expr
        self.le = []        # (src wnode, dest wnode, what, line)
        self.eq = []        # (expr, expr, line)

    def new(self):
        w = WNode()
        self.track.append(w)
        return w

    def constrain_eq(self, w, expr, line=0):
        if w.expr is None:
            w.expr = expr
            return
        if w.expr == expr:
            return
        for e, other in ((w.expr, expr), (expr, w.expr)):
            if e[0] == 'param':
                key = (e[1], e[2])
                if key not in self.bindings and key not in _free_params(other):
                    self.bindings[key] = other
                    return
        self.eq.append((w.expr, expr, line))

    def add_le(self, src, dest, what, line):
        self.le.append((src, dest, what, line))

    def eval(self, e, seen=None):
        if e is None:
            return None
        if isinstance(e, WNode):
            if e.value is not None:
                return e.value
            if seen is None:
                seen = set()
            if id(e) in seen:
                return None
            seen.add(id(e))
            return self.eval(e.expr, seen)
        if seen is None:
            seen = set()
        if e[0] == 'const':
            return e[1]
        if e[0] == 'param':
            key = (e[1], e[2])
            if key in self.bindings:
                return self.eval(self.bindings[key], seen)
            return None
        if e[0] == 'wnode':
            w = e[1]
            if w.value is not None:
                return w.value
            if id(w) in seen:
                return None
            seen.add(id(w))
            return self.eval(w.expr, seen)
        if e[0] == 'un':
            v = self.eval(e[2], seen)
            return None if v is None else (-v if e[1] == '-' else v)
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
        lower = {}
        for src, dest, what, line in self.le:
            lower.setdefault(id(dest), []).append(src)
        for _ in range(len(self.track) * 3 + 8):
            progressed = False
            for w in self.track:
                v = self.eval(w.expr) if w.expr is not None else None
                if v is None:
                    lbs = [self.eval(s) for s in lower.get(id(w), [])]
                    lbs = [x for x in lbs if x is not None]
                    if lbs:
                        v = max(lbs)
                        if w.expr is not None and w.expr[0] == 'param':
                            key = (w.expr[1], w.expr[2])
                            if key not in self.bindings:
                                self.bindings[key] = ('const', v)
                                progressed = True
                if v is not None and v != w.value:
                    w.value = v
                    progressed = True
            if not progressed:
                break
        for e1, e2, line in self.eq:
            v1, v2 = self.eval(e1), self.eval(e2)
            if v1 is not None and v2 is not None and v1 != v2:
                raise Diag('conflicting widths %s and %s' % (v1, v2), line)
        for src, dest, what, line in self.le:
            sv = self.eval(src)
            dv = self.eval(dest)
            if sv is None or dv is None:
                raise Diag('unresolved width for %s' % what, line)
            if sv > dv:
                raise Diag('width %d of %s exceeds destination width %d; '
                           'select explicitly with a slice' % (sv, what, dv), line)
        for w in self.track:
            v = self.eval(w.expr) if w.expr is not None else w.value
            if v is None:
                raise Diag('unresolved width (no constraint anchors it)')
            if v.denominator != 1 or v <= 0:
                raise Diag('width must be a positive integer, got %s' % v)
            w.value = v


# ---- program loading: files, imports, namespaces ----

class FileInfo:
    def __init__(self, path, fid):
        self.path = path
        self.fid = fid
        self.defs = {}      # qname tuple -> GateDef|BlockDef
        self.imports = []   # direct import file ids (names are not re-exported)
        self.benches = []
        self.tests = []


class Program:
    def __init__(self, main_path):
        self.main_path = os.path.normpath(main_path)
        self.files = {}
        self.order = []
        self._load(self.main_path, [], set())
        self.benches = [(fi, b) for fi in self.order for b in fi.benches]
        self.tests = [(fi, t) for fi in self.order for t in fi.tests]

    def _load(self, path, stack, done):
        if path in stack:
            raise Diag('import cycle: %s' % ' -> '.join(stack + [path]))
        if path in done:
            return
        done.add(path)
        defs = parse_file(path)
        fi = FileInfo(path, len(self.order))
        self.files[path] = fi
        self.order.append(fi)
        stack.append(path)
        imports = []

        def walk(items, ns):
            for d in items:
                if isinstance(d, A.NamespaceDef):
                    walk(d.defs, ns + tuple(d.parts))
                    continue
                if isinstance(d, A.Import):
                    imports.append(d)
                    continue
                qname = ns + (d.name,)
                d.qname = qname
                d.ns = ns
                d.file = path
                d.file_id = fi.fid
                if isinstance(d, (A.GateDef, A.BlockDef)):
                    if qname in fi.defs:
                        raise Diag('duplicate qualified definition %r'
                                   % '::'.join(qname), d.line, path)
                    fi.defs[qname] = d
                elif isinstance(d, A.BenchDef):
                    fi.benches.append(d)
                elif isinstance(d, A.TestDef):
                    fi.tests.append(d)
                else:
                    raise Diag('unexpected definition %r' % type(d).__name__,
                               d.line, path)

        walk(defs, ())
        for d in imports:
            sub = os.path.normpath(os.path.join(os.path.dirname(path), d.path))
            self._load(sub, stack, done)
            fi.imports.append(self.files[sub].fid)
        stack.pop()

    def scope(self, fid):
        """Files whose declarations are visible from file `fid`: the file
        itself plus its direct imports (imports are not re-exported)."""
        fi = self.order[fid]
        return [fi] + [self.order[i] for i in fi.imports]

    def lookup_def(self, parts, absolute, ns, fid, line=0, file=None):
        parts = tuple(parts)
        if absolute:
            cands = [parts]
        else:
            cands = [tuple(ns[:i]) + parts for i in range(len(ns), -1, -1)]
        for cand in cands:
            found = [f.defs[cand] for f in self.scope(fid) if cand in f.defs]
            if len(found) > 1:
                raise Diag('ambiguous reference %r' % '::'.join(cand), line, file)
            if found:
                return found[0]
        raise Diag('unknown name %r' % '::'.join(parts), line, file)

    def find_bench(self, name):
        matches = [b for _, b in self.benches
                   if b.name == name or '::'.join(b.qname) == name]
        if not matches:
            raise Diag('no bench named %r (available: %s)'
                       % (name, ', '.join(b.name for _, b in self.benches) or 'none'))
        if len(matches) > 1:
            raise Diag('ambiguous bench name %r' % name)
        return matches[0]

    def find_test(self, name):
        matches = [t for _, t in self.tests
                   if t.name == name or '::'.join(t.qname) == name]
        if len(matches) > 1:
            raise Diag('ambiguous test name %r' % name)
        return matches[0] if matches else None


# ---- gate tables ----

def build_gate_table(rows, arity, name, file=None, line=0):
    """Rows apply in source order; wildcard rows assign still-unassigned
    combinations; explicit rows must identify an unassigned combination;
    every known input combination must be covered."""
    assigned = {}
    warnings = []
    for pat, res in rows:
        if len(pat) != arity:
            raise Diag('gate %s: row has %d positions, arity is %d'
                       % (name, len(pat), arity), line, file)
        if not any(it[0] == 'any' for it in pat):
            tup = tuple(it[1] for it in pat)
            if tup in assigned:
                raise Diag('gate %s: explicit row %s designates an already assigned '
                           'combination' % (name, tup), line, file)
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
        raise Diag('gate %s: truth table does not cover all %d combinations '
                   '(missing %d, e.g. %s)' % (name, total, len(missing), missing[0]),
                   line, file)
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
    def __init__(self, comp, file_id, ns, def_stack, inst_key=None):
        self.comp = comp
        self.file_id = file_id
        self.ns = tuple(ns)
        self.def_stack = def_stack
        self.inst_key = inst_key
        self.ports_in = {}       # block input ports: name -> Net
        self.ports_out = {}      # block output ports: name -> Net
        self.ep_in = {}          # bench/test input & clock endpoints: name -> Net
        self.ep_out = {}         # bench/test output endpoints: name -> Net
        self.insts = {}
        self.named = {}
        self.pending_named = {}


def wnode_of(src):
    return src.wnode


def ctx_file(ctx):
    return ctx.comp.prog.order[ctx.file_id].path if ctx.file_id is not None else None


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

    def instantiate(self, parts, absolute, inst_name, ctx, init=None, line=0):
        self.inst_counter += 1
        key = self.inst_counter
        rt = self.rt
        if parts == ['register'] and not absolute:
            if init is None:
                raise Diag('register needs an initializer', line)
            if not init or any(c not in ALL_STATES for c in init):
                raise Diag('bad register initializer %r' % init, line)
            val = make_literal(init, tick=-1)
            data_w = self.solver.new()
            clk_w = self.solver.new()
            out_w = self.solver.new()
            out_w.expr = ('const', Fraction(len(init)))
            data_net = rt.new_net('%s.in.0' % inst_name, data_w)
            clk_net = rt.new_net('%s.in.1' % inst_name, clk_w)
            out_net = rt.new_net('%s.out' % inst_name, out_w)
            node = RegisterNode(rt, inst_name, val)
            node.outs = [out_net]
            out_net.conn.contributors.append(('node', node, 0, '%s.out' % inst_name))
            node.add_input(NetSrc(data_net), '%s.in.0' % inst_name)
            node.add_input(NetSrc(clk_net), '%s.in.1' % inst_name)
            inst = Inst(key, inst_name, None)
            inst.in_nets = [data_net, clk_net]
            inst.out_nets = [out_net]
            inst.node = node
            self.all_insts.append(inst)
            if inst_name is not None:
                ctx.insts[inst_name] = inst
            return inst
        defn = self.prog.lookup_def(parts, absolute, ctx.ns, ctx.file_id, line,
                                     ctx_file(ctx))
        qname = '::'.join(defn.qname)
        if isinstance(defn, A.GateDef):
            arity = self.const_int(defn.arity, defn.line, defn.file, 'gate arity')
            if arity < 1:
                raise Diag('gate arity must be positive', defn.line, defn.file)
            table, warns = build_gate_table(defn.rows, arity, qname, defn.file, defn.line)
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
            out_net.conn.contributors.append(('node', node, 0, '%s.out' % inst_name))
            for i, n in enumerate(in_nets):
                node.add_input(NetSrc(n), '%s.in.%d' % (inst_name, i))
            inst = Inst(key, inst_name, defn)
            inst.in_nets = in_nets
            inst.out_nets = [out_net]
            inst.node = node
            self.all_insts.append(inst)
            if inst_name is not None:
                ctx.insts[inst_name] = inst
            return inst
        # block definition
        if defn in ctx.def_stack:
            raise Diag('component %r instantiates itself (recursion is forbidden)'
                       % qname, line)
        in_nets, out_nets = [], []
        in_map, out_map = {}, {}
        for p in defn.ports:
            w = self.solver.new()
            if p.width.kind == 'const':
                w.expr = self.num_to_wexpr(p.width.expr, key, p.line, defn.file)
            elif p.width.kind == 'param':
                w.expr = ('param', key, p.width.name)
            elif p.width.kind == 'tuple':
                raise Diag('tuple width annotation is not valid on a port',
                           p.line, defn.file)
            net = rt.new_net('%s.%s' % (inst_name, p.name), w)
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
        if inst_name is not None:
            ctx.insts[inst_name] = inst
        sub_ctx = Ctx(self, defn.file_id, defn.ns, ctx.def_stack + [defn], inst_key=key)
        sub_ctx.ports_in = in_map
        sub_ctx.ports_out = out_map
        for item in defn.with_items:
            self.instantiate(item.type_parts, item.absolute, item.inst_name, sub_ctx,
                             init=item.init, line=item.line)
        self.compile_body(defn.body, sub_ctx)
        return inst

    # ---- body / chain compilation ----

    def compile_body(self, stmts, ctx):
        for st in stmts:
            if isinstance(st, A.Connection):
                self.compile_chain(st, ctx)
            else:
                raise Diag('%s declarations are only allowed in bench/test bodies'
                           % type(st).__name__, st.line, ctx_file(ctx))
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
                        raise Diag('@%s is a multi-member chain used as a single value'
                                   % name)
                    eff = parts[0]
                if late.wnode is not None:
                    self.solver.constrain_eq(late.wnode, ('wnode', wnode_of(eff)))

    def compile_chain(self, conn, ctx):
        segs = conn.segments
        chain = None
        for i, seg in enumerate(segs):
            last = (i == len(segs) - 1)
            if isinstance(seg, A.NamedSeg):
                if i == 0 or chain is None:
                    if last:
                        raise Diag('a chain must end at a driven port or interface',
                                   seg.line)
                    chain = self.named_chain_ref(seg.name, ctx, seg.line)
                else:
                    if seg.name in ctx.named:
                        raise Diag('named connection @%s declared twice' % seg.name,
                                   seg.line)
                    if not chain:
                        raise Diag('@%s labels an empty chain' % seg.name, seg.line)
                    ctx.named[seg.name] = list(chain)
                    if seg.widths is not None:
                        self.annotate(seg.name, seg.widths, chain, ctx, seg.line)
                continue
            if isinstance(seg, A.AdapterSeg):
                if last:
                    raise Diag('a chain must end at a driven port or interface',
                               seg.line)
                chain = self.apply_adapter(seg, chain or [], ctx)
                continue
            if isinstance(seg, A.ExprSeg):
                if last:
                    raise Diag('an expression cannot terminate a chain (nothing to '
                               'drive)', seg.line)
                chain = self.compile_expr(seg.expr, ctx)
                continue
            if isinstance(seg, A.QNameSeg):
                inst = self.instantiate(seg.parts, seg.absolute,
                                        'anon%d' % self.inst_counter, ctx, line=seg.line)
                if chain is not None:
                    self.drive_inputs(inst, chain, ctx, seg.line)
                chain = None if last else [NetSrc(n) for n in inst.out_nets]
                continue
            if isinstance(seg, A.QualSeg):
                net = self.resolve_port_net(seg, ctx)
                if last:
                    if seg.field != 'in':
                        raise Diag('cannot drive %s.%s.%s here'
                                   % (seg.name, seg.field, seg.index), seg.line)
                    if chain is None or len(chain) != 1:
                        raise Diag('port %s.in.%s needs exactly one chain member'
                                   % (seg.name, seg.index), seg.line)
                    self.drive(net, chain[0], '%s.in.%s' % (seg.name, seg.index), seg.line)
                    chain = None
                else:
                    chain = [NetSrc(net)]
                continue
            if isinstance(seg, A.BareSeg):
                name = seg.name
                if name in ctx.ports_in or name in ctx.ep_in:
                    net = ctx.ports_in.get(name) or ctx.ep_in[name]
                    if last:
                        raise Diag('cannot drive input %r' % name, seg.line)
                    chain = [NetSrc(net)]
                    continue
                if name in ctx.ports_out or name in ctx.ep_out:
                    net = ctx.ports_out.get(name) or ctx.ep_out[name]
                    if not last:
                        raise Diag('%r is an output port and cannot be a chain source'
                                   % name, seg.line)
                    if chain is None or len(chain) != 1:
                        raise Diag('output %r needs exactly one chain member' % name,
                                   seg.line)
                    self.drive(net, chain[0], name, seg.line)
                    chain = None
                    continue
                if name in ctx.insts:
                    inst = ctx.insts[name]
                    if chain is not None:
                        self.drive_inputs(inst, chain, ctx, seg.line)
                    chain = None if last else [NetSrc(n) for n in inst.out_nets]
                    continue
                inst = self.instantiate([name], False, 'anon%d' % self.inst_counter,
                                        ctx, line=seg.line)
                if chain is not None:
                    self.drive_inputs(inst, chain, ctx, seg.line)
                chain = None if last else [NetSrc(n) for n in inst.out_nets]
                continue
            raise Diag('unsupported chain segment', seg.line)

    def annotate(self, name, widths, chain, ctx, line):
        if widths.kind == 'tuple':
            if len(widths.items) != len(chain):
                raise Diag('@%s annotation describes %d members, chain has %d'
                           % (name, len(widths.items), len(chain)), line)
            for item, src in zip(widths.items, chain):
                self.annotate_one(src, item, ctx, line)
        else:
            if len(chain) != 1:
                raise Diag('@%s annotation describes one member, chain has %d'
                           % (name, len(chain)), line)
            self.annotate_one(chain[0], widths, ctx, line)

    def annotate_one(self, src, ws, ctx, line):
        if ws.kind == 'const':
            self.solver.constrain_eq(src.wnode,
                                     self.num_to_wexpr(ws.expr, ctx.inst_key, line), line)
        elif ws.kind == 'param':
            self.solver.constrain_eq(src.wnode,
                                     ('param', ctx.inst_key, ws.name), line)
        elif ws.kind == 'inferred':
            pass
        else:
            raise Diag('bad width annotation', line)

    # ---- named connections ----

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

    # ---- adapters ----

    def apply_adapter(self, seg, chain, ctx):
        out = []
        for m in seg.members:
            base = m.base
            if base[0] == 'lit':
                if any(c not in ALL_STATES for c in base[1]):
                    raise Diag('bad literal %r' % base[1], m.line)
                w = self.solver.new()
                w.expr = ('const', Fraction(len(base[1])))
                src = ConstSrc(w, make_literal(base[1], tick=-1))
            elif base[0] == 'usd':
                n = base[1]
                if n >= len(chain):
                    raise Diag('$%d out of range (chain has %d members)'
                               % (n, len(chain)), m.line)
                src = chain[n]
            elif base[0] == 'named':
                name, member = base[1], base[2]
                if member is not None:
                    src = self.named_member(name, member, ctx, m.line)
                elif name in ctx.named and m.slice is None:
                    parts = ctx.named[name]
                    if len(parts) == 1:
                        src = parts[0]
                    else:
                        out.extend(parts)  # splice the whole named chain
                        continue
                else:
                    src = self.named_member(name, None, ctx, m.line)
            elif base[0] == 'ref':
                src = self.resolve_ref(base[1], ctx, m.line)
            else:
                raise Diag('bad adapter member', m.line)
            if m.slice is not None:
                src = self.make_slice(src, m.slice, ctx, m.line)
            out.append(src)
        return out

    # ---- expressions ----

    def compile_expr(self, e, ctx):
        if isinstance(e, A.LitExpr):
            if any(c not in ALL_STATES for c in e.text):
                raise Diag('bad literal %r' % e.text, e.line)
            w = self.solver.new()
            w.expr = ('const', Fraction(len(e.text)))
            src = ConstSrc(w, make_literal(e.text, tick=-1))
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
            w.expr = ('sum', tuple(wnode_of(p) for p in parts))
            src = ConcatSrc(parts)
            src.wnode = w
            return [src]
        raise Diag('unsupported expression', getattr(e, 'line', 0))

    def make_slice(self, src, spec, ctx, line):
        if spec.lo is None and spec.hi is not None:
            try:
                hi_v = eval_num(spec.hi, {}, line, what='slice bound')
            except Diag:
                hi_v = None
            if hi_v == -1:
                s = SliceSrc(src, rev=True)
                s.wnode = self.solver.new()
                s.wnode.expr = ('wnode', src.wnode)
                return s
            raise Diag('only v[:-1] reversal is supported', line)
        lo_e = spec.lo if spec.lo is not None else A.NumLit(Fraction(0), line)
        s = SliceSrc(src, rev=False)
        s.wnode = self.solver.new()
        lo_w = self.num_to_wexpr(lo_e, ctx.inst_key, line)
        hi_w = self.num_to_wexpr(spec.hi, ctx.inst_key, line) if spec.hi is not None \
            else ('wnode', src.wnode)
        s.wnode.expr = ('bin', '-', hi_w, lo_w)
        s.lo_expr = lo_e
        s.hi_expr = spec.hi
        s.inst_key = ctx.inst_key
        return s

    def resolve_ref(self, ref, ctx, line):
        if ref.kind == 'bare':
            name = ref.parts[0]
            if name in ctx.ports_in or name in ctx.ep_in:
                return NetSrc(ctx.ports_in.get(name) or ctx.ep_in[name])
            if name in ctx.ports_out or name in ctx.ep_out:
                return NetSrc(ctx.ports_out.get(name) or ctx.ep_out[name])
            if name in ctx.insts:
                raise Diag('instance %r is not a value; use %s.out.N'
                           % (name, name), line)
            raise Diag('unknown name %r' % name, line)
        if ref.kind == 'qual':
            seg = A.QualSeg(ref.parts[0], ref.field, ref.index, line)
            return NetSrc(self.resolve_port_net(seg, ctx))
        if ref.kind == 'named':
            return self.named_member(ref.parts[0], ref.member, ctx, line)
        raise Diag('bad reference', line)

    def resolve_port_net(self, seg, ctx):
        inst = ctx.insts.get(seg.name)
        if inst is None:
            raise Diag('unknown instance %r' % seg.name, seg.line)
        if seg.field == 'out':
            idx = seg.index if seg.index is not None else 0
            if seg.index is None and len(inst.out_nets) != 1:
                raise Diag('%s.out shorthand requires a single output' % seg.name,
                           seg.line)
            if idx >= len(inst.out_nets):
                raise Diag('%s.out.%d out of range' % (seg.name, idx), seg.line)
            return inst.out_nets[idx]
        if seg.index is None:
            raise Diag('%s.in needs an index' % seg.name, seg.line)
        if seg.index >= len(inst.in_nets):
            raise Diag('%s.in.%d out of range' % (seg.name, seg.index), seg.line)
        return inst.in_nets[seg.index]

    # ---- driving ----

    def drive_inputs(self, inst, chain, ctx, line):
        if chain is None:
            chain = []
        if len(chain) != len(inst.in_nets):
            raise Diag('%s expects %d input(s), chain has %d member(s)'
                       % (inst.name, len(inst.in_nets), len(chain)), line)
        for net, src in zip(inst.in_nets, chain):
            self.drive(net, src, '%s input port' % inst.name, line)

    def drive(self, net, src, what, line):
        if net.source_value is not None or net.conn.is_clock:
            raise Diag('cannot drive source endpoint %r' % net.name, line)
        net.conn.contributors.append(('src', src, what))
        self.solver.add_le(src.wnode, net.conn.wnode, what, line)

    # ---- classification ----

    def classify(self, defn):
        key = id(defn)
        if key in self.class_memo:
            return self.class_memo[key]
        if isinstance(defn, A.GateDef):
            self.class_memo[key] = 'combinational'
            return 'combinational'
        if key in self._classifying:
            raise Diag('component %r instantiates itself transitively'
                       % '::'.join(defn.qname))
        self._classifying.append(key)
        saved_rt = self.rt
        saved_solver = self.solver
        scratch = Runtime()
        self.rt = scratch
        self.solver = WSolver()
        try:
            ctx = Ctx(self, defn.file_id, defn.ns, [])
            self.instantiate(list(defn.qname), True, 'scratch', ctx)
        finally:
            self.rt = saved_rt
            self.solver = saved_solver
            self._classifying.pop()
        nodes = set(n.nid for n in scratch.nodes)
        has_reg = any(n.kind == 'register' for n in scratch.nodes)
        arcs = {nid: set() for nid in nodes}
        for node in scratch.nodes:
            for src, _label in node.input_specs:
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
        self.class_memo[key] = cls
        return cls

    def _driver_nodes(self, src, rt, seen=None):
        if seen is None:
            seen = set()
        if id(src) in seen:
            return []
        seen.add(id(src))
        if isinstance(src, NetSrc):
            out = []
            for c in src.net.conn.contributors:
                if c[0] == 'node':
                    if c[1].kind != 'register':
                        out.append(c[1])
                elif c[0] == 'src':
                    out.extend(self._driver_nodes(c[1], rt, seen))
            return out
        if isinstance(src, SliceSrc):
            return self._driver_nodes(src.inner, rt, seen)
        if isinstance(src, ConcatSrc):
            out = []
            for p in src.parts:
                out.extend(self._driver_nodes(p, rt, seen))
            return out
        if isinstance(src, LateSrc):
            out = []
            for p in (src.parts or []):
                out.extend(self._driver_nodes(p, rt, seen))
            return out
        return []

    # ---- finalization ----

    def finalize(self, rt):
        def fix(src):
            if isinstance(src, LateSrc):
                if src.parts is None:
                    raise Diag('named connection @%s never declared' % src.name)
                if src.member is not None:
                    return src.parts[src.member]
                if len(src.parts) != 1:
                    raise Diag('@%s is a multi-member chain used as a single value'
                               % src.name)
                return src.parts[0]
            if isinstance(src, SliceSrc):
                src.inner = fix(src.inner)
                return src
            if isinstance(src, ConcatSrc):
                src.parts = [fix(p) for p in src.parts]
                return src
            return src
        for conn in rt.conns:
            conn.contributors = [
                (c[0], fix(c[1]), *c[2:]) if c[0] == 'src' else c
                for c in conn.contributors
            ]
        for node in rt.nodes:
            node.input_specs = [(fix(s), l) for s, l in node.input_specs]
        self.solver.solve()
        for net in rt.nets:
            net.conn.width = int(self.solver.eval(net.wnode))
        for conn in rt.conns:
            for c in conn.contributors:
                if c[0] == 'src':
                    self._finalize_src(c[1])
        for node in rt.nodes:
            for src, _l in node.input_specs:
                self._finalize_src(src)
        for node in rt.nodes:
            if node.kind == 'register':
                src = node.input_specs[1][0]
                clk_net = self._trace_clock(src)
                if clk_net is None:
                    raise Diag('register %s: clock input is not a transparent route '
                               'to a single clock generator%s'
                               % (node.name, self._clock_breaker(src)))
                node.clk_conn = clk_net.conn
        rt.finalize_reads()
        rt.initialize()

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
            hi = int(eval_num(src.hi_expr, env, what='slice bound')) \
                if src.hi_expr is not None else src.inner.width
            if not (0 <= lo < hi <= src.inner.width):
                raise Diag('invalid slice [%d:%d] on width %d'
                           % (lo, hi, src.inner.width))
            src.lo, src.hi = lo, hi
            src.width = hi - lo
        elif isinstance(src, ConcatSrc):
            for p in src.parts:
                self._finalize_src(p, seen)
            src.width = sum(p.width for p in src.parts)
        elif isinstance(src, NetSrc):
            src.width = src.net.conn.width
        elif isinstance(src, ConstSrc):
            src.width = len(src.value.trits)
        elif isinstance(src, LateSrc):
            for p in (src.parts or []):
                self._finalize_src(p, seen)
            src.width = src.parts[0].width if src.parts else None

    def _trace_clock(self, src, seen=None):
        if seen is None:
            seen = set()
        if id(src) in seen:
            return None
        seen.add(id(src))
        if isinstance(src, NetSrc):
            conn = src.net.conn
            if conn.is_clock and len(conn.contributors) == 1:
                return src.net
            if len(conn.contributors) != 1:
                return None
            c = conn.contributors[0]
            if c[0] == 'source':
                return c[1] if c[1].conn.is_clock else None
            if c[0] == 'node':
                return None
            return self._trace_clock(c[1], seen)
        if isinstance(src, SliceSrc) and not src.rev and src.lo == 0 \
                and src.hi == src.inner.width:
            return self._trace_clock(src.inner, seen)
        if isinstance(src, ConcatSrc) and len(src.parts) == 1:
            return self._trace_clock(src.parts[0], seen)
        return None

    def _clock_breaker(self, src):
        seen = set()
        while True:
            if isinstance(src, NetSrc):
                conn = src.net.conn
                if id(src) in seen:
                    return ''
                seen.add(id(src))
                if len(conn.contributors) != 1:
                    return ' (multiple output contributors on %s)' % conn.name
                c = conn.contributors[0]
                if c[0] == 'node':
                    return ' (broken by computation %r)' % c[1].name
                if c[0] == 'source' and not c[1].conn.is_clock:
                    return ' (ends at non-clock source %s)' % c[1].name
                if c[0] == 'src':
                    src = c[1]
                    continue
                return ''
            return ' (broken by non-transparent routing)'

    # ---- entrypoints (benches and sequential tests) ----

    def build_entry(self, with_items, body, file_id, ns, script_inputs=None):
        rt = Runtime()
        self.rt = rt
        self.solver = WSolver()
        self.all_insts = []
        ctx = Ctx(self, file_id, ns, [])
        names = set()
        keys = {}

        def add_source(name, key, width, value, line):
            if name in names:
                raise Diag('duplicate endpoint name %r' % name, line)
            if key in keys:
                raise Diag('duplicate input key %r' % key, line)
            names.add(name)
            keys[key] = name
            w = self.solver.new()
            w.expr = ('const', Fraction(width))
            net = rt.new_net('input.%s' % name, w)
            rt.add_source(net, value)
            rt.input_by_name[name] = net
            rt.input_by_key[key] = net
            ctx.ep_in[name] = net

        for st in body:
            if isinstance(st, A.InputDef):
                if st.init is not None:
                    if any(c not in ALL_STATES for c in st.init):
                        raise Diag('bad input initializer %r' % st.init, st.line)
                    add_source(st.name, st.key, len(st.init),
                               make_literal(st.init, tick=-1), st.line)
                else:
                    add_source(st.name, st.key, 1, Value((ZERO,)), st.line)
            elif isinstance(st, A.ClockDef):
                if st.name in names:
                    raise Diag('duplicate endpoint name %r' % st.name, st.line)
                names.add(st.name)
                period = self.const_int(st.period, st.line, ctx_file(ctx), 'clock period')
                if period < 1:
                    raise Diag('clock period must be a positive integer number of ticks',
                               st.line, ctx_file(ctx))
                duty = eval_num(st.duty, {}, st.line, ctx_file(ctx), 'clock duty')
                if not (0 < duty < 100):
                    raise Diag('clock duty must be strictly between 0 and 100',
                               st.line, ctx_file(ctx))
                h = period * duty / 100
                if h.denominator != 1 or not (0 < h < period):
                    raise Diag('clock duty must give an integral high portion strictly '
                               'inside the period (got %s of %s)' % (h, period),
                               st.line, ctx_file(ctx))
                w = self.solver.new()
                w.expr = ('const', Fraction(1))
                net = rt.new_net('clock.%s' % st.name, w)
                rt.add_clock(net, period, duty)
                ctx.ep_in[st.name] = net
            elif isinstance(st, A.OutputDef):
                if st.name in names:
                    raise Diag('duplicate endpoint name %r' % st.name, st.line)
                names.add(st.name)
                w = self.solver.new()
                net = rt.new_net('output.%s' % st.name, w)
                ctx.ep_out[st.name] = net
                rt.out_endpoints.append(Endpoint(st.label, NetSrc(net), st.mode))
        for key, width in (script_inputs or {}).items():
            if key not in keys:
                add_source(key, key, width, Value((ZERO,) * width), 0)
        for item in with_items:
            self.instantiate(item.type_parts, item.absolute, item.inst_name, ctx,
                             init=item.init, line=item.line)
        for st in body:
            if isinstance(st, A.Connection):
                self.compile_chain(st, ctx)
        self.finalize(rt)
        return rt

    # ---- table tests ----

    def build_table_row(self, test_def, row):
        parts, absolute = test_def.target
        tdef = self.prog.lookup_def(parts, absolute, test_def.ns, test_def.file_id,
                                     test_def.line, test_def.file)
        if not isinstance(tdef, A.BlockDef):
            raise Diag('table test target %r must be a block' % '::'.join(parts),
                       test_def.line, test_def.file)
        cls = self.classify(tdef)
        if cls != 'combinational':
            raise Diag('table tests target combinational blocks only (%r is %s)'
                       % ('::'.join(tdef.qname), cls), test_def.line, test_def.file)
        rt = Runtime()
        self.rt = rt
        self.solver = WSolver()
        self.all_insts = []
        ctx = Ctx(self, test_def.file_id, test_def.ns, [])
        inst = self.instantiate(list(tdef.qname), True, 'dut', ctx, line=test_def.line)
        in_srcs = self.row_chain(row.inputs, ctx)
        self.drive_inputs(inst, in_srcs, ctx, row.line)
        exp_srcs = self.row_chain(row.outputs, ctx)
        if len(exp_srcs) != len(inst.out_nets):
            raise Diag('row expects %d output(s), block has %d'
                       % (len(exp_srcs), len(inst.out_nets)), row.line)
        self.finalize(rt)
        rt.run_until_idle(100000)
        if rt.cur_queue:
            raise Diag('combinational network did not reach idle (compiler bug)')
        actual = [net.conn.value.trits for net in inst.out_nets]
        expected = [s.read(rt).trits for s in exp_srcs]
        return actual, expected

    def row_chain(self, seg, ctx):
        if isinstance(seg, A.AdapterSeg):
            return self.apply_adapter(seg, [], ctx)
        if isinstance(seg, A.ExprSeg):
            return self.compile_expr(seg.expr, ctx)
        raise Diag('table row chains must be adapters or expressions', seg.line)

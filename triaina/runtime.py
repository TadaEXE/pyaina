"""Runtime graph: sources, nets, computation nodes, and the tick scheduler.

One tick is one complete discrete scheduler step: apply sources, evaluate
each queued node once against the post-source/pre-commit snapshot, commit
all outputs simultaneously, queue consumers of changed observations for
the next tick.
"""

from itertools import product

from .values import (NEG, ZERO, POS, U, E, Z, LOGICAL, Value, unknown,
                     make_literal, pad_left, concat_values, slice_value,
                     reverse_value, merge_prov)


# ---- sources (compile-time routing endpoints of a value) ----

class Src:
    def __init__(self, wnode):
        self.wnode = wnode
        self.width = None

    def constituents(self):
        return []

    def read(self, rt, kind):
        raise NotImplementedError

    def read_raw(self, rt):
        return self.read(rt, 'raw')


class NetSrc(Src):
    def __init__(self, net):
        super().__init__(net.wnode)
        self.net = net

    def constituents(self):
        return [self.net]

    def read(self, rt, kind):
        v = self.net.value
        if kind != 'raw' or self.net.boundary:
            return Value(v.observed(raw=False), v.prov)
        return v


class ConstSrc(Src):
    def __init__(self, wnode, value):
        super().__init__(wnode)
        self.value = value

    def read(self, rt, kind):
        v = self.value
        if kind != 'raw':
            v = Value(v.observed(raw=False), v.prov)
        return v


class SliceSrc(Src):
    def __init__(self, inner, lo, hi, rev=False):
        super().__init__(None)  # wnode set by compiler
        self.inner = inner
        self.lo = lo
        self.hi = hi
        self.rev = rev

    def constituents(self):
        return self.inner.constituents()

    def read(self, rt, kind):
        v = self.inner.read(rt, kind)
        if self.rev:
            return reverse_value(v)
        # values are stored in written order (leftmost = most significant);
        # index 0 is the rightmost trit, so v[lo:hi] maps to positions
        # [w-hi : w-lo] of the written-order tuple
        w = self.inner.width
        return Value(v.trits[w - self.hi:w - self.lo], v.prov)


class ConcatSrc(Src):
    def __init__(self, parts):
        super().__init__(None)
        self.parts = parts

    def constituents(self):
        out = []
        for p in self.parts:
            out.extend(p.constituents())
        return out

    def read(self, rt, kind):
        return concat_values([p.read(rt, kind) for p in self.parts])


class LateSrc(Src):
    """Forward reference to a named connection; parts bound at scope end."""

    def __init__(self, name):
        super().__init__(None)
        self.name = name
        self.parts = None
        self.member = None

    def constituents(self):
        out = []
        for p in (self.parts or []):
            out.extend(p.constituents())
        return out

    def read(self, rt, kind):
        if not self.parts or len(self.parts) != 1:
            raise RuntimeError('late src %s used as a single value' % self.name)
        return self.parts[0].read(rt, kind)


# ---- nets ----

class Net:
    _ids = 0

    def __init__(self, rt, name, wnode, boundary=False):
        Net._ids += 1
        self.nid = Net._ids
        self.name = name
        self.wnode = wnode
        self.width = None
        self.value = None
        self.driver = None  # None | ('node', node) | ('src', Src)
        self.boundary = boundary
        self.is_clock = False
        self.consumers = []  # list of Read
        rt.nets.append(self)


# ---- reads (consumer observations) ----

class Read:
    def __init__(self, src, kind, owner, label):
        self.src = src
        self.kind = kind  # 'normal' | 'raw'
        self.owner = owner  # node or Endpoint
        self.label = label
        self.last = None
        for net in src.constituents():
            net.consumers.append(self)

    def observed(self, rt):
        return self.src.read(rt, self.kind)

    def change_key(self, rt):
        v = self.observed(rt)
        return (v.trits, v.prov)


# ---- nodes ----

class Node:
    def __init__(self, rt, kind, name):
        self.rt = rt
        self.kind = kind
        self.name = name
        self.nid = len(rt.nodes)
        self.input_specs = []  # list of (Src, kind, label)
        self.inputs = []       # list of Read, bound by finalize_reads()
        self.outs = []         # list of Net
        rt.nodes.append(self)

    def add_input(self, src, kind, label):
        self.input_specs.append((src, kind, label))

    def evaluate(self, rt, src_changes):
        raise NotImplementedError


def apply_gate_table(table, vals, width):
    """Positionwise gate application with zero-extension, e dominance, and
    independent unknown enumeration. Returns Value."""
    ext = [pad_left(v, width) for v in vals]
    out = []
    prov = ()
    for i in range(width):
        col = [v.trits[i] for v in ext]
        if E in col:
            out.append(E)
            for v in ext:
                if v.trits[i] == E:
                    prov = prov + v.prov
            continue
        unk = [j for j in range(len(col)) if col[j] == U]
        results = set()
        for combo in product(LOGICAL, repeat=len(unk)):
            t = list(col)
            for j, c in zip(unk, combo):
                t[j] = c
            results.add(table[tuple(t)])
        out.append(results.pop() if len(results) == 1 else U)
    return Value(out, tuple(dict.fromkeys(prov)))


class GateNode(Node):
    def __init__(self, rt, name, table, arity):
        super().__init__(rt, 'gate', name)
        self.table = table
        self.arity = arity

    def evaluate(self, rt, src_changes):
        vals = [r.observed(rt) for r in self.inputs]
        return [apply_gate_table(self.table, vals, self.outs[0].width)]


def match_pattern(items, trits):
    seqs = [i for i, it in enumerate(items) if it[0] == 'seq']
    if not seqs:
        if len(items) != len(trits):
            return False
        return all(it[0] == 'lit' and it[1] == t or it[0] == 'any'
                   for it, t in zip(items, trits))
    i = seqs[0]
    pre, post = items[:i], items[i + 1:]
    if len(trits) < len(pre) + len(post):
        return False
    for it, t in zip(pre, trits):
        if not (it[0] == 'any' or (it[0] == 'lit' and it[1] == t)):
            return False
    for it, t in zip(post, trits[len(trits) - len(post):]):
        if not (it[0] == 'any' or (it[0] == 'lit' and it[1] == t)):
            return False
    return True


class MatchNode(Node):
    def __init__(self, rt, name, patterns, arms):
        super().__init__(rt, 'match', name)
        self.patterns = patterns  # list of pattern item lists
        self.arms = arms          # list of Src (read raw: arms may drive z)

    def evaluate(self, rt, src_changes):
        sel = self.inputs[0].observed(rt)
        for pat, arm in zip(self.patterns, self.arms):
            if match_pattern(pat, sel.trits):
                return [arm.read(rt, 'raw')]
        raise RuntimeError('match without matching arm (compiler must require final _)')


class MergeNode(Node):
    def __init__(self, rt, name):
        super().__init__(rt, 'merge', name)

    def evaluate(self, rt, src_changes):
        members = [r.observed(rt) for r in self.inputs]  # raw reads
        w = self.outs[0].width
        out = []
        prov = ()
        for i in range(w):
            col = [(v.trits[i], v.prov) for v in members]
            states = [c[0] for c in col]
            if E in states:
                out.append(E)
                for c in col:
                    if c[0] == E:
                        prov = prov + c[1]
                continue
            if U in states:
                out.append(U)
                continue
            active = [(s, p) for s, p in col if s != Z]
            if not active:
                out.append(U)  # all released -> floating
                continue
            first = active[0][0]
            if all(s == first for s, _ in active):
                out.append(first)
                continue
            # contention: fault with provenance naming contributing ports
            out.append(E)
            detail = tuple((r.label, s) for r, (s, _) in zip(self.inputs, col) if s != Z)
            prov = prov + ((rt.tick, 'contention', detail),)
        return [Value(out, tuple(dict.fromkeys(prov)))]


class FoldNode(Node):
    def __init__(self, rt, name, table):
        super().__init__(rt, 'fold', name)
        self.table = table

    def evaluate(self, rt, src_changes):
        members = [r.observed(rt) for r in self.inputs]
        w = self.outs[0].width
        acc = pad_left(members[0], w)
        for m in members[1:]:
            acc = apply_gate_table(self.table, [acc, pad_left(m, w)], w)
        return [acc]


class RegisterNode(Node):
    def __init__(self, rt, name, init_value):
        super().__init__(rt, 'register', name)
        self.init_value = init_value
        self.clk_net = None  # set by compiler (must be a clock source)

    def evaluate(self, rt, src_changes):
        ch = src_changes.get(self.clk_net) if self.clk_net is not None else None
        if ch is not None:
            old, new = ch
            if old.observed(raw=False) == (NEG,) and new.observed(raw=False) == (POS,):
                data = self.inputs[0].observed(rt)  # pre-commit snapshot
                return [pad_left(data, self.outs[0].width)]
        return [self.outs[0].value]  # hold


# ---- clocks ----

class Clock:
    def __init__(self, net, period, duty):
        self.net = net
        self.period = period
        h = period * duty / 100
        if h.denominator != 1 or not (0 < h < period):
            raise ValueError('clock duty must give integral high portion strictly inside the period')
        self.H = int(h)
        self.L = period - self.H

    def value_at(self, tick):
        if tick < self.L:
            return NEG
        t = (tick - self.L) % self.period
        return POS if t < self.H else NEG


# ---- output endpoints ----

class Record:
    __slots__ = ('seq', 'tick', 'phase', 'label', 'trits')

    def __init__(self, seq, tick, phase, label, trits):
        self.seq = seq
        self.tick = tick
        self.phase = phase
        self.label = label
        self.trits = trits

    def __str__(self):
        return '%d %s %s: %s' % (self.tick, self.phase, self.label, ''.join(self.trits))


class Endpoint:
    def __init__(self, label, src, mode):
        self.label = label
        self.read = None  # Read, set at init
        self.src = src
        self.mode = mode
        self.last_emitted = None


# ---- the scheduler ----

class Runtime:
    def __init__(self):
        self.nets = []
        self.nodes = []
        self.input_by_key = {}   # key -> Net
        self.input_by_name = {}  # endpoint name -> Net
        self.clocks = []
        self.out_endpoints = []
        self.cur_queue = set()
        self.next_queue = set()
        self.tick = 0
        self.records = []
        self.seq = 0

    def new_net(self, name, wnode, boundary=False):
        return Net(self, name, wnode, boundary)

    def add_clock(self, net, period, duty):
        c = Clock(net, period, duty)
        net.is_clock = True
        self.clocks.append(c)
        return c

    # -- initialization --

    def finalize_reads(self):
        for node in self.nodes:
            for src, kind, label in node.input_specs:
                node.inputs.append(Read(src, kind, node, label))
        for ep in self.out_endpoints:
            ep.read = Read(ep.src, 'normal', ep, ep.label)

    def settle_routing(self):
        """Recompute net-driven nets from their drivers (transparent routing
        consumes no ticks, so this also applies to the initial snapshot)."""
        for _ in range(len(self.nets) + 2):
            progress = False
            for net in self.nets:
                if net.driver is None or net.driver[0] != 'src':
                    continue
                v = pad_left(net.driver[1].read_raw(self), net.width)
                if v.trits != net.value.trits or v.prov != net.value.prov:
                    net.value = v
                    progress = True
            if not progress:
                break

    def initialize(self, const_nets):
        for net in self.nets:
            if net.value is None:
                net.value = unknown(net.width)
        for net, val in const_nets:
            net.value = val
        for node in self.nodes:
            if node.kind == 'register':
                node.outs[0].value = node.init_value
        self.settle_routing()
        for ep in self.out_endpoints:
            ep.read.last = ep.read.change_key(self)
            ep.last_emitted = None
        for node in self.nodes:
            for r in node.inputs:
                r.last = r.change_key(self)
        self.cur_queue = set(node.nid for node in self.nodes)
        self.emit('init')

    # -- change propagation through net-driven nets --

    def propagate(self, changed, src_changes=None):
        changed = list(changed)
        seen = set(n.nid for n in changed)
        progress = True
        passes = 0
        max_passes = len(self.nets) + 2
        while progress and passes < max_passes:
            progress = False
            passes += 1
            for net in self.nets:
                if net.driver is None or net.driver[0] != 'src':
                    continue
                src = net.driver[1]
                consts = src.constituents()
                if not any(c.nid in seen for c in consts):
                    continue
                v = pad_left(src.read_raw(self), net.width)
                if v.trits != net.value.trits or v.prov != net.value.prov:
                    if src_changes is not None:
                        src_changes[net] = (net.value, v)
                    net.value = v
                    if net.nid not in seen:
                        seen.add(net.nid)
                        changed.append(net)
                        progress = True
        return changed

    def _enqueue(self, queue, read):
        key = read.change_key(self)
        if read.last is None or key != read.last:
            read.last = key
            owner = read.owner
            if isinstance(owner, Node):
                queue.add(owner.nid)

    # -- one tick --

    def run_tick(self, events=None):
        src_changes = {}
        changed = []
        for net, val in (events or {}).items():
            old = net.value
            if old.trits != val.trits or old.prov != val.prov:
                src_changes[net] = (old, val)
                net.value = val
                changed.append(net)
        for clk in self.clocks:
            v = Value((clk.value_at(self.tick),))
            old = clk.net.value
            if old.trits != v.trits:
                src_changes[clk.net] = (old, v)
                clk.net.value = v
                changed.append(clk.net)
        changed = self.propagate(changed, src_changes)
        # phase 1: consumers of changed sources join the current queue
        for net in changed:
            for read in net.consumers:
                self._enqueue(self.cur_queue, read)
        # phase 2: evaluate each queued node once on the snapshot
        results = {}
        for nid in sorted(self.cur_queue):
            node = self.nodes[nid]
            results[nid] = node.evaluate(self, src_changes)
        # phase 3: simultaneous commit, then transparent routing
        committed = []
        for nid, vals in results.items():
            node = self.nodes[nid]
            for net, v in zip(node.outs, vals):
                if net.value.trits != v.trits or net.value.prov != v.prov:
                    net.value = v
                    committed.append(net)
        committed = self.propagate(committed)
        for net in committed:
            for read in net.consumers:
                self._enqueue(self.next_queue, read)
        # phase 4: publish post-tick observations, then advance
        self.emit('post')
        self.tick += 1
        self.cur_queue = self.next_queue
        self.next_queue = set()

    def emit(self, phase):
        for ep in self.out_endpoints:
            obs = ep.read.observed(self)
            if phase == 'init' or ep.mode == 'every_step' or \
                    ep.last_emitted is None or obs.trits != ep.last_emitted.trits:
                self.records.append(Record(self.seq, self.tick, phase, ep.label, obs.trits))
                self.seq += 1
                ep.last_emitted = obs

    def run_until_idle(self, max_ticks):
        n = 0
        while self.cur_queue and n < max_ticks:
            self.run_tick()
            n += 1
        return n

"""Runtime graph: sources, connections, computation nodes, tick scheduler.

A connection (net) may have several output contributors (node output ports,
source endpoints, constant/sliced/concatenated drivers). For each trit
position independently the connection resolves its contributions to a
five-state value: e or conflicting known actives -> e; else any u -> u;
else the agreeing known actives; all-z or no contributors -> u. Resolution
is part of connection propagation and adds no computational node or tick.

One tick is one scheduler step. Initialization is the theoretical tick -1;
tick 0 performs the first evaluation. Tick k: apply input events and clock
transitions assigned to k and resolve, enqueue affected consumers; evaluate
each node in the current queue once against the post-source snapshot;
commit outputs simultaneously, resolve, and enqueue consumers for tick k+1;
emit post-tick observations and advance.
"""

import threading
import time
from itertools import product

from .values import (NEG, ZERO, POS, U, E, Z, LOGICAL, Value, unknown,
                     make_literal, pad_left, concat_values, slice_value,
                     reverse_value)


# ---- sources (routing endpoints of a value) ----

class Src:
    def __init__(self, wnode):
        self.wnode = wnode
        self.width = None

    def constituents(self):
        return []

    def read(self, rt):
        """Resolved five-state value observed by a consumer."""
        raise NotImplementedError

    def read_raw(self, rt):
        """Six-state contribution value (may contain z)."""
        raise NotImplementedError


class NetSrc(Src):
    def __init__(self, net):
        super().__init__(net.wnode)
        self.net = net

    def constituents(self):
        return [self.net]

    def read(self, rt):
        return self.net.conn.value

    def read_raw(self, rt):
        return self.net.conn.raw


class ConstSrc(Src):
    def __init__(self, wnode, value):
        super().__init__(wnode)
        self.value = value

    def read(self, rt):
        v = self.value
        return Value(v.observed(raw=False), v.prov)

    def read_raw(self, rt):
        return self.value


class SliceSrc(Src):
    def __init__(self, inner, rev=False):
        super().__init__(None)  # wnode set by compiler
        self.inner = inner
        self.rev = rev
        self.lo_expr = None
        self.hi_expr = None
        self.inst_key = None
        self.lo = None
        self.hi = None

    def constituents(self):
        return self.inner.constituents()

    def _view(self, v):
        if self.rev:
            return reverse_value(v)
        w = self.inner.width
        return Value(v.trits[w - self.hi:w - self.lo], v.prov)

    def read(self, rt):
        return self._view(self.inner.read(rt))

    def read_raw(self, rt):
        return self._view(self.inner.read_raw(rt))


class ConcatSrc(Src):
    def __init__(self, parts):
        super().__init__(None)
        self.parts = parts

    def constituents(self):
        out = []
        for p in self.parts:
            out.extend(p.constituents())
        return out

    def read(self, rt):
        return concat_values([p.read(rt) for p in self.parts])

    def read_raw(self, rt):
        return concat_values([p.read_raw(rt) for p in self.parts])


class LateSrc(Src):
    """Forward reference to a named connection; bound at scope end."""

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

    def read(self, rt):
        if not self.parts or len(self.parts) != 1:
            raise RuntimeError('named connection @%s used as a single value' % self.name)
        return self.parts[0].read(rt)

    def read_raw(self, rt):
        if not self.parts or len(self.parts) != 1:
            raise RuntimeError('named connection @%s used as a single value' % self.name)
        return self.parts[0].read_raw(rt)


# ---- connections and nets ----

class Connection:
    _ids = 0

    def __init__(self, rt, name, wnode):
        Connection._ids += 1
        self.cid = Connection._ids
        self.rt = rt
        self.name = name
        self.wnode = wnode
        self.width = None
        self.raw = None            # six-state driver view (z preserved)
        self.value = None          # resolved five-state Value (z read as u)
        self.contributors = []     # ('node', node, idx, label) |
                                   # ('src', src, label) | ('source', net, label)
        self.consumers = []        # Read objects
        self.is_clock = False
        rt.conns.append(self)


class Net:
    _ids = 0

    def __init__(self, rt, name, wnode):
        Net._ids += 1
        self.nid = Net._ids
        self.name = name
        self.wnode = wnode
        self.conn = Connection(rt, name, wnode)
        self.source_value = None   # for source endpoints (inputs, clocks)
        rt.nets.append(self)


# ---- reads (consumer observations) ----

class Read:
    def __init__(self, src, owner, label):
        self.src = src
        self.owner = owner  # Node or Endpoint
        self.label = label
        self.last = None    # last observed trits (change identity)
        for net in src.constituents():
            net.conn.consumers.append(self)

    def observed(self, rt):
        return self.src.read(rt)


# ---- nodes ----

class Node:
    def __init__(self, rt, kind, name):
        self.rt = rt
        self.kind = kind
        self.name = name
        self.nid = len(rt.nodes)
        self.input_specs = []   # list of (Src, label)
        self.inputs = []        # list of Read, bound by finalize_reads()
        self.outs = []          # list of Net
        self.out_values = []    # list of Value (six-state output ports)
        rt.nodes.append(self)

    def add_input(self, src, label):
        self.input_specs.append((src, label))

    def evaluate(self, rt, changes):
        raise NotImplementedError


def apply_gate_table(table, vals, width):
    """Positionwise gate application with zero-extension, e dominance, and
    independent per-position unknown enumeration (a unanimous result,
    including z, is emitted; otherwise u)."""
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

    def evaluate(self, rt, changes):
        vals = [r.observed(rt) for r in self.inputs]
        return [apply_gate_table(self.table, vals, self.outs[0].conn.width)]


class RegisterNode(Node):
    def __init__(self, rt, name, init_value):
        super().__init__(rt, 'register', name)
        self.init_value = init_value
        self.clk_conn = None  # set by compiler (must trace to a clock origin)

    def evaluate(self, rt, changes):
        ch = changes.get(self.clk_conn) if self.clk_conn is not None else None
        if ch is not None and ch[0].trits == (NEG,) and ch[1].trits == (POS,):
            data = self.inputs[0].observed(rt)  # pre-commit resolved snapshot
            return [pad_left(data, self.outs[0].conn.width)]
        return [self.out_values[0]]  # hold between edges


# ---- clocks ----

class Clock:
    def __init__(self, net, period, duty):
        self.net = net
        self.period = period
        h = period * duty / 100
        if h.denominator != 1 or not (0 < h < period):
            raise ValueError('clock duty must give an integral high portion strictly '
                             'inside the period')
        self.H = int(h)
        self.L = period - self.H

    def value_at(self, tick):
        if tick < self.L:
            return NEG
        t = (tick - self.L) % self.period
        return POS if t < self.H else NEG


# ---- output endpoints and records ----

class Record:
    __slots__ = ('seq', 'tick', 'label', 'trits')

    def __init__(self, seq, tick, label, trits):
        self.seq = seq
        self.tick = tick
        self.label = label
        self.trits = tuple(trits)

    def __str__(self):
        return '%d %s: %s' % (self.tick, self.label, ''.join(self.trits))


class Endpoint:
    def __init__(self, label, src, mode):
        self.label = label
        self.src = src
        self.mode = mode  # 'changes' | 'every_step'
        self.read = None
        self.last_emitted = None


# ---- connection resolution ----

def resolve_conn(rt, conn):
    """Trit-wise resolution of the connection's contributions, returning the
    raw six-state driver view (a fully released connection stays z here)."""
    w = conn.width
    cols = []
    for kind, obj, *rest in conn.contributors:
        label = rest[-1]
        if kind == 'node':
            v = obj.out_values[rest[0]]
        elif kind == 'source':
            v = obj.source_value
        else:
            v = obj.read_raw(rt)
        if v is None:
            v = unknown(w)  # not yet resolved (initial pass)
        cols.append((pad_left(v, w), label))
    out = []
    prov = ()
    for i in range(w):
        states = [v.trits[i] for v, _ in cols]
        if E in states:
            out.append(E)
            for v, _ in cols:
                if v.trits[i] == E:
                    prov = prov + v.prov
            continue
        known = [s for s in states if s in LOGICAL]
        if len(set(known)) > 1:
            out.append(E)
            drivers = tuple((label, v.trits[i]) for v, label in cols
                            if v.trits[i] in LOGICAL)
            prov = prov + ((rt.tick, 'contention', (conn.name, i, drivers)),)
            continue
        if U in states:
            out.append(U)
            continue
        if known:
            out.append(known[0])
            continue
        out.append(Z)  # all released (z), or no contributors
    return Value(tuple(out), tuple(dict.fromkeys(prov)))


def resolved_view(raw):
    """Five-state connection value: a released driver (z) reads as u."""
    return Value(tuple(U if t == Z else t for t in raw.trits), raw.prov)


# ---- the scheduler ----

class Runtime:
    def __init__(self):
        self.nets = []
        self.conns = []
        self.nodes = []
        self.input_by_key = {}    # key -> Net
        self.input_by_name = {}   # endpoint name -> Net
        self.clocks = []
        self.out_endpoints = []
        self.cur_queue = set()
        self.next_queue = set()
        self.tick = -1            # last completed tick; init snapshot is tick -1
        self.records = []
        self.seq = 0
        self.subscribers = []
        self.pending = []         # (tick, net, Value) input injections

    def new_net(self, name, wnode):
        return Net(self, name, wnode)

    def add_source(self, net, value):
        net.source_value = value
        net.conn.contributors.append(('source', net, net.name))

    def add_clock(self, net, period, duty):
        c = Clock(net, period, duty)
        net.conn.is_clock = True
        self.add_source(net, Value((NEG,)))
        self.clocks.append(c)
        return c

    # -- initialization (tick -1) --

    def finalize_reads(self):
        for node in self.nodes:
            for src, label in node.input_specs:
                node.inputs.append(Read(src, node, label))
        for ep in self.out_endpoints:
            ep.read = Read(ep.src, ep, ep.label)

    def resolve_all(self, changed=None, changes=None):
        for _ in range(len(self.conns) + 2):
            progress = False
            for conn in self.conns:
                new_raw = resolve_conn(self, conn)
                new = resolved_view(new_raw)
                old = conn.value
                old_raw = conn.raw
                raw_changed = old_raw is None or new_raw.trits != old_raw.trits or \
                    new_raw.prov != old_raw.prov
                if old is None or new.trits != old.trits or new.prov != old.prov or \
                        raw_changed:
                    if changes is not None and old is not None:
                        changes[conn] = (old, new)
                    trits_changed = old is None or new.trits != old.trits
                    conn.raw = new_raw
                    conn.value = new
                    if trits_changed and changed is not None:
                        changed.append(conn)
                    progress = True
            if not progress:
                break

    def initialize(self):
        for node in self.nodes:
            if node.kind == 'gate':
                node.out_values = [unknown(node.outs[0].conn.width)]
            elif node.kind == 'register':
                node.out_values = [node.init_value]
        self.resolve_all()
        for node in self.nodes:
            for r in node.inputs:
                r.last = r.observed(self).trits
        for ep in self.out_endpoints:
            ep.read.last = ep.read.observed(self).trits
            ep.last_emitted = None
        self.cur_queue = set(node.nid for node in self.nodes)
        self.emit(-1)  # tick -1 snapshot

    # -- input injection --

    def inject(self, key, trits, tick=None):
        """Assign an input value; an input arriving during a tick is assigned
        to a later tick boundary. Returns the tick it was assigned to."""
        net = self.input_by_key.get(key)
        if net is None:
            raise KeyError('no input with key %r' % key)
        if len(trits) != net.conn.width:
            raise ValueError('input %r expects width %d, got %r'
                             % (key, net.conn.width, trits))
        if any(c not in ('-', '0', '+', 'u', 'e', 'z') for c in trits):
            raise ValueError('bad trit symbols %r' % trits)
        if tick is None or tick <= self.tick:
            tick = self.tick + 1
        prov = ((tick, 'external', 'input %r' % key),) if E in trits else ()
        self.pending.append((tick, net, Value(tuple(trits), prov)))
        self.pending.sort(key=lambda p: p[0])
        return tick

    def has_pending(self):
        return any(t > self.tick for t, _, _ in self.pending)

    # -- queueing --

    def _enqueue(self, queue, read):
        obs = read.observed(self)
        if read.last is None or obs.trits != read.last:
            read.last = obs.trits
            owner = read.owner
            if isinstance(owner, Node):
                queue.add(owner.nid)

    # -- one tick --

    def run_tick(self):
        k = self.tick + 1
        changes = {}
        changed = []
        events = [(net, val) for t, net, val in self.pending if t == k]
        self.pending = [p for p in self.pending if p[0] != k]
        for net, val in events:  # last event in record order wins
            net.source_value = val
        for clk in self.clocks:
            clk.net.source_value = Value((clk.value_at(k),))
        self.resolve_all(changed=changed, changes=changes)
        # phase 1: consumers affected by changed input values join this tick
        for conn in changed:
            for read in conn.consumers:
                self._enqueue(self.cur_queue, read)
        # phase 2: evaluate each queued node once on the same snapshot
        results = {}
        for nid in sorted(self.cur_queue):
            node = self.nodes[nid]
            results[nid] = node.evaluate(self, changes)
        # phase 3: commit simultaneously, resolve, queue next tick
        for nid, vals in results.items():
            node = self.nodes[nid]
            for i, v in enumerate(vals):
                node.out_values[i] = v
        committed = []
        self.resolve_all(changed=committed)
        for conn in committed:
            for read in conn.consumers:
                self._enqueue(self.next_queue, read)
        # phase 4: post-tick observations, then advance
        self.emit(k)
        self.tick = k
        self.cur_queue = self.next_queue
        self.next_queue = set()

    def emit(self, tick):
        for ep in self.out_endpoints:
            obs = ep.read.observed(self)
            if ep.mode == 'every_step' or ep.last_emitted is None or \
                    obs.trits != ep.last_emitted.trits:
                rec = Record(self.seq, tick, ep.label, obs.trits)
                self.seq += 1
                self.records.append(rec)
                ep.last_emitted = obs
                for cb in self.subscribers:
                    cb(rec)

    def run_until_idle(self, max_ticks):
        n = 0
        while self.cur_queue and n < max_ticks:
            self.run_tick()
            n += 1
        return n

    # -- host API --

    def step(self):
        self.run_tick()

    def advance(self, n):
        for _ in range(n):
            self.run_tick()

    def subscribe(self, cb):
        self.subscribers.append(cb)

    def outputs(self):
        return {ep.label: ''.join(ep.read.observed(self).trits)
                for ep in self.out_endpoints}

    def diagnostics(self):
        out = []
        seen = set()
        vals = [c.value for c in self.conns if c.value is not None]
        for node in self.nodes:
            vals.extend(node.out_values)
        for v in vals:
            for origin in v.prov:
                if origin not in seen:
                    seen.add(origin)
                    tick, reason, detail = origin
                    out.append('tick %s: %s: %s' % (tick, reason, detail))
        return out

    def run_paced(self, ticks, interval):
        """Advance `ticks` ticks; `interval` is the minimum wall-clock target
        between tick starts. Every tick completes fully."""
        for _ in range(ticks):
            start = time.monotonic()
            self.run_tick()
            elapsed = time.monotonic() - start
            if interval > 0 and elapsed < interval:
                time.sleep(interval - elapsed)


class Pacer:
    """Background paced execution with start/pause/stop."""

    def __init__(self, rt, interval):
        self.rt = rt
        self.interval = interval
        self._pause = threading.Event()
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is not None:
            return
        self._stop.clear()
        self._pause.clear()

        def loop():
            while not self._stop.is_set():
                self._pause.wait()
                if self._stop.is_set():
                    break
                t0 = time.monotonic()
                self.rt.run_tick()
                elapsed = time.monotonic() - t0
                if self.interval > 0 and elapsed < self.interval:
                    time.sleep(self.interval - elapsed)

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()

    def pause(self):
        self._pause.set()

    def resume(self):
        self._pause.clear()

    def stop(self):
        self._stop.set()
        self._pause.clear()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

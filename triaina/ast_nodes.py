"""AST nodes for Triaina."""


class Node:
    __slots__ = ('line',)

    def __init__(self, line=0):
        self.line = line


# ---- numeric expressions (widths, bounds, backtick configs) ----

class NumLit(Node):
    def __init__(self, value, line=0):
        super().__init__(line)
        self.value = value  # Fraction


class NumParam(Node):
    def __init__(self, name, line=0):
        super().__init__(line)
        self.name = name


class NumUnary(Node):
    def __init__(self, op, operand, line=0):
        super().__init__(line)
        self.op = op
        self.operand = operand


class NumBinary(Node):
    def __init__(self, op, left, right, line=0):
        super().__init__(line)
        self.op = op
        self.left = left
        self.right = right


# ---- width specs ----
# ('const', NumExpr) | ('inferred', None) | ('param', name)

class WidthSpec(Node):
    def __init__(self, kind, name=None, expr=None, line=0):
        super().__init__(line)
        self.kind = kind        # 'const' | 'inferred' | 'param'
        self.name = name        # for 'param': the ?n name
        self.expr = expr        # for 'const': NumExpr


# ---- slice spec ----

class SliceSpec(Node):
    def __init__(self, lo, hi, line=0):
        super().__init__(line)
        self.lo = lo   # NumExpr or None (default 0)
        self.hi = hi   # NumExpr or None (default width)


# ---- references ----

class Ref(Node):
    """bare name, name.in.N / name.out.N, or @name[.i]"""
    def __init__(self, kind, name, field=None, index=None, member=None, line=0):
        super().__init__(line)
        self.kind = kind      # 'bare' | 'qual' | 'named'
        self.name = name
        self.field = field    # 'in' | 'out'
        self.index = index    # int or None
        self.member = member  # int or None for @name.member


# ---- expressions ----

class LitExpr(Node):
    def __init__(self, text, slice=None, line=0):
        super().__init__(line)
        self.text = text
        self.slice = slice  # SliceSpec or None


class RefExpr(Node):
    def __init__(self, ref, slice=None, line=0):
        super().__init__(line)
        self.ref = ref
        self.slice = slice  # SliceSpec or None


class ConcatExpr(Node):
    def __init__(self, parts, line=0):
        super().__init__(line)
        self.parts = parts


class CallExpr(Node):
    def __init__(self, name, args, line=0):
        super().__init__(line)
        self.name = name
        self.args = args


class MatchExpr(Node):
    def __init__(self, selector, arms, line=0):
        super().__init__(line)
        self.selector = selector
        self.arms = arms  # list of (pattern, expr); pattern = list of items


# pattern items: ('lit', state) | ('any',) | ('seq',)


# ---- chain segments ----

class AdapterSeg(Node):
    def __init__(self, members, line=0):
        super().__init__(line)
        self.members = members  # list of AdapterMember


class AdapterMember(Node):
    """literal | $n | @name[.i], each optionally sliced"""
    def __init__(self, base, slice=None, line=0):
        super().__init__(line)
        self.base = base  # ('lit', text) | ('usd', n) | ('named', name, member)
        self.slice = slice


class NamedSeg(Node):
    def __init__(self, name, width=None, line=0):
        super().__init__(line)
        self.name = name
        self.width = width  # WidthSpec const or None


class BareSeg(Node):
    def __init__(self, name, line=0):
        super().__init__(line)
        self.name = name


class QualSeg(Node):
    def __init__(self, name, field, index, line=0):
        super().__init__(line)
        self.name = name
        self.field = field
        self.index = index


class ExprSeg(Node):
    def __init__(self, expr, line=0):
        super().__init__(line)
        self.expr = expr


class MergeSeg(Node):
    pass


class FoldSeg(Node):
    def __init__(self, gate_name, line=0):
        super().__init__(line)
        self.gate_name = gate_name


class Connection(Node):
    def __init__(self, segments, line=0):
        super().__init__(line)
        self.segments = segments


# ---- top-level definitions ----

class Import(Node):
    def __init__(self, path, line=0):
        super().__init__(line)
        self.path = path


class Port(Node):
    def __init__(self, direction, name, width, line=0):
        super().__init__(line)
        self.direction = direction  # 'in' | 'out'
        self.name = name
        self.width = width          # WidthSpec


class GateDef(Node):
    def __init__(self, name, arity, rows, line=0):
        super().__init__(line)
        self.name = name
        self.arity = arity          # NumExpr (backtick)
        self.rows = rows            # list of (pattern items, result state)


class WithItem(Node):
    def __init__(self, type_name, inst_name, init=None, line=0):
        super().__init__(line)
        self.type_name = type_name  # def name or 'register'
        self.inst_name = inst_name
        self.init = init            # literal text for register


class BlockDef(Node):
    def __init__(self, name, ports, using, body, with_items=None, line=0):
        super().__init__(line)
        self.name = name
        self.ports = ports
        self.using = using          # list of names or None (unsupported)
        self.with_items = with_items or []
        self.body = body            # list of Connection


class InputDef(Node):
    def __init__(self, key, name, init=None, line=0):
        super().__init__(line)
        self.key = key              # string
        self.name = name
        self.init = init            # literal text or None


class OutputDef(Node):
    def __init__(self, label, name, mode='changes', line=0):
        super().__init__(line)
        self.label = label
        self.name = name
        self.mode = mode            # 'changes' | 'every_step'


class ClockDef(Node):
    def __init__(self, name, period, duty, line=0):
        super().__init__(line)
        self.name = name
        self.period = period        # NumExpr
        self.duty = duty            # NumExpr


class BenchDef(Node):
    def __init__(self, name, with_items, body, line=0):
        super().__init__(line)
        self.name = name
        self.with_items = with_items
        self.body = body


class TableRow(Node):
    def __init__(self, inputs, outputs, line=0):
        super().__init__(line)
        self.inputs = inputs   # list of chain segments (one chain)
        self.outputs = outputs


class ScriptRec(Node):
    def __init__(self, tick, key, value, line=0):
        super().__init__(line)
        self.tick = tick    # NumExpr
        self.key = key
        self.value = value  # literal text


class ExpectRec(Node):
    def __init__(self, tick, phase, label, value, line=0):
        super().__init__(line)
        self.tick = tick    # NumExpr
        self.phase = phase  # None | 'init' | 'post'
        self.label = label
        self.value = value  # literal text


class TestDef(Node):
    def __init__(self, name, target=None, rows=None, until=None, script=None,
                 expect=None, with_items=None, body=None, line=0):
        super().__init__(line)
        self.name = name
        self.target = target    # block name for table tests
        self.rows = rows        # table rows
        self.until = until      # NumExpr
        self.script = script or []
        self.expect = expect or []
        self.with_items = with_items or []
        self.body = body or []

"""AST nodes for Triaina."""


class Node:
    __slots__ = ('line',)

    def __init__(self, line=0):
        self.line = line


# ---- numeric expressions (widths, bounds, arities, tick counts) ----

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
# kind: 'const' (expr) | 'inferred' | 'param' (name) | 'tuple' (items)

class WidthSpec(Node):
    def __init__(self, kind, name=None, expr=None, items=None, line=0):
        super().__init__(line)
        self.kind = kind
        self.name = name
        self.expr = expr
        self.items = items or []


# ---- slice spec ----

class SliceSpec(Node):
    def __init__(self, lo, hi, line=0):
        super().__init__(line)
        self.lo = lo   # NumExpr or None (default 0)
        self.hi = hi   # NumExpr or None (default width; -1 with lo None = reverse)


# ---- references ----
# kind: 'bare' (name_parts len 1) | 'qname' (qualified def) |
#       'qual' (instance.in.N / instance.out.N) | 'named' (@name[.member])

class Ref(Node):
    def __init__(self, kind, parts, field=None, index=None, member=None,
                 absolute=False, line=0):
        super().__init__(line)
        self.kind = kind
        self.parts = list(parts)
        self.field = field    # 'in' | 'out' for 'qual'
        self.index = index    # int or None
        self.member = member  # int or None for 'named'
        self.absolute = absolute


# ---- expressions ----

class LitExpr(Node):
    def __init__(self, text, slice=None, line=0):
        super().__init__(line)
        self.text = text
        self.slice = slice


class RefExpr(Node):
    def __init__(self, ref, slice=None, line=0):
        super().__init__(line)
        self.ref = ref
        self.slice = slice


class ConcatExpr(Node):
    def __init__(self, parts, line=0):
        super().__init__(line)
        self.parts = parts


# ---- chain segments ----

class AdapterSeg(Node):
    def __init__(self, members, line=0):
        super().__init__(line)
        self.members = members  # list of AdapterMember


class AdapterMember(Node):
    """literal | $n | @name[.i] | port/instance ref, each optionally sliced"""
    def __init__(self, base, slice=None, line=0):
        super().__init__(line)
        self.base = base  # ('lit', text) | ('usd', n) | ('named', name, member|None)
                          # | ('ref', Ref)
        self.slice = slice


class NamedSeg(Node):
    """@name, optionally annotated with widths (single or tuple)."""
    def __init__(self, name, widths=None, line=0):
        super().__init__(line)
        self.name = name
        self.widths = widths  # WidthSpec or None


class BareSeg(Node):
    """A bare identifier: local port/endpoint/instance name, or a component
    (gate/block) occurrence."""
    def __init__(self, name, line=0):
        super().__init__(line)
        self.name = name


class QNameSeg(Node):
    """A qualified component reference (a::b::name or ::a::b::name)."""
    def __init__(self, parts, absolute=False, line=0):
        super().__init__(line)
        self.parts = list(parts)
        self.absolute = absolute


class QualSeg(Node):
    """instance.in.N / instance.out.N / instance.out"""
    def __init__(self, name, field, index, line=0):
        super().__init__(line)
        self.name = name
        self.field = field
        self.index = index


class ExprSeg(Node):
    def __init__(self, expr, line=0):
        super().__init__(line)
        self.expr = expr


class Connection(Node):
    def __init__(self, segments, line=0):
        super().__init__(line)
        self.segments = segments


# ---- top-level / namespace definitions ----

class Import(Node):
    def __init__(self, path, line=0):
        super().__init__(line)
        self.path = path


class NamespaceDef(Node):
    def __init__(self, parts, defs, line=0):
        super().__init__(line)
        self.parts = list(parts)
        self.defs = defs


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
        self.arity = arity          # NumExpr
        self.rows = rows            # list of (pattern items, result state)
                                    # pattern item: ('lit', state) | ('any',)


class WithItem(Node):
    def __init__(self, type_parts, inst_name, init=None, absolute=False, line=0):
        super().__init__(line)
        self.type_parts = list(type_parts)
        self.absolute = absolute
        self.inst_name = inst_name
        self.init = init            # literal text for register


class BlockDef(Node):
    def __init__(self, name, ports, with_items, body, line=0):
        super().__init__(line)
        self.name = name
        self.ports = ports
        self.with_items = with_items or []
        self.body = body            # list of Connection


# ---- bench/test-local endpoint declarations ----

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
        self.body = body            # list of InputDef|OutputDef|ClockDef|Connection


class TableRow(Node):
    def __init__(self, inputs, outputs, line=0):
        super().__init__(line)
        self.inputs = inputs   # one chain segment
        self.outputs = outputs


class ScriptRec(Node):
    def __init__(self, tick, key, value, line=0):
        super().__init__(line)
        self.tick = tick    # NumExpr
        self.key = key
        self.value = value  # literal text


class ExpectRec(Node):
    def __init__(self, tick, label, value, line=0):
        super().__init__(line)
        self.tick = tick    # NumExpr
        self.label = label
        self.value = value  # literal text


class TestDef(Node):
    def __init__(self, name, target=None, rows=None, until=None, script=None,
                 expect=None, with_items=None, body=None, line=0):
        super().__init__(line)
        self.name = name
        self.target = target    # (parts, absolute) for table tests
        self.rows = rows        # table rows
        self.until = until      # NumExpr
        self.script = script or []
        self.expect = expect or []
        self.with_items = with_items or []
        self.body = body or []

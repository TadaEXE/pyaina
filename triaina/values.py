"""Six-state balanced-ternary values and fault provenance."""

NEG, ZERO, POS, U, E, Z = '-', '0', '+', 'u', 'e', 'z'
LOGICAL = (NEG, ZERO, POS)
ALL_STATES = (NEG, ZERO, POS, U, E, Z)
LITERAL_CHARS = frozenset('-0+uez')


class Value:
    """A trit vector plus fault provenance.

    trits: tuple of state chars, index 0 = rightmost (least significant).
    prov: tuple of origins; each origin is (tick, reason, detail).
    Signal equality ignores prov; queueing/emission rules treat it as
    change identity for `e` values.
    """

    __slots__ = ('trits', 'prov')

    def __init__(self, trits, prov=()):
        self.trits = tuple(trits)
        self.prov = tuple(prov)

    def observed(self, raw):
        """Observed trits for a reader: raw readers see z, others see u."""
        if raw:
            return self.trits
        return tuple(U if t == Z else t for t in self.trits)

    def __eq__(self, other):
        return isinstance(other, Value) and self.trits == other.trits

    def __hash__(self):
        return hash(self.trits)

    def __repr__(self):
        return 'Value(%s)' % ''.join(self.trits)


def unknown(width):
    return Value((U,) * width)


def make_literal(text, tick=0):
    """Literal value; `e` positions get explicit-fault origins."""
    prov = ()
    if E in text:
        prov = ((tick, 'literal', 'literal e in %r' % text),)
    return Value(text, prov)


def obs_normal(value):
    """z is observed as u by every non-merge reader."""
    return value.observed(raw=False)


def pad_left(value, width):
    """Zero-extend on the left; extension inserts driven 0 per position."""
    if len(value.trits) >= width:
        return value
    pad = (ZERO,) * (width - len(value.trits))
    return Value(pad + value.trits, value.prov)


def concat_values(values):
    trits = ()
    prov = ()
    for v in values:
        trits = trits + v.trits
        prov = prov + v.prov
    return Value(trits, tuple(dict.fromkeys(prov)))


def slice_value(value, lo, hi):
    return Value(value.trits[lo:hi], value.prov)


def reverse_value(value):
    return Value(value.trits[::-1], value.prov)


def merge_prov(*values):
    prov = ()
    for v in values:
        prov = prov + v.prov
    return tuple(dict.fromkeys(prov))

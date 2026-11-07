"""Recursive-descent parser for Triaina.

Numeric contexts (width annotations, slice bounds, gate arity, clock args,
`until`, script/expect tick indices) accept integer literals, width
parameters, parentheses, unary +/- and binary + - * /; the lexer's LITERAL
token `0` is read as integer 0 and single-char LITERALs `+`/`-` as operators
there. Signal contexts read them as trits.
"""

from fractions import Fraction

from . import ast_nodes as A
from .lexer import lex


class ParseError(Exception):
    pass


class Parser:
    def __init__(self, toks):
        self.toks = toks
        self.pos = 0

    @property
    def cur(self):
        return self.toks[self.pos]

    def peek(self, k=1):
        p = self.pos + k
        return self.toks[p] if p < len(self.toks) else self.toks[-1]

    def next(self):
        t = self.toks[self.pos]
        if t.kind != 'EOF':
            self.pos += 1
        return t

    def expect(self, kind, what=None):
        t = self.cur
        if t.kind != kind:
            raise ParseError('line %d: expected %s, got %s %r' % (
                t.line, what or kind, t.kind, t.value))
        return self.next()

    def at_kw(self, word):
        t = self.cur
        return t.kind == 'IDENT' and t.value == word

    # ---- numeric expressions ----

    def parse_numexpr(self):
        return self.parse_num_add()

    def parse_num_add(self):
        left = self.parse_num_mul()
        while True:
            t = self.cur
            if t.kind == 'LITERAL' and t.value in ('+', '-'):
                op = self.next().value
                right = self.parse_num_mul()
                left = A.NumBinary(op, left, right, t.line)
            else:
                return left

    def parse_num_mul(self):
        left = self.parse_num_unary()
        while True:
            t = self.cur
            if t.kind == 'STAR':
                self.next()
                right = self.parse_num_unary()
                left = A.NumBinary('*', left, right, t.line)
            elif t.kind == 'SLASH':
                self.next()
                right = self.parse_num_unary()
                left = A.NumBinary('/', left, right, t.line)
            else:
                return left

    def parse_num_unary(self):
        t = self.cur
        if t.kind == 'LITERAL' and t.value in ('+', '-'):
            op = self.next().value
            operand = self.parse_num_unary()
            return A.NumUnary(op, operand, t.line)
        return self.parse_num_primary()

    def parse_num_primary(self):
        t = self.cur
        if t.kind == 'NUM':
            self.next()
            return A.NumLit(Fraction(t.value), t.line)
        if t.kind == 'LITERAL' and t.value and all(c == '0' for c in t.value):
            self.next()
            return A.NumLit(Fraction(int(t.value)), t.line)
        if t.kind == 'IDENT':
            self.next()
            return A.NumParam(t.value, t.line)
        if t.kind == 'LPAREN':
            self.next()
            e = self.parse_numexpr()
            self.expect('RPAREN', ')')
            return e
        raise ParseError('line %d: expected numeric expression, got %s %r' % (
            t.line, t.kind, t.value))

    def tok_int(self, what='index'):
        t = self.cur
        if t.kind == 'NUM':
            self.next()
            return t.value
        if t.kind == 'LITERAL' and t.value and all(c == '0' for c in t.value):
            self.next()
            return int(t.value)
        raise ParseError('line %d: expected %s, got %s %r' % (t.line, what, t.kind, t.value))

    # ---- widths and slices ----

    def parse_widthspec(self):
        """After COLON: `:4` | `:?` | `:?n` | `:(n + 1)` | `:n`."""
        t = self.cur
        if t.kind == 'QMARK':
            self.next()
            name = None
            if self.cur.kind == 'IDENT':
                name = self.next().value
            return A.WidthSpec('param' if name else 'inferred', name=name, line=t.line)
        e = self.parse_numexpr()
        return A.WidthSpec('const', expr=e, line=t.line)

    def parse_width_annotation(self):
        """After COLON in a `@name:...` annotation: single widthspec or a
        tuple `@name:(1, 4, 2)`."""
        t = self.cur
        if t.kind == 'LPAREN':
            self.next()
            items = [self.parse_widthspec()]
            while self.cur.kind == 'COMMA':
                self.next()
                items.append(self.parse_widthspec())
            self.expect('RPAREN', ')')
            return A.WidthSpec('tuple', items=items, line=t.line)
        return self.parse_widthspec()

    def parse_slice(self):
        """After LBRACKET: v[i] | v[lo:hi] | v[:-1] | v[:]."""
        t = self.cur
        lo = hi = None
        if self.cur.kind not in ('COLON', 'RBRACKET'):
            lo = self.parse_numexpr()
        if self.cur.kind == 'COLON':
            self.next()
            if self.cur.kind != 'RBRACKET':
                hi = self.parse_numexpr()
        elif lo is not None:
            # v[i] selects the single trit i, i.e. [i:i+1]
            hi = A.NumBinary('+', lo, A.NumLit(Fraction(1), lo.line), lo.line)
        self.expect('RBRACKET', "']'")
        return A.SliceSpec(lo, hi, t.line)

    def parse_slice_opt(self):
        if self.cur.kind == 'LBRACKET':
            self.next()
            return self.parse_slice()
        return None

    # ---- qualified names ----

    def parse_qname(self):
        """[::] ident (:: ident)* -> (absolute, parts)."""
        t = self.cur
        absolute = False
        if t.kind == 'DCOLON':
            self.next()
            absolute = True
        parts = [self.expect('IDENT', 'name').value]
        while self.cur.kind == 'DCOLON':
            self.next()
            parts.append(self.expect('IDENT', 'name').value)
        return absolute, parts

    # ---- gate patterns ----

    def parse_pattern_item(self):
        t = self.cur
        if t.kind == 'LITERAL':
            self.next()
            items = []
            for ch in t.value:
                if ch not in '-0+':
                    raise ParseError('line %d: gate pattern positions are - 0 + or _, got %r'
                                     % (t.line, ch))
                items.append(('lit', ch))
            return items
        if t.kind == 'UNDERSCORE':
            self.next()
            return [('any',)]
        raise ParseError('line %d: bad gate pattern item %s %r' % (t.line, t.kind, t.value))

    # ---- references and expressions ----

    def parse_ref(self):
        t = self.cur
        if t.kind == 'AT':
            self.next()
            member = None
            if self.cur.kind == 'DOT':
                self.next()
                member = self.tok_int('member index')
            return A.Ref('named', [t.value], member=member, line=t.line)
        if t.kind == 'IDENT':
            self.next()
            if self.cur.kind == 'DOT':
                self.next()
                f = self.expect('IDENT', 'in/out')
                if f.value not in ('in', 'out'):
                    raise ParseError('line %d: expected in/out after dot, got %r'
                                     % (f.line, f.value))
                index = None
                if self.cur.kind == 'DOT':
                    self.next()
                    index = self.tok_int('port index')
                return A.Ref('qual', [t.value], field=f.value, index=index, line=t.line)
            return A.Ref('bare', [t.value], line=t.line)
        raise ParseError('line %d: expected reference, got %s %r' % (t.line, t.kind, t.value))

    def parse_primary(self):
        t = self.cur
        if t.kind == 'LITERAL':
            self.next()
            return A.LitExpr(t.value, slice=self.parse_slice_opt(), line=t.line)
        if t.kind == 'AT' or t.kind == 'IDENT':
            ref = self.parse_ref()
            return A.RefExpr(ref, slice=self.parse_slice_opt(), line=t.line)
        if t.kind == 'LPAREN':
            self.next()
            e = self.parse_expr()
            self.expect('RPAREN', ')')
            return e
        raise ParseError('line %d: expected expression, got %s %r' % (t.line, t.kind, t.value))

    def parse_expr(self):
        parts = [self.parse_primary()]
        while self.cur.kind == 'PERCENT':
            self.next()
            parts.append(self.parse_primary())
        if len(parts) == 1:
            return parts[0]
        return A.ConcatExpr(parts, parts[0].line)

    # ---- chain segments ----

    def parse_adapter_member(self):
        t = self.cur
        if t.kind == 'LITERAL':
            self.next()
            base = ('lit', t.value)
        elif t.kind == 'USD':
            self.next()
            base = ('usd', t.value)
        elif t.kind == 'AT':
            self.next()
            member = None
            if self.cur.kind == 'DOT':
                self.next()
                member = self.tok_int('member index')
            base = ('named', t.value, member)
        elif t.kind == 'IDENT':
            base = ('ref', self.parse_ref())
        else:
            raise ParseError('line %d: bad adapter member %s %r' % (t.line, t.kind, t.value))
        return A.AdapterMember(base, slice=self.parse_slice_opt(), line=t.line)

    def parse_segment(self):
        t = self.cur
        if t.kind == 'PIPE':
            self.next()
            members = [self.parse_adapter_member()]
            while self.cur.kind == 'COMMA':
                self.next()
                members.append(self.parse_adapter_member())
            self.expect('PIPE', '|')
            return A.AdapterSeg(members, t.line)
        if t.kind == 'AT':
            if self.peek().kind in ('DOT', 'LBRACKET'):
                return A.ExprSeg(self.parse_primary(), t.line)
            self.next()
            widths = None
            if self.cur.kind == 'COLON':
                self.next()
                widths = self.parse_width_annotation()
            return A.NamedSeg(t.value, widths=widths, line=t.line)
        if t.kind in ('LITERAL', 'LPAREN'):
            return A.ExprSeg(self.parse_expr(), t.line)
        if t.kind == 'DCOLON':
            absolute, parts = self.parse_qname()
            return A.QNameSeg(parts, absolute=absolute, line=t.line)
        if t.kind == 'IDENT':
            if self.peek().kind == 'DCOLON':
                absolute, parts = self.parse_qname()
                return A.QNameSeg(parts, absolute=absolute, line=t.line)
            if self.peek().kind == 'DOT':
                name = self.next().value
                self.expect('DOT')
                f = self.expect('IDENT', 'in/out')
                if f.value not in ('in', 'out'):
                    raise ParseError('line %d: expected in/out after dot, got %r'
                                     % (f.line, f.value))
                index = None
                if self.cur.kind == 'DOT':
                    self.next()
                    index = self.tok_int('port index')
                if self.cur.kind in ('PERCENT', 'LBRACKET'):
                    ref = A.Ref('qual', [name], field=f.value, index=index, line=t.line)
                    parts = [A.RefExpr(ref, line=t.line)]
                    while self.cur.kind == 'PERCENT':
                        self.next()
                        parts.append(self.parse_primary())
                    expr = parts[0] if len(parts) == 1 else A.ConcatExpr(parts, t.line)
                    if self.cur.kind == 'LBRACKET':
                        raise ParseError('line %d: slice of a concatenation is not supported'
                                         % t.line)
                    return A.ExprSeg(expr, t.line)
                return A.QualSeg(name, f.value, index, t.line)
            if self.peek().kind in ('PERCENT', 'LBRACKET'):
                return A.ExprSeg(self.parse_expr(), t.line)
            self.next()
            return A.BareSeg(t.value, t.line)
        raise ParseError('line %d: bad chain segment %s %r' % (t.line, t.kind, t.value))

    def parse_connection(self):
        t = self.cur
        segs = [self.parse_segment()]
        while self.cur.kind == 'RARROW':
            self.next()
            segs.append(self.parse_segment())
        self.expect('SEMI', ';')
        return A.Connection(segs, t.line)

    # ---- bodies ----

    def parse_input(self):
        t = self.expect('IDENT')  # input
        self.expect('LPAREN')
        key = self.expect('QSTRING', 'input key')
        init = None
        if self.cur.kind == 'COMMA':
            self.next()
            init = self.expect('LITERAL', 'initializer').value
        self.expect('RPAREN')
        name = self.expect('IDENT', 'input name')
        self.expect('SEMI')
        return A.InputDef(key.value, name.value, init, t.line)

    def parse_output(self):
        t = self.expect('IDENT')  # output
        self.expect('LPAREN')
        label = self.expect('QSTRING', 'output label')
        mode = 'changes'
        if self.cur.kind == 'COMMA':
            self.next()
            m = self.expect('IDENT', 'emission mode')
            if m.value not in ('changes', 'every_step'):
                raise ParseError('line %d: bad emission mode %r' % (m.line, m.value))
            mode = m.value
        self.expect('RPAREN')
        name = self.expect('IDENT', 'output name')
        self.expect('SEMI')
        return A.OutputDef(label.value, name.value, mode, t.line)

    def parse_clock(self):
        t = self.expect('IDENT')  # clock
        self.expect('LPAREN')
        period = self.parse_numexpr()
        self.expect('COMMA')
        duty = self.parse_numexpr()
        self.expect('RPAREN')
        name = self.expect('IDENT', 'clock name')
        self.expect('SEMI')
        return A.ClockDef(name.value, period, duty, t.line)

    def parse_script_block(self):
        t = self.expect('IDENT')  # script
        self.expect('LBRACE')
        recs = []
        while self.cur.kind != 'RBRACE':
            tick = self.parse_numexpr()
            self.expect('COLON', ':')
            key = self.expect('IDENT', 'input key')
            val = self.expect('LITERAL', 'input value')
            self.expect('SEMI')
            recs.append(A.ScriptRec(tick, key.value, val.value, t.line))
        self.expect('RBRACE')
        return recs

    def parse_expect_block(self):
        t = self.expect('IDENT')  # expect
        self.expect('LBRACE')
        recs = []
        while self.cur.kind != 'RBRACE':
            tick = self.parse_numexpr()
            self.expect('COLON', ':')
            label = self.expect('IDENT', 'output label')
            val = self.expect('LITERAL', 'expected value')
            self.expect('SEMI')
            recs.append(A.ExpectRec(tick, label.value, val.value, t.line))
        self.expect('RBRACE')
        return recs

    def parse_body(self, allow_endpoints=False, allow_script_expect=False):
        self.expect('LBRACE')
        stmts = []
        script = []
        expect = []
        while self.cur.kind != 'RBRACE':
            t = self.cur
            if t.kind == 'IDENT' and allow_endpoints and t.value == 'input':
                stmts.append(self.parse_input())
                continue
            if t.kind == 'IDENT' and allow_endpoints and t.value == 'output':
                stmts.append(self.parse_output())
                continue
            if t.kind == 'IDENT' and allow_endpoints and t.value == 'clock':
                stmts.append(self.parse_clock())
                continue
            if t.kind == 'IDENT' and allow_script_expect and t.value == 'script':
                script.extend(self.parse_script_block())
                continue
            if t.kind == 'IDENT' and allow_script_expect and t.value == 'expect':
                expect.extend(self.parse_expect_block())
                continue
            stmts.append(self.parse_connection())
        self.expect('RBRACE')
        return stmts, script, expect

    # ---- definitions ----

    def parse_with(self):
        t = self.expect('IDENT')  # with
        self.expect('LPAREN')
        items = []
        while True:
            absolute, parts = self.parse_qname()
            if parts == ['register'] and not absolute:
                self.expect('LPAREN')
                lit = self.expect('LITERAL', 'register initializer')
                self.expect('RPAREN')
                name = self.expect('IDENT', 'instance name')
                items.append(A.WithItem(parts, name.value, init=lit.value,
                                        absolute=absolute, line=t.line))
            else:
                name = self.expect('IDENT', 'instance name')
                items.append(A.WithItem(parts, name.value, absolute=absolute, line=t.line))
            if self.cur.kind == 'COMMA':
                self.next()
                continue
            break
        self.expect('RPAREN')
        return items

    def parse_gate(self):
        t = self.expect('IDENT')  # gate
        self.expect('LPAREN')
        arity = self.parse_numexpr()
        self.expect('RPAREN')
        name = self.expect('IDENT', 'gate name')
        self.expect('LBRACE')
        rows = []
        while self.cur.kind != 'RBRACE':
            self.expect('LPAREN')
            items = []
            while self.cur.kind != 'RPAREN':
                items.extend(self.parse_pattern_item())
                if self.cur.kind == 'COMMA':
                    self.next()
                    continue
                break
            self.expect('RPAREN')
            self.expect('RARROW', '>> in gate row')
            res = self.expect('LITERAL', 'gate result')
            if len(res.value) != 1 or res.value not in '-0+z':
                raise ParseError('line %d: gate result must be one of - 0 + z, got %r'
                                 % (res.line, res.value))
            self.expect('SEMI')
            rows.append((items, res.value))
        self.expect('RBRACE')
        if not rows:
            raise ParseError('line %d: gate needs rows' % t.line)
        return A.GateDef(name.value, arity, rows, t.line)

    def parse_ports(self):
        self.expect('LPAREN')
        ports = []
        if self.cur.kind == 'RPAREN':
            self.next()
            return ports
        while True:
            d = self.expect('IDENT', 'in/out')
            if d.value not in ('in', 'out'):
                raise ParseError('line %d: expected in/out, got %r' % (d.line, d.value))
            name = self.expect('IDENT', 'port name')
            self.expect('COLON', ':')
            w = self.parse_widthspec()
            ports.append(A.Port(d.value, name.value, w, d.line))
            if self.cur.kind == 'COMMA':
                self.next()
                continue
            break
        self.expect('RPAREN')
        return ports

    def parse_block(self):
        t = self.expect('IDENT')  # block
        ports = self.parse_ports()
        name = self.expect('IDENT', 'block name')
        with_items = self.parse_with() if self.at_kw('with') else []
        body, _, _ = self.parse_body(allow_endpoints=False)
        return A.BlockDef(name.value, ports, with_items, body, t.line)

    def parse_bench(self):
        t = self.expect('IDENT')  # bench
        self.expect('LPAREN')
        name = self.expect('QSTRING', 'bench name')
        self.expect('RPAREN')
        with_items = self.parse_with() if self.at_kw('with') else []
        body, _, _ = self.parse_body(allow_endpoints=True)
        return A.BenchDef(name.value, with_items, body, t.line)

    def parse_test(self):
        t = self.expect('IDENT')  # test
        self.expect('LPAREN')
        name = self.expect('QSTRING', 'test name')
        self.expect('RPAREN')
        if self.at_kw('until'):
            self.next()
            until = self.parse_numexpr()
            with_items = self.parse_with() if self.at_kw('with') else []
            body, script, expect = self.parse_body(allow_endpoints=True,
                                                   allow_script_expect=True)
            return A.TestDef(name.value, until=until, script=script, expect=expect,
                             with_items=with_items, body=body, line=t.line)
        absolute, parts = self.parse_qname()
        self.expect('LBRACE')
        rows = []
        while self.cur.kind != 'RBRACE':
            self.expect('LPAREN')
            ins = self.parse_segment()
            self.expect('RPAREN')
            self.expect('RARROW', '>> in test row')
            self.expect('LPAREN')
            outs = self.parse_segment()
            self.expect('RPAREN')
            self.expect('SEMI')
            rows.append(A.TableRow(ins, outs, t.line))
        self.expect('RBRACE')
        if not rows:
            raise ParseError('line %d: table test needs rows' % t.line)
        return A.TestDef(name.value, target=(parts, absolute), rows=rows, line=t.line)

    def parse_namespace(self):
        t = self.expect('IDENT')  # namespace
        absolute, parts = self.parse_qname()
        if absolute:
            raise ParseError('line %d: namespace name cannot be absolute' % t.line)
        self.expect('LBRACE')
        defs = []
        while self.cur.kind != 'RBRACE':
            defs.append(self.parse_toplevel())
        self.expect('RBRACE')
        return A.NamespaceDef(parts, defs, t.line)

    def parse_toplevel(self):
        t = self.cur
        if t.kind != 'IDENT':
            raise ParseError('line %d: unexpected top-level %s %r' % (t.line, t.kind, t.value))
        word = t.value
        if word == 'import':
            self.next()
            path = self.expect('QSTRING', 'import path')
            self.expect('SEMI')
            return A.Import(path.value, t.line)
        if word == 'namespace':
            return self.parse_namespace()
        if word == 'gate':
            return self.parse_gate()
        if word == 'block':
            return self.parse_block()
        if word == 'bench':
            return self.parse_bench()
        if word == 'test':
            return self.parse_test()
        if word in ('input', 'output', 'clock'):
            raise ParseError('line %d: %s declarations are only allowed inside a bench '
                             'or test body' % (t.line, word))
        raise ParseError('line %d: unexpected top-level keyword %r' % (t.line, word))

    def parse_file(self):
        defs = []
        while self.cur.kind != 'EOF':
            defs.append(self.parse_toplevel())
        return defs


def parse_text(text, file='<string>'):
    defs = Parser(lex(text)).parse_file()
    for d in defs:
        d.file = file
    return defs


def parse_file(path):
    with open(path, 'r') as f:
        text = f.read()
    return parse_text(text, file=path)

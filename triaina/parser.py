"""Recursive-descent parser for Triaina."""

from fractions import Fraction

from . import ast_nodes as A
from .lexer import lex, lex_numexpr


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
        while self.cur.kind == 'NUMOP' and self.cur.value in '+-':
            op = self.next().value
            right = self.parse_num_mul()
            left = A.NumBinary(op, left, right, left.line)
        return left

    def parse_num_mul(self):
        left = self.parse_num_unary()
        while self.cur.kind == 'NUMOP' and self.cur.value in '*/':
            op = self.next().value
            right = self.parse_num_unary()
            left = A.NumBinary(op, left, right, left.line)
        return left

    def parse_num_unary(self):
        t = self.cur
        if t.kind == 'NUMOP' and t.value in '+-':
            op = self.next().value
            operand = self.parse_num_unary()
            return A.NumUnary(op, operand, t.line)
        return self.parse_num_primary()

    def parse_num_primary(self):
        t = self.cur
        if t.kind == 'NUM':
            self.next()
            return A.NumLit(Fraction(t.value), t.line)
        if t.kind == 'PARAM':
            self.next()
            return A.NumParam(t.value, t.line)
        if t.kind == 'NUMOP' and t.value == '(':
            self.next()
            e = self.parse_numexpr()
            t2 = self.cur
            if t2.kind != 'NUMOP' or t2.value != ')':
                raise ParseError('line %d: expected )' % t.line)
            self.next()
            return e
        raise ParseError('line %d: expected numeric expression, got %s %r' % (
            t.line, t.kind, t.value))

    def parse_backtick(self):
        t = self.expect('NUMEXPR', 'backtick numeric')
        sub = Parser(lex_numexpr(t.value, t.line, t.col))
        e = sub.parse_numexpr()
        if sub.cur.kind != 'EOF':
            raise ParseError('line %d: trailing tokens in numeric %r' % (t.line, t.value))
        return e

    # ---- widths and slices ----

    def parse_widthspec(self):
        """Called right after COLON. Returns WidthSpec."""
        t = self.cur
        if t.kind == 'QMARK':
            self.next()
            name = None
            if self.cur.kind == 'PARAM':
                name = self.next().value
            return A.WidthSpec('param' if name else 'inferred', name=name, line=t.line)
        e = self.parse_numexpr()
        return A.WidthSpec('const', expr=e, line=t.line)

    def parse_slice(self):
        """Called right after LBRACKET. Returns SliceSpec."""
        t = self.cur
        lo = hi = None
        if self.cur.kind not in ('COLON', 'RBRACKET'):
            lo = self.parse_numexpr()
        if self.cur.kind == 'COLON':
            self.next()
            if self.cur.kind != 'RBRACKET':
                hi = self.parse_numexpr()
        else:
            if lo is not None:
                # v[i] selects the single trit i, i.e. [i:i+1]
                hi = A.NumBinary('+', lo, A.NumLit(Fraction(1), lo.line), lo.line)
        self.expect('RBRACKET', "']'")
        return A.SliceSpec(lo, hi, t.line)


    def parse_slice_opt(self):
        if self.cur.kind == 'LBRACKET':
            self.next()
            return self.parse_slice()
        return None

    # ---- patterns ----

    def parse_pattern_items(self):
        """One or more pattern positions (no commas)."""
        items = []
        while True:
            t = self.cur
            if t.kind == 'LITERAL':
                self.next()
                for ch in t.value:
                    items.append(('lit', ch))
            elif t.kind == 'QMARK':
                self.next()
                items.append(('any',))
            elif t.kind == 'UNDERSCORE':
                self.next()
                items.append(('seq',))
            else:
                break
        if not items:
            t = self.cur
            raise ParseError('line %d: empty pattern' % t.line)
        return items

    def parse_pattern(self):
        return self.parse_pattern_items()

    # ---- references and expressions ----

    def parse_ref(self):
        t = self.cur
        if t.kind == 'AT':
            self.next()
            member = None
            if self.cur.kind == 'DOT':
                self.next()
                member = self.expect('NUM', 'member index').value
            return A.Ref('named', t.value, member=member, line=t.line)
        if t.kind == 'IDENT':
            self.next()
            if self.cur.kind == 'DOT':
                self.next()
                f = self.expect('IDENT', 'in/out')
                if f.value not in ('in', 'out'):
                    raise ParseError('line %d: expected in/out after dot, got %r' % (
                        f.line, f.value))
                index = None
                if self.cur.kind == 'DOT':
                    self.next()
                    index = self.expect('NUM', 'port index').value
                return A.Ref('qual', t.value, field=f.value, index=index, line=t.line)
            return A.Ref('bare', t.value, line=t.line)
        raise ParseError('line %d: expected reference, got %s %r' % (t.line, t.kind, t.value))

    def parse_primary(self):
        t = self.cur
        if t.kind == 'LITERAL':
            self.next()
            sl = self.parse_slice_opt()
            return A.LitExpr(t.value, slice=sl, line=t.line)
        if t.kind == 'AT':
            ref = self.parse_ref()
            sl = self.parse_slice_opt()
            return A.RefExpr(ref, slice=sl, line=t.line)
        if t.kind == 'IDENT' and t.value == 'match':
            return self.parse_match()
        if t.kind == 'IDENT' and self.peek().kind == 'LPAREN':
            name = self.next().value
            self.expect('LPAREN')
            args = []
            if self.cur.kind != 'RPAREN':
                args.append(self.parse_expr())
                while self.cur.kind == 'COMMA':
                    self.next()
                    args.append(self.parse_expr())
            self.expect('RPAREN')
            return A.CallExpr(name, args, t.line)
        if t.kind == 'IDENT':
            ref = self.parse_ref()
            sl = self.parse_slice_opt()
            return A.RefExpr(ref, slice=sl, line=t.line)
        if t.kind == 'LPAREN':
            self.next()
            e = self.parse_expr()
            self.expect('RPAREN')
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

    def parse_match(self):
        t = self.expect('IDENT')  # 'match'
        sel = self.parse_expr()
        self.expect('LBRACE')
        arms = []
        while self.cur.kind != 'RBRACE':
            pat = self.parse_pattern()
            self.expect('RARROW', '>> in match arm')
            e = self.parse_expr()
            self.expect('SEMI')
            arms.append((pat, e))
        self.expect('RBRACE')
        if not arms:
            raise ParseError('line %d: match needs arms' % t.line)
        return A.MatchExpr(sel, arms, t.line)

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
                member = self.expect('NUM', 'member index').value
            base = ('named', t.value, member)
        elif t.kind == 'IDENT':
            ref = self.parse_ref()
            base = ('ref', ref)
        else:
            raise ParseError('line %d: bad adapter member %s %r' % (t.line, t.kind, t.value))
        sl = self.parse_slice_opt()
        return A.AdapterMember(base, slice=sl, line=t.line)

    def parse_segment(self):
        t = self.cur
        if t.kind == 'PIPE':
            self.next()
            members = [self.parse_adapter_member()]
            while self.cur.kind == 'COMMA':
                self.next()
                members.append(self.parse_adapter_member())
            self.expect('PIPE')
            return A.AdapterSeg(members, t.line)
        if t.kind == 'AT':
            self.next()
            width = None
            if self.cur.kind == 'COLON':
                self.next()
                width = self.parse_widthspec()
            return A.NamedSeg(t.value, width=width, line=t.line)
        if t.kind == 'IDENT':
            if t.value == 'merge' and self.peek().kind != 'LPAREN':
                self.next()
                return A.MergeSeg(t.line)
            if t.value == 'fold' and self.peek().kind == 'LPAREN':
                self.next()
                self.expect('LPAREN')
                g = self.expect('IDENT', 'gate name')
                self.expect('RPAREN')
                return A.FoldSeg(g.value, t.line)
            if t.value == 'match':
                return A.ExprSeg(self.parse_match(), t.line)
            if self.peek().kind in ('LPAREN', 'PERCENT', 'LBRACKET'):
                return A.ExprSeg(self.parse_expr(), t.line)
            if self.peek().kind == 'DOT':
                self.next()
                self.expect('DOT')
                f = self.expect('IDENT', 'in/out')
                if f.value not in ('in', 'out'):
                    raise ParseError('line %d: expected in/out after dot' % f.line)
                index = None
                if self.cur.kind == 'DOT':
                    self.next()
                    index = self.expect('NUM', 'port index').value
                if self.cur.kind == 'PERCENT':
                    ref = A.Ref('qual', t.value, field=f.value, index=index, line=t.line)
                    parts = [A.RefExpr(ref, line=t.line)]
                    while self.cur.kind == 'PERCENT':
                        self.next()
                        parts.append(self.parse_primary())
                    return A.ExprSeg(A.ConcatExpr(parts, t.line), t.line)
                return A.QualSeg(t.value, f.value, index, t.line)
            self.next()
            return A.BareSeg(t.value, t.line)
        if t.kind in ('LITERAL', 'LPAREN'):
            return A.ExprSeg(self.parse_expr(), t.line)
        raise ParseError('line %d: bad chain segment %s %r' % (t.line, t.kind, t.value))

    def parse_connection(self):
        t = self.cur
        segs = [self.parse_segment()]
        while self.cur.kind == 'RARROW':
            self.next()
            segs.append(self.parse_segment())
        self.expect('SEMI')
        return A.Connection(segs, t.line)

    # ---- bodies ----

    def parse_with(self):
        self.expect('IDENT')  # 'with'
        self.expect('LPAREN')
        items = []
        while True:
            tt = self.expect('IDENT', 'type name')
            if tt.value == 'register':
                self.expect('LPAREN')
                lit = self.expect('LITERAL', 'register initializer')
                self.expect('RPAREN')
                name = self.expect('IDENT', 'instance name')
                items.append(A.WithItem('register', name.value, init=lit.value, line=tt.line))
            else:
                name = self.expect('IDENT', 'instance name')
                items.append(A.WithItem(tt.value, name.value, line=tt.line))
            if self.cur.kind == 'COMMA':
                self.next()
                continue
            break
        self.expect('RPAREN')
        return items

    def parse_body(self, allow_script_expect=False):
        self.expect('LBRACE')
        stmts = []
        script = []
        expect = []
        while self.cur.kind != 'RBRACE':
            t = self.cur
            if allow_script_expect and t.kind == 'IDENT' and t.value == 'script':
                self.next()
                self.expect('LBRACE')
                while self.cur.kind != 'RBRACE':
                    tick = self.parse_backtick()
                    self.expect('COLON')
                    key = self.expect('IDENT', 'input key')
                    val = self.expect('LITERAL', 'input value')
                    self.expect('SEMI')
                    script.append(A.ScriptRec(tick, key.value, val.value, t.line))
                self.expect('RBRACE')
                continue
            if allow_script_expect and t.kind == 'IDENT' and t.value == 'expect':
                self.next()
                self.expect('LBRACE')
                while self.cur.kind != 'RBRACE':
                    tick = self.parse_backtick()
                    self.expect('COLON')
                    phase = None
                    if self.cur.kind == 'IDENT' and self.cur.value in ('init', 'post'):
                        phase = self.next().value
                    label = self.expect('IDENT', 'output label')
                    val = self.expect('LITERAL', 'expected value')
                    self.expect('SEMI')
                    expect.append(A.ExpectRec(tick, phase, label.value, val.value, t.line))
                self.expect('RBRACE')
                continue
            stmts.append(self.parse_connection())
        self.expect('RBRACE')
        return stmts, script, expect

    # ---- top level ----

    def parse_file(self):
        defs = []
        while self.cur.kind != 'EOF':
            t = self.cur
            if not (t.kind == 'IDENT'):
                raise ParseError('line %d: unexpected top-level %s %r' % (t.line, t.kind, t.value))
            word = t.value
            if word == 'import':
                self.next()
                path = self.expect('QSTRING', 'import path')
                self.expect('SEMI')
                d = A.Import(path.value, t.line)
                defs.append(d)
                continue
            if word == 'gate':
                defs.append(self.parse_gate())
                continue
            if word == 'block':
                defs.append(self.parse_block())
                continue
            if word == 'input':
                self.next()
                self.expect('LPAREN')
                key = self.expect('QSTRING', 'input key')
                init = None
                if self.cur.kind == 'COMMA':
                    self.next()
                    init = self.expect('LITERAL', 'initializer').value
                self.expect('RPAREN')
                name = self.expect('IDENT', 'input name')
                self.expect('SEMI')
                defs.append(A.InputDef(key.value, name.value, init, t.line))
                continue
            if word == 'output':
                self.next()
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
                defs.append(A.OutputDef(label.value, name.value, mode, t.line))
                continue
            if word == 'clock':
                self.next()
                self.expect('LPAREN')
                period = self.parse_backtick()
                self.expect('COMMA')
                duty = self.parse_backtick()
                self.expect('RPAREN')
                name = self.expect('IDENT', 'clock name')
                self.expect('SEMI')
                defs.append(A.ClockDef(name.value, period, duty, t.line))
                continue
            if word == 'bench':
                self.next()
                self.expect('LPAREN')
                name = self.expect('QSTRING', 'bench name')
                self.expect('RPAREN')
                with_items = self.parse_with() if self.at_kw('with') else []
                body, _, _ = self.parse_body()
                defs.append(A.BenchDef(name.value, with_items, body, t.line))
                continue
            if word == 'test':
                defs.append(self.parse_test())
                continue
            raise ParseError('line %d: unexpected top-level keyword %r' % (t.line, word))
        return defs

    def parse_gate(self):
        t = self.expect('IDENT')  # gate
        self.expect('LPAREN')
        arity = self.parse_backtick()
        self.expect('RPAREN')
        name = self.expect('IDENT', 'gate name')
        self.expect('LBRACE')
        rows = []
        while self.cur.kind != 'RBRACE':
            self.expect('LPAREN')
            items = []
            while True:
                if self.cur.kind == 'RPAREN':
                    break
                items.extend(self.parse_pattern_items())
                if self.cur.kind == 'COMMA':
                    self.next()
                    continue
                break
            self.expect('RPAREN')
            self.expect('RARROW')
            res = self.expect('LITERAL', 'gate result')
            if len(res.value) != 1 or res.value not in '-0+':
                raise ParseError('line %d: gate result must be one of - 0 +, got %r' % (
                    res.line, res.value))
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
            if self.cur.kind != 'COLON':
                raise ParseError('line %d: expected : after port name' % name.line)
            self.next()
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
        using = None
        if self.at_kw('using'):
            self.next()
            self.expect('LPAREN')
            using = []
            while True:
                using.append(self.expect('IDENT', 'using param').value)
                if self.cur.kind == 'COMMA':
                    self.next()
                    continue
                break
            self.expect('RPAREN')
        with_items = self.parse_with() if self.at_kw('with') else []
        body, _, _ = self.parse_body()
        return A.BlockDef(name.value, ports, using, body, with_items=with_items, line=t.line)

    def parse_test(self):
        t = self.expect('IDENT')  # test
        self.expect('LPAREN')
        name = self.expect('QSTRING', 'test name')
        self.expect('RPAREN')
        if self.at_kw('until'):
            self.next()
            until = self.parse_backtick()
            with_items = self.parse_with() if self.at_kw('with') else []
            body, script, expect = self.parse_body(allow_script_expect=True)
            return A.TestDef(name.value, until=until, script=script, expect=expect,
                             with_items=with_items, body=body, line=t.line)
        target = self.expect('IDENT', 'target block')
        self.expect('LBRACE')
        rows = []
        while self.cur.kind != 'RBRACE':
            self.expect('LPAREN')
            ins = [self.parse_segment()]
            self.expect('RPAREN')
            self.expect('RARROW')
            self.expect('LPAREN')
            outs = [self.parse_segment()]
            self.expect('RPAREN')
            self.expect('SEMI')
            rows.append(A.TableRow(ins, outs, t.line))
        self.expect('RBRACE')
        if not rows:
            raise ParseError('line %d: table test needs rows' % t.line)
        return A.TestDef(name.value, target=target.value, rows=rows, line=t.line)


def parse_text(text, file='<string>'):
    defs = Parser(lex(text)).parse_file()
    for d in defs:
        d.file = file
    return defs


def parse_file(path):
    with open(path, 'r') as f:
        text = f.read()
    return parse_text(text, file=path)

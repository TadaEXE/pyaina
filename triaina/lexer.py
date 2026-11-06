"""Lexer for Triaina.

Modes: 'sig' (signal context, default), 'num' (after ':' in signal
context: width annotations), 'brk' (inside '[' ... ']' slice bounds).
Backtick regions lex as a single NUMEXPR token whose raw text is parsed
later as a numeric expression.
"""

from .values import LITERAL_CHARS

KEYWORDS = {
    'gate', 'block', 'with', 'using', 'in', 'out', 'match', 'register',
    'clock', 'input', 'output', 'bench', 'test', 'script', 'expect',
    'until', 'import',
}

WORD_CHARS = frozenset('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_')


class Tok:
    __slots__ = ('kind', 'value', 'line', 'col')

    def __init__(self, kind, value, line, col):
        self.kind = kind
        self.value = value
        self.line = line
        self.col = col

    def __repr__(self):
        return 'Tok(%s,%r@%d:%d)' % (self.kind, self.value, self.line, self.col)


class LexError(Exception):
    pass


SINGLE = {
    '%': 'PERCENT', '|': 'PIPE', '(': 'LPAREN', ')': 'RPAREN',
    '{': 'LBRACE', '}': 'RBRACE', '[': 'LBRACKET', ']': 'RBRACKET',
    ',': 'COMMA', ';': 'SEMI', ':': 'COLON', '.': 'DOT', '=': 'EQ',
    '?': 'QMARK',
}


def lex(text):
    toks = []
    i = 0
    n = len(text)
    line = 1
    col = 1
    mode = 'sig'
    depth = 0  # paren depth inside num/brk mode

    def adv(k=1):
        nonlocal i, line, col
        for _ in range(k):
            if i < n and text[i] == '\n':
                line += 1
                col = 1
            else:
                col += 1
            i += 1

    def emit(kind, value, l, c):
        toks.append(Tok(kind, value, l, c))

    while i < n:
        ch = text[i]
        if ch in ' \t\r\n':
            adv()
            continue
        if ch == '/' and i + 1 < n and text[i + 1] == '/':
            while i < n and text[i] != '\n':
                adv()
            continue
        l, c = line, col
        if mode == 'sig':
            if ch == '`':
                adv()
                start = i
                while i < n and text[i] != '`':
                    adv()
                if i >= n:
                    raise LexError('unterminated backtick at %d:%d' % (l, c))
                raw = text[start:i]
                adv()
                emit('NUMEXPR', raw, l, c)
                continue
            if ch == '[':
                emit('LBRACKET', '[', l, c)
                adv()
                mode = 'brk'
                depth = 0
                continue
            if ch == ':':
                prev = toks[-1].kind if toks else None
                emit('COLON', ':', l, c)
                adv()
                if prev in ('IDENT', 'AT'):
                    mode = 'num'
                    depth = 0
                continue
            if ch == '"' or ch == "'":
                q = ch
                adv()
                start = i
                while i < n and text[i] != q:
                    adv()
                if i >= n:
                    raise LexError('unterminated string at %d:%d' % (l, c))
                emit('QSTRING', text[start:i], l, c)
                adv()
                continue
            if ch == '@':
                adv()
                start = i
                while i < n and text[i] in WORD_CHARS:
                    adv()
                if start == i:
                    raise LexError('empty @name at %d:%d' % (l, c))
                emit('AT', text[start:i], l, c)
                continue
            if ch == '$':
                adv()
                start = i
                while i < n and text[i].isdigit():
                    adv()
                if start == i:
                    raise LexError('empty $n at %d:%d' % (l, c))
                emit('USD', int(text[start:i]), l, c)
                continue
            if ch == '>':
                if i + 1 < n and text[i + 1] == '>':
                    emit('RARROW', '>>', l, c)
                    adv(2)
                else:
                    raise LexError('unexpected > at %d:%d' % (l, c))
                continue
            if ch in '-+':
                # pure literal run (cannot start an identifier)
                start = i
                while i < n and text[i] in LITERAL_CHARS:
                    adv()
                emit('LITERAL', text[start:i], l, c)
                continue
            if ch == '_':
                emit('UNDERSCORE', '_', l, c)
                adv()
                continue
            if ch in WORD_CHARS:
                start = i
                while i < n and text[i] in WORD_CHARS:
                    adv()
                word = text[start:i]
                # longest-token: favor literal on ties with word length
                j = start
                while j < n and text[j] in LITERAL_CHARS:
                    j += 1
                lit = text[start:j]
                if len(lit) >= len(word) or (lit and j < n and text[j] == '_'):
                    emit('LITERAL', lit, l, c)
                    i = j
                    col = c + len(lit)
                    continue
                if word[0].isdigit():
                    raise LexError('bare number %r in signal context at %d:%d' % (word, l, c))
                emit('IDENT', word, l, c)
                continue
            if ch == '.':
                emit('DOT', '.', l, c)
                adv()
                if i < n and text[i].isdigit():
                    start = i
                    while i < n and text[i].isdigit():
                        adv()
                    emit('NUM', int(text[start:i]), l, c)
                continue
            if ch in SINGLE:
                emit(SINGLE[ch], ch, l, c)
                adv()
                continue
            raise LexError('unexpected %r at %d:%d' % (ch, l, c))
        else:  # num or brk mode
            if ch.isdigit():
                start = i
                while i < n and text[i].isdigit():
                    adv()
                emit('NUM', int(text[start:i]), l, c)
                continue
            if ch in WORD_CHARS:
                start = i
                while i < n and text[i] in WORD_CHARS:
                    adv()
                emit('PARAM', text[start:i], l, c)
                continue
            if ch in '+-*/':
                emit('NUMOP', ch, l, c)
                adv()
                continue
            if ch == '(':
                emit('NUMOP', '(', l, c)
                adv()
                depth += 1
                continue
            if ch == ')':
                if depth > 0:
                    emit('NUMOP', ')', l, c)
                    adv()
                    depth -= 1
                elif mode == 'num':
                    emit('RPAREN', ')', l, c)
                    adv()
                    mode = 'sig'
                else:
                    raise LexError('unexpected ) in slice at %d:%d' % (l, c))
                continue
            if ch == '?' and mode == 'num':
                emit('QMARK', '?', l, c)
                adv()
                continue
            if ch == ':' and mode == 'brk':
                emit('COLON', ':', l, c)
                adv()
                continue
            if ch == ']' and mode == 'brk' and depth == 0:
                emit('RBRACKET', ']', l, c)
                adv()
                mode = 'sig'
                continue
            # terminators: hand the char back to signal mode
            mode = 'sig'
            depth = 0
            continue
    if mode != 'sig':
        raise LexError('unterminated numeric context at EOF')
    toks.append(Tok('EOF', None, line, col))
    return toks


def lex_numexpr(raw, line=0, col=0):
    """Lex the raw text inside backticks as a numeric expression."""
    toks = []
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch in ' \t\r\n':
            i += 1
            continue
        if ch.isdigit():
            start = i
            while i < n and raw[i].isdigit():
                i += 1
            toks.append(Tok('NUM', int(raw[start:i]), line, col))
            continue
        if ch in WORD_CHARS:
            start = i
            while i < n and raw[i] in WORD_CHARS:
                i += 1
            toks.append(Tok('PARAM', raw[start:i], line, col))
            continue
        if ch in '+-*/()':
            toks.append(Tok('NUMOP', ch, line, col))
            i += 1
            continue
        raise LexError('bad char %r in numeric expression %r' % (ch, raw))
    toks.append(Tok('EOF', None, line, col))
    return toks

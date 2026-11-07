"""Lexer for Triaina.

The lexer is context-free: whether a token such as `0` is an integer or a
trit is decided by the parser (numeric vs signal context). Literal and
identifier recognition follows longest-token rules; `u`, `e`, and `z` are
reserved trit symbols, so an equally long literal run wins the tie.
"""

from .values import LITERAL_CHARS

WORD_CHARS = frozenset('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_')
DIGITS = frozenset('0123456789')

KEYWORDS = {
    'gate', 'block', 'with', 'namespace', 'in', 'out', 'register', 'clock',
    'input', 'output', 'bench', 'test', 'script', 'expect', 'until', 'import',
}

PUNCT = {
    '%': 'PERCENT', '|': 'PIPE', '(': 'LPAREN', ')': 'RPAREN',
    '{': 'LBRACE', '}': 'RBRACE', '[': 'LBRACKET', ']': 'RBRACKET',
    ',': 'COMMA', ';': 'SEMI', '.': 'DOT', '?': 'QMARK',
    '*': 'STAR', '/': 'SLASH',
}


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


def lex(text):
    toks = []
    i = 0
    n = len(text)
    line = 1
    col = 1

    def adv(k=1):
        nonlocal i, line, col
        for _ in range(k):
            if i < n and text[i] == '\n':
                line += 1
                col = 1
            else:
                col += 1
            i += 1

    def run(chars):
        j = i
        while j < n and text[j] in chars:
            j += 1
        return text[i:j], j

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
        if ch == '"' or ch == "'":
            q = ch
            adv()
            start = i
            while i < n and text[i] != q:
                adv()
            if i >= n:
                raise LexError('unterminated string at %d:%d' % (l, c))
            toks.append(Tok('QSTRING', text[start:i], l, c))
            adv()
            continue
        if ch == '@':
            adv()
            word, j = run(WORD_CHARS)
            if not word:
                raise LexError('empty @name at %d:%d' % (l, c))
            toks.append(Tok('AT', word, l, c))
            i, col = j, c + 1 + len(word)
            continue
        if ch == '$':
            adv()
            num, j = run(DIGITS)
            if not num:
                raise LexError('empty $n at %d:%d' % (l, c))
            toks.append(Tok('USD', int(num), l, c))
            i, col = j, c + 1 + len(num)
            continue
        if ch == '>':
            if i + 1 < n and text[i + 1] == '>':
                toks.append(Tok('RARROW', '>>', l, c))
                adv(2)
            else:
                raise LexError('unexpected > at %d:%d' % (l, c))
            continue
        if ch == ':':
            if i + 1 < n and text[i + 1] == ':':
                toks.append(Tok('DCOLON', '::', l, c))
                adv(2)
            else:
                toks.append(Tok('COLON', ':', l, c))
                adv()
            continue
        if ch in LITERAL_CHARS or ch in DIGITS:
            lit, j1 = run(LITERAL_CHARS)
            num, j2 = run(DIGITS)
            word, j3 = run(WORD_CHARS)
            if word and word[0] in DIGITS:
                word = ''  # identifiers cannot start with a digit
            cands = [(len(lit), 0, 'LITERAL', lit, j1),
                     (len(num), 1, 'NUM', num, j2),
                     (len(word), 2, 'IDENT', word, j3)]
            cands = [c for c in cands if c[0] > 0]
            cands.sort(key=lambda c: (-c[0], c[1]))
            ln, _, kind, val, j = cands[0]
            if kind == 'NUM':
                val = int(val)
            toks.append(Tok(kind, val, l, c))
            i, col = j, c + ln
            continue
        if ch in WORD_CHARS:
            word, j = run(WORD_CHARS)
            if word == '_':
                toks.append(Tok('UNDERSCORE', '_', l, c))
            else:
                toks.append(Tok('IDENT', word, l, c))
            i, col = j, c + len(word)
            continue
        if ch in PUNCT:
            toks.append(Tok(PUNCT[ch], ch, l, c))
            adv()
            continue
        raise LexError('unexpected %r at %d:%d' % (ch, l, c))
    toks.append(Tok('EOF', None, line, col))
    return toks

"""Routing rule language: `when <cond> go anthropic|valar|local`, one rule per line. Pure, no eval."""
import re
from dataclasses import dataclass

FIELDS = {"text": "text", "tag": "tags", "tier": "word", "model": "text",
          "tokens": "number", "subagent": "bool", "servertool": "bool", "label": "word"}
KEYWORDS = {"when", "go", "and", "or", "not", "is", "contains", "matches"}
_TOK = re.compile(r'\s*(?:(?P<op>>=|<=|>|<)|(?P<par>[()])|(?P<word>#?[A-Za-z0-9_.-]+)|(?P<str>")|(?P<re>/))')
MAX_TERMS = 200  # comparisons + nots per line; keeps evaluate's recursion far from Python's limit
_NUM = re.compile(r"(\d+(?:\.\d+)?)(k?)$", re.I)


class RuleError(ValueError):
    def __init__(self, message, line, col):
        super().__init__(f"line {line}, col {col}: {message}")
        self.message, self.line, self.col = message, line, col


@dataclass(frozen=True)
class Line:
    cond: object
    track: str


def _lex(s, ln):
    """-> [(kind, value, col)]; kinds: op par word str re. Regex value = (pattern, flags)."""
    out, i = [], 0
    while True:
        m = _TOK.match(s, i)
        if not m:
            if s[i:].strip():
                r = s[i:].lstrip()
                raise RuleError(f"unexpected {r[0]!r}", ln, len(s) - len(r) + 1)
            return out
        col, kind = m.start(m.lastgroup) + 1, m.lastgroup
        i = m.end()
        if kind == "str":
            val, esc = "", False
            while i < len(s) and (esc or s[i] != '"'):
                if not esc and s[i] == "\\":
                    esc = True
                else:
                    val, esc = val + s[i], False
                i += 1
            if i >= len(s):
                raise RuleError("unterminated string", ln, col)
            i += 1
            out.append(("str", val, col))
        elif kind == "re":
            j = i
            while j < len(s) and s[j] != "/":
                j += 2 if s[j] == "\\" else 1
            if j >= len(s):
                raise RuleError("unterminated regex", ln, col)
            k = j + 1
            while k < len(s) and s[k].isalpha():
                k += 1
            out.append(("re", (s[i:j], s[j + 1:k]), col))
            i = k
        else:
            out.append((kind, m.group(kind), col))


class _P:
    def __init__(self, toks, ln):
        self.t, self.i, self.ln, self.n = toks, 0, ln, 0

    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else None

    def err(self, msg, tok=None):
        tok = tok or self.peek()
        raise RuleError(msg, self.ln, tok[2] if tok else self._end())

    def _end(self):
        return self.t[-1][2] + len(str(self.t[-1][1])) + 1 if self.t else 1  # ponytail: approximate for quoted tokens

    def kw(self, w):
        t = self.peek()
        return t and t[0] == "word" and t[1].lower() == w

    def eat(self, w):
        if not self.kw(w):
            self.err(f"expected '{w}'")
        self.i += 1

    def next(self):
        t = self.peek()
        self.i += 1
        return t

    def or_(self):
        a = self.and_()
        while self.kw("or"):
            self.i += 1
            a = ("or", a, self.and_())
        return a

    def and_(self):
        a = self.not_()
        while self.kw("and"):
            self.i += 1
            a = ("and", a, self.not_())
        return a

    def term(self):
        self.n += 1
        if self.n > MAX_TERMS:
            self.err("rule is too long")

    def not_(self):
        self.term()
        if self.kw("not"):
            self.i += 1
            return ("not", self.not_())
        return self.atom()

    def atom(self):
        t = self.peek()
        if t and t[0] == "par" and t[1] == "(":
            self.i += 1
            c = self.or_()
            t = self.peek()
            if not (t and t[0] == "par" and t[1] == ")"):
                self.err("expected ')'")
            self.i += 1
            return c
        if not t or t[0] != "word" or t[1].lower() in KEYWORDS:
            self.err("expected a field name")
        field = t[1].lower()
        if field not in FIELDS:
            self.err(f"unknown field '{t[1]}'")
        self.i += 1
        typ, op = FIELDS[field], self.peek()
        is_op = op and (op[0] == "op" or (op[0] == "word" and op[1].lower() in ("is", "contains", "matches")))
        if typ == "bool" and not is_op:
            return ("cmp", field, "is", True)
        if not is_op:
            self.err("expected 'is', 'contains', 'matches' or a comparison")
        self.i += 1
        o = op[1].lower()
        if o in ("contains", "matches") and typ != "text":
            self.err(f"'{o}' needs a text field", op)
        if o in (">", "<", ">=", "<=") and typ != "number":
            self.err(f"'{o}' needs a number field", op)
        v = self.next()
        if o == "matches":
            if not v or v[0] != "re":
                self.err("expected /regex/", v)
            pat, fl = v[1]
            if set(fl) - set("ims"):
                self.err("bad regex flags (use i, m, s)", v)
            try:
                return ("cmp", field, o, re.compile(pat, sum({"i": re.I, "m": re.M, "s": re.S}[f] for f in set(fl))))
            except re.error as e:
                self.err(f"bad regex: {e}", v)
        if not v or v[0] not in ("word", "str") or (v[0] == "word" and v[1].lower() in KEYWORDS and o != "is"):
            self.err("expected a value", v)
        val = v[1]
        if typ == "number":
            m = _NUM.match(val)
            if not m:
                self.err("expected a number", v)
            return ("cmp", field, o, float(m[1]) * (1000 if m[2] else 1))
        if typ == "bool":
            if val.lower() not in ("yes", "no"):
                self.err("expected 'yes' or 'no'", v)
            return ("cmp", field, "is", val.lower() == "yes")
        if typ == "tags":
            val = val.lstrip("#")
        return ("cmp", field, o, val.lower())


def parse(src):
    lines = [(n, s) for n, s in enumerate(src.split("\n"), 1) if s.strip()]
    if not lines:
        raise RuleError("expected 'when'", 1, 1)
    out = []
    for n, s in lines:
        p = _P(_lex(s, n), n)
        p.eat("when")
        c = p.or_()
        p.eat("go")
        t = p.peek()
        if not t or t[0] != "word" or t[1].lower() not in ("anthropic", "valar", "local"):
            p.err("expected anthropic, valar or local")
        p.i += 1
        if p.peek():
            p.err("unexpected text after track")
        out.append(Line(c, t[1].lower()))
    return out


def evaluate(cond, get):
    k = cond[0]
    if k == "or":
        return evaluate(cond[1], get) or evaluate(cond[2], get)
    if k == "and":
        return evaluate(cond[1], get) and evaluate(cond[2], get)
    if k == "not":
        return not evaluate(cond[1], get)
    _, f, o, v = cond
    x = get(f)
    if FIELDS[f] == "bool":
        return bool(x) == v
    if FIELDS[f] == "tags":
        return v in x
    if FIELDS[f] == "number":
        return {"is": x == v, ">": x > v, "<": x < v, ">=": x >= v, "<=": x <= v}[o]
    if o == "matches":
        return v.search(x) is not None
    x = str(x).lower()
    return v in x if o == "contains" else x == v


def fields_used(lines):
    def walk(c):
        return walk(c[1]) | (walk(c[2]) if len(c) > 2 else set()) if c[0] != "cmp" else {c[1]}
    return set().union(*(walk(l.cond) for l in lines))

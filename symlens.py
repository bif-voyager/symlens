#!/usr/bin/env python3
"""symlens — symbol-level code navigation for coding agents, standard library only.

Instead of reading a whole 4000-line file, an agent asks for what it needs:

  outline <file>            classes, functions, methods, fields with line ranges
  body <file> <name>        the source of one symbol (``Cls.method``, ``method``, ``func.inner``)
  find <name>               where a symbol is defined, across the tree
  refs <name>               where a name is used, each hit tagged with its enclosing symbol
  serve                     the same four operations as an MCP server over stdio (Claude Code, etc.)

Languages: Python (``ast``), Java and TypeScript/JavaScript (small tokenizers that skip comments, strings,
template literals and regex and match braces). Other files are ignored by outline/find; refs search any extension.

A ``.symlens.json`` in the root may list ``exclude`` globs (paths relative to the root, ``**`` allowed)
for copies, archives and vendored code that would otherwise duplicate results.
"""
import argparse
import ast
import fnmatch
import json
import os
import re
import sys
import warnings

__version__ = "0.2.0"

SKIP_DIRS = {".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules", "__pycache__", ".gitnexus", ".serena",
             ".idea", ".vscode", ".gradle", ".mypy_cache", ".pytest_cache", ".tox", "build", "target", "dist", "out"}
LANG_BY_EXT = {".py": "python", ".java": "java", ".ts": "ts", ".tsx": "ts", ".mts": "ts", ".cts": "ts",
               ".js": "ts", ".jsx": "ts", ".mjs": "ts", ".cjs": "ts"}
MAX_BODY_LINES = 300
MAX_RESULTS = 60


class Sym:
    __slots__ = ("qual", "kind", "start", "end", "sig", "doc")

    def __init__(self, qual, kind, start, end, sig, doc=""):
        self.qual, self.kind, self.start, self.end, self.sig, self.doc = qual, kind, start, end, sig, doc

    @property
    def name(self):
        return self.qual.rsplit(".", 1)[-1]

    def line(self, rel=None):
        loc = "%s:%d-%d" % (rel, self.start, self.end) if rel else "%d-%d" % (self.start, self.end)
        return "%s %s %s%s" % (loc, self.kind, self.qual, (" — " + self.sig) if self.sig else "")


# ───────────────────────────────────────────────────────────────────────────── Python
def _py_sig(lines, node):
    """Header text of a def/class from the source (ast.unparse is ~10x slower): up to the first ':' at depth 0."""
    first = node.lineno
    last = node.body[0].lineno - 1 if node.body and node.body[0].lineno > first else first
    text = " ".join(ln.strip() for ln in lines[first - 1:last])
    depth = 0
    for k, ch in enumerate(text):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == ":" and depth == 0:
            return re.sub(r"\s+", " ", text[:k]).strip()
    return re.sub(r"\s+", " ", text).strip()


def outline_python(src):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")                   # invalid escapes etc. in old code are not our business
        tree = ast.parse(src)
    lines = src.splitlines()
    out = []

    def walk(node, prefix, in_class):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                start = min([d.lineno for d in ch.decorator_list] + [ch.lineno])
                is_cls = isinstance(ch, ast.ClassDef)
                kind = "class" if is_cls else ("method" if in_class else "function")
                doc = (ast.get_docstring(ch, clean=False) or "").strip().split("\n", 1)[0][:160]
                out.append(Sym(prefix + ch.name, kind, start, ch.end_lineno, _py_sig(lines, ch), doc))
                walk(ch, prefix + ch.name + ".", is_cls)
            elif isinstance(ch, (ast.Assign, ast.AnnAssign)) and (node is tree or in_class):
                targets = ch.targets if isinstance(ch, ast.Assign) else [ch.target]
                for t in targets:
                    if isinstance(t, ast.Name):
                        out.append(Sym(prefix + t.id, "var", ch.lineno, ch.end_lineno, ""))
    walk(tree, "", False)
    return out


# ───────────────────────────────────────────────────────────────────────────── Java
_TYPE_KW = {"class", "interface", "enum", "record"}
_JAVA_KW = {"if", "for", "while", "switch", "catch", "synchronized", "return", "new", "throw", "else", "try",
            "do", "case", "default", "assert", "super", "this"}


_JAVA_LEX = re.compile(r'''
    (?P<nl>\n)
  | [ \t\r\f]+
  | //[^\n]*
  | (?P<bc>/\*.*?\*/)
  | (?P<tb>""".*?(?<!\\)""")
  | (?P<str>"(?:[^"\\\n]|\\.)*"?)
  | (?P<chr>'(?:[^'\\\n]|\\.)*'?)
  | (?P<id>[\w$]+)
  | (?P<p>.)
''', re.S | re.X)


def _java_tokens(src):
    """[(token, line)] without comments; a string, text block or char literal becomes one placeholder token."""
    toks, line = [], 1
    for m in _JAVA_LEX.finditer(src):
        kind = m.lastgroup
        if kind == "nl":
            line += 1
        elif kind == "id" or kind == "p":
            toks.append((m.group(), line))
        elif kind in ("str", "chr"):
            toks.append(('""' if kind == "str" else "''", line))
        elif kind in ("bc", "tb"):
            if kind == "tb":
                toks.append(('""', line))
            line += m.group().count("\n")
    return toks


def _strip_annotations(h):
    out, i = [], 0
    while i < len(h):
        if h[i][0] == "@" and i + 1 < len(h):
            if h[i + 1][0] == "interface":          # @interface — an annotation type, keep the keyword
                i += 1
                continue
            i += 2
            while i + 1 < len(h) and h[i][0] == ".":
                i += 2
            if i < len(h) and h[i][0] == "(":
                depth = 0
                while i < len(h):
                    if h[i][0] == "(":
                        depth += 1
                    elif h[i][0] == ")":
                        depth -= 1
                        if depth == 0:
                            i += 1
                            break
                    i += 1
            continue
        out.append(h[i])
        i += 1
    return out


def _is_word(t):
    return bool(t) and (t[0].isalnum() or t[0] in "_$" or t in ('""', "''", "?"))


_MODIFIERS = {"public", "protected", "private", "static", "final", "abstract", "synchronized", "native", "default",
              "strictfp", "sealed"}


def _join(tokens):
    s, prev = "", ""
    for t in tokens:
        if s and (_is_word(t) and (_is_word(prev) or prev in (",", ")", ">", "]")) or t == "<" and prev in _MODIFIERS):
            s += " "
        s += t
        prev = t
    return s


def _first(h, tok):
    for k, (t, _) in enumerate(h):
        if t == tok:
            return k
    return -1


def _field_name(h):
    depth, name = 0, None
    for t, _ in h:
        if t == "<":
            depth += 1
        elif t == ">":
            depth -= 1
        elif depth == 0 and t in ("=", ","):
            break
        elif depth == 0 and _is_word(t) and t not in ('""', "''", "?"):
            name = t
    return name


def outline_java(src):
    toks = _java_tokens(src)
    out = []
    stack = [{"kind": "type", "sym": None, "qual": "", "name": "", "enum": False, "consts": False}]
    header = []
    for tok, ln in toks:
        top = stack[-1]
        if top["kind"] != "type":
            if tok == "{":
                stack.append({"kind": "block", "sym": None})
            elif tok == "}":
                done = stack.pop()
                if done["sym"] is not None:
                    done["sym"].end = ln
            continue
        if tok not in ("{", "}", ";"):
            header.append((tok, ln))
            continue
        if top["enum"] and not top["consts"]:          # enum constants until the first ';' of the enum body
            if tok == "{":
                stack.append({"kind": "block", "sym": None})
            elif tok == ";":
                top["consts"] = True
            elif tok == "}":
                done = stack.pop()
                if done["sym"] is not None:
                    done["sym"].end = ln
            header = []
            continue
        if tok == "}":
            done = stack.pop() if len(stack) > 1 else top
            if done["sym"] is not None:
                done["sym"].end = ln
            header = []
            continue
        start = header[0][1] if header else ln
        raw, header = header, []
        h = _strip_annotations(raw)
        is_anno = any(raw[k][0] == "@" and raw[k + 1][0] == "interface" for k in range(len(raw) - 1))
        if not h or h[0][0] in ("package", "import"):
            if tok == "{":
                stack.append({"kind": "block", "sym": None})
            continue
        paren, eq = _first(h, "("), _first(h, "=")
        kw = next((k for k, (t, _) in enumerate(h)
                   if t in _TYPE_KW and (k == 0 or h[k - 1][0] != ".") and (paren < 0 or k < paren)), -1)
        if tok == "{" and kw >= 0 and (eq < 0 or kw < eq) and kw + 1 < len(h):
            name = h[kw + 1][0]
            kind = "annotation" if is_anno and h[kw][0] == "interface" else h[kw][0]
            sym = Sym(top["qual"] + name, kind, start, ln, _join(t for t, _ in h))
            out.append(sym)
            stack.append({"kind": "type", "sym": sym, "qual": sym.qual + ".", "name": name,
                          "enum": h[kw][0] == "enum", "consts": False})
        elif paren > 0 and (eq < 0 or paren < eq) and _is_word(h[paren - 1][0]) \
                and h[paren - 1][0] not in _JAVA_KW and (paren < 2 or h[paren - 2][0] != "new"):
            name = h[paren - 1][0]
            kind = "constructor" if name == top["name"] else "method"
            sym = Sym(top["qual"] + name, kind, start, ln, _join(t for t, _ in h))
            out.append(sym)
            if tok == "{":
                stack.append({"kind": "body", "sym": sym})
        else:
            name = _field_name(h)
            if name and (eq >= 0 or tok == ";"):
                out.append(Sym(top["qual"] + name, "field", start, ln, _join(t for t, _ in (h[:eq] if eq > 0 else h))))
                if tok == "{":                            # initializer with a body: array, lambda, anonymous class
                    stack.append({"kind": "block", "sym": out[-1]})
            elif tok == "{":
                stack.append({"kind": "block", "sym": None})
    return out


# ───────────────────────────────────────────────────────────────────────────── TypeScript / JavaScript
_TS_TYPE_KW = {"class": "class", "interface": "interface", "enum": "enum", "namespace": "namespace",
               "module": "namespace"}
_TS_MODIFIERS = {"export", "default", "declare", "abstract", "public", "private", "protected", "static",
                 "readonly", "async", "override", "get", "set", "accessor"}
_TS_NOT_METHOD = {"if", "for", "while", "switch", "catch", "return", "function", "do", "else", "case",
                  "await", "typeof", "new", "yield", "with", "super", "in", "of"}
_REGEX_OK = set("([{,;:=!&|?+-*%<>~^") | {"", "return", "typeof", "instanceof", "in", "of", "new", "do",
                                          "else", "yield", "await", "case", "delete", "void"}


def _ts_tokens(src):
    """[(token, line)] without comments; a string, template literal or regex becomes one placeholder token."""
    toks, i, n, line, prev = [], 0, len(src), 1, ""
    while i < n:
        c = src[i]
        if c == "\n":
            line += 1; i += 1
        elif c in " \t\r\f":
            i += 1
        elif c == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i); i = n if j < 0 else j
        elif c == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2); end = n if j < 0 else j + 2
            line += src.count("\n", i, end); i = end
        elif c in "'\"":
            j = i + 1
            while j < n and src[j] not in (c, "\n"):
                j += 2 if src[j] == "\\" else 1
            i = j + 1; prev = "''"; toks.append((prev, line))
        elif c == "`":                                     # template literal, tracking ${ } nesting
            j, depth = i + 1, 0
            while j < n:
                d = src[j]
                if d == "\\":
                    j += 2; continue
                if d == "`" and depth == 0:
                    j += 1; break
                if d == "$" and j + 1 < n and src[j + 1] == "{":
                    depth += 1; j += 1
                elif d == "}" and depth > 0:
                    depth -= 1
                j += 1
            line += src.count("\n", i, j); i = j; prev = "``"; toks.append((prev, line))
        elif c == "/" and prev in _REGEX_OK:               # regex literal (vs division — by preceding token)
            j, klass = i + 1, False
            while j < n and src[j] != "\n" and (src[j] != "/" or klass):
                if src[j] == "\\":
                    j += 2; continue
                if src[j] == "[":
                    klass = True
                elif src[j] == "]":
                    klass = False
                j += 1
            if j < n and src[j] == "/":
                j += 1
                while j < n and src[j].isalpha():          # flags
                    j += 1
                i = j; prev = "/re/"; toks.append((prev, line))
            else:                                          # not a closed regex — treat '/' as punctuation
                prev = c; toks.append((c, line)); i += 1
        elif c.isalnum() or c in "_$":
            j = i
            while j < n and (src[j].isalnum() or src[j] in "_$"):
                j += 1
            prev = src[i:j]; toks.append((prev, line)); i = j
        elif c == "=" and i + 1 < n and src[i + 1] == ">":    # arrow, kept whole so outliners can spot it
            prev = "=>"; toks.append((prev, line)); i += 2
        else:
            prev = c; toks.append((c, line)); i += 1
    return toks


def _ts_name(t):
    return bool(t) and (t[:1].isalnum() or t[:1] in "_$") and t not in ('""', "''", "``", "/re/")


def _ts_strip(h):
    """Drop leading decorators (@Foo(...)) and modifiers from a header."""
    i = 0
    while i < len(h):
        t = h[i][0]
        if t == "@":
            i += 1
            while i < len(h) and (h[i][0] == "." or _ts_name(h[i][0])):
                i += 1
            if i < len(h) and h[i][0] == "(":
                depth = 0
                while i < len(h):
                    if h[i][0] == "(":
                        depth += 1
                    elif h[i][0] == ")":
                        depth -= 1
                        if depth == 0:
                            i += 1
                            break
                    i += 1
            continue
        if t in _TS_MODIFIERS:
            i += 1
            continue
        break
    return h[i:]


def _ts_find(words, tok):
    return words.index(tok) if tok in words else -1


def _ts_join(h):
    s, prev = "", ""
    for t, _ in h:
        if s and _ts_name(t) and (_ts_name(prev) or prev in (",", ")", ">", "]")):
            s += " "
        s += t
        prev = t
    return s


def _ts_open_sym(h, top, start, ln):
    """Classify a header ending in '{'. Returns a Sym or None."""
    words = [t for t, _ in h]
    eq = _ts_find(words, "=")
    for k, t in enumerate(words):                          # class/interface/enum/namespace NAME {
        if t in _TS_TYPE_KW and (k == 0 or words[k - 1] != ".") and k + 1 < len(words) and _ts_name(words[k + 1]):
            return Sym(top["qual"] + words[k + 1], _TS_TYPE_KW[t], start, ln, _ts_join(h))
    fi = _ts_find(words, "function")                       # function NAME ( ) { / function* NAME
    if fi >= 0:
        j = fi + 1 + (1 if fi + 1 < len(words) and words[fi + 1] == "*" else 0)
        if j < len(words) and _ts_name(words[j]):
            return Sym(top["qual"] + words[j], "method" if top["in_type"] else "function", start, ln, _ts_join(h))
    if "=>" in words and eq > 0 and _ts_name(words[eq - 1]) and words[eq - 1] not in _TS_NOT_METHOD \
            and (top["in_type"] or top.get("vars_ok")):     # NAME = (...) => {  only as a module const / class field
        return Sym(top["qual"] + words[eq - 1], "method" if top["in_type"] else "function", start, ln, _ts_join(h[:eq]))
    if top["in_type"]:                                     # method inside a class/interface: NAME ( ) {
        p = _ts_find(words, "(")
        if p > 0 and _ts_name(words[p - 1]) and words[p - 1] not in _TS_NOT_METHOD and (eq < 0 or p < eq):
            return Sym(top["qual"] + words[p - 1], "method", start, ln, _ts_join(h))
    return None


def _ts_stmt_sym(h, top, start, ln):
    """Statement-level symbol at ';': type alias, or a top-level const/let/var."""
    words = [t for t, _ in h]
    if len(words) >= 2 and words[0] == "type" and _ts_name(words[1]):
        eq = _ts_find(words, "=")
        return Sym(top["qual"] + words[1], "type", start, ln, _ts_join(h[:eq] if eq > 0 else h))
    if len(words) >= 2 and words[0] in ("const", "let", "var") and _ts_name(words[1]) and top.get("vars_ok"):
        kind = "function" if "=>" in words else "var"
        return Sym(top["qual"] + words[1], kind, start, ln, _ts_join(h) if kind == "function" else "")
    return None


def outline_ts(src):
    toks = _ts_tokens(src)
    out, header, pdepth = [], [], 0
    stack = [{"in_type": False, "vars_ok": True, "sym": None, "qual": ""}]
    for tok, ln in toks:
        if tok in ("(", "["):
            pdepth += 1
        elif tok in (")", "]") and pdepth > 0:
            pdepth -= 1
        if pdepth > 0 or tok not in ("{", "}", ";"):       # {}/; inside (...)/[...] are params/types, not structure
            header.append((tok, ln))
            continue
        top = stack[-1]
        if tok == "}":
            if len(stack) > 1:
                done = stack.pop()
                if done["sym"] is not None:
                    done["sym"].end = ln
            header = []
            continue
        raw, header = header, []
        start = raw[0][1] if raw else ln
        h = _ts_strip(raw)
        if tok == ";":
            sym = _ts_stmt_sym(h, top, start, ln)
            if sym:
                out.append(sym)
            continue
        sym = _ts_open_sym(h, top, start, ln)                # tok == "{"
        if sym:
            out.append(sym)
            stack.append({"in_type": sym.kind in ("class", "interface", "enum"),
                          "vars_ok": sym.kind == "namespace", "sym": sym, "qual": sym.qual + "."})
        else:
            stack.append({"in_type": False, "vars_ok": False, "sym": None, "qual": top["qual"]})
    return out


# ───────────────────────────────────────────────────────────────────────────── files
def read_text(path):
    with open(path, encoding="utf-8-sig", errors="replace") as fh:
        return fh.read()


_CACHE = {}
_DISK = {"data": None, "dirty": False}


def _cache_file():
    base = os.environ.get("SYMLENS_CACHE_DIR") or os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.cache"), "symlens")
    return os.path.join(base, "outlines-%s.json" % __version__)


def _disk():
    if _DISK["data"] is None:
        _DISK["data"] = {}
        if os.environ.get("SYMLENS_NO_CACHE") != "1":
            try:
                with open(_cache_file(), encoding="utf-8") as fh:
                    _DISK["data"] = json.load(fh)
            except (OSError, ValueError):
                pass
    return _DISK["data"]


def save_cache():
    """Write the outline cache if it changed (a full-tree find parses ~1500 files once, later runs reuse it)."""
    if not _DISK["dirty"] or os.environ.get("SYMLENS_NO_CACHE") == "1":
        return
    path = _cache_file()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".%d.tmp" % os.getpid()
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_DISK["data"], fh, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
        _DISK["dirty"] = False
    except OSError:
        pass


def outline(path):
    """[Sym] of a supported file (cached by size and mtime); ValueError for other files or unparsable code."""
    lang = LANG_BY_EXT.get(os.path.splitext(path)[1].lower())
    if not lang:
        raise ValueError("unsupported file type (python, java): %s" % path)
    st = os.stat(path)
    full = os.path.abspath(path)
    stamp = [st.st_size, st.st_mtime_ns]
    hit = _CACHE.get(full)
    if hit and hit[0] == stamp:
        return hit[1]
    row = _disk().get(full)
    if row and row[0] == stamp:
        syms = [Sym(*r) for r in row[1]]
    else:
        src = read_text(path)
        try:
            syms = outline_python(src) if lang == "python" else outline_java(src) if lang == "java" else outline_ts(src)
        except SyntaxError as exc:
            raise ValueError("cannot parse %s: %s" % (path, exc))
        _disk()[full] = [stamp, [[s.qual, s.kind, s.start, s.end, s.sig, s.doc] for s in syms]]
        _DISK["dirty"] = True
    _CACHE[full] = (stamp, syms)
    return syms


def load_config(root):
    try:
        with open(os.path.join(root, ".symlens.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        return {"exclude": []}
    return {"exclude": list(cfg.get("exclude") or [])}


def _excluded(rel, patterns):
    return any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(rel + "/", p) or rel.startswith(p.rstrip("*/") + "/")
               and p.endswith("/**") for p in patterns)


def walk(root, glob=None, exts=None):
    """Relative paths of files under root, skipping SKIP_DIRS and .symlens.json excludes."""
    exclude = load_config(root)["exclude"]
    exts = set(exts or LANG_BY_EXT)
    for base, dirs, files in os.walk(root):
        rel_base = os.path.relpath(base, root).replace("\\", "/")
        rel_base = "" if rel_base == "." else rel_base + "/"
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")
                         and not _excluded(rel_base + d, exclude))
        for f in sorted(files):
            rel = rel_base + f
            if os.path.splitext(f)[1].lower() in exts and not _excluded(rel, exclude) \
                    and (not glob or fnmatch.fnmatch(rel, glob) or fnmatch.fnmatch(f, glob)):
                yield rel


def _match(sym, name, substring=False):
    if substring:
        return name.lower() in sym.qual.lower()
    return sym.qual == name or sym.qual.endswith("." + name) or (sym.name == name)


def enclosing(syms, line):
    best = None
    for s in syms:
        if s.start <= line <= s.end and s.kind not in ("var", "field"):
            if best is None or s.start >= best.start:
                best = s
    return best


# ───────────────────────────────────────────────────────────────────────────── operations
def op_outline(root, path, depth=0, kinds=None, docs=False):
    full = _resolve(root, path)
    syms = outline(full)
    lines = []
    for s in syms:
        if depth and s.qual.count(".") >= depth:
            continue
        if kinds and s.kind not in kinds:
            continue
        row = "  " * s.qual.count(".") + s.line()
        if docs and s.doc:
            row += "  · " + s.doc
        lines.append(row)
    head = "%s — %d symbols" % (_rel(root, full), len(lines))
    return head + ("\n" + "\n".join(lines) if lines else "")


def op_body(root, path, name, line=None, numbered=False, max_lines=MAX_BODY_LINES, signature=False):
    full = _resolve(root, path)
    syms = [s for s in outline(full) if _match(s, name)]
    if line:
        syms = [s for s in syms if s.start <= int(line) <= s.end] or syms
    if not syms:
        return "no symbol %r in %s (try outline)" % (name, _rel(root, full))
    rel = _rel(root, full)
    if signature:                                     # interface only: the header carries the signature; add first doc line
        return "\n".join("# %s%s" % (s.line(rel), "\n    " + s.doc if s.doc else "") for s in syms[:12])
    src = read_text(full).splitlines()
    parts = []
    for s in syms[:5]:
        hi = min(s.end, s.start + max_lines - 1)
        body = src[s.start - 1:hi]
        if numbered:
            body = ["%d\t%s" % (s.start + k, t) for k, t in enumerate(body)]
        tail = "\n… %d more lines (max_lines)" % (s.end - hi) if hi < s.end else ""
        parts.append("# %s\n%s%s" % (s.line(rel), "\n".join(body), tail))
    more = "\n(+%d more matches — pass line=)" % (len(syms) - 5) if len(syms) > 5 else ""
    return "\n\n".join(parts) + more


def op_find(root, name, kind=None, glob=None, substring=False, max_results=MAX_RESULTS):
    hits = []
    for rel in walk(root, glob):
        try:
            syms = outline(os.path.join(root, rel))
        except (ValueError, OSError):
            continue
        for s in syms:
            if (not kind or s.kind == kind) and _match(s, name, substring):
                hits.append(s.line(rel))
                if len(hits) >= max_results:
                    return "\n".join(hits) + "\n… stopped at %d (max_results)" % max_results
    return "\n".join(hits) if hits else "no definition of %r" % name


def op_refs(root, name, glob=None, exts=None, max_results=MAX_RESULTS):
    word = name.rsplit(".", 1)[-1]
    qualifier = name[:len(name) - len(word) - 1] if "." in name else ""
    if qualifier:                                      # refs 'session.close' → only `session.close`, not every `.close`
        rx = re.compile(r"(?<![\w$])%s\s*\.\s*%s(?![\w$])" % (re.escape(qualifier), re.escape(word)))
    else:
        rx = re.compile(r"(?<![\w$])%s(?![\w$])" % re.escape(word))
    out, count = [], 0
    for rel in walk(root, glob, exts):
        full = os.path.join(root, rel)
        try:
            text = read_text(full)
        except OSError:
            continue
        if word not in text:
            continue
        try:
            syms = outline(full)
        except (ValueError, OSError):
            syms = []
        defs = {s.start for s in syms if s.name == word}
        rows = []
        for k, ln in enumerate(text.splitlines(), 1):
            if rx.search(ln) and k not in defs:
                enc = enclosing(syms, k)
                rows.append("  %d%s %s" % (k, " [%s]" % enc.qual if enc else "", ln.strip()[:160]))
                count += 1
                if count >= max_results:
                    break
        if rows:
            out.append(rel + "\n" + "\n".join(rows))
        if count >= max_results:
            out.append("… stopped at %d (max_results)" % max_results)
            break
    return "\n".join(out) if out else "no references to %r" % word


def op_at(root, path, line):
    """The symbol (function/method/class) that contains a 1-based line — for a traceback, diff hunk or grep hit."""
    full = _resolve(root, path)
    enc = enclosing(outline(full), int(line))
    if enc is None:
        return "no enclosing symbol at %s:%s" % (_rel(root, full), line)
    return enc.line(_rel(root, full))


def _resolve(root, path):
    full = path if os.path.isabs(path) else os.path.join(root, path)
    if not os.path.isfile(full):
        raise ValueError("no such file: %s" % path)
    return full


def _rel(root, full):
    try:
        return os.path.relpath(full, root).replace("\\", "/")
    except ValueError:
        return full.replace("\\", "/")


# ───────────────────────────────────────────────────────────────────────────── MCP over stdio
_P = {"path": {"type": "string", "description": "file path, relative to the server root or absolute"}}
TOOLS = [
    {"name": "symbols_overview",
     "description": "Outline of a Python/Java/TypeScript file: classes, functions, methods, fields with line ranges. "
                    "Use INSTEAD of reading a whole file. Pass docs=true for a one-line docstring per symbol — often "
                    "enough to understand the file without any symbol_body call.",
     "inputSchema": {"type": "object", "properties": dict(_P, depth={
         "type": "integer", "description": "0 = all nesting levels; 1 = top level only"}, docs={
         "type": "boolean", "description": "append each symbol's first docstring line"}),
         "required": ["path"]}},
    {"name": "symbol_body",
     "description": "Source of one symbol: 'Cls.method', 'method', 'func.inner'. Overloads are all returned "
                    "(narrow with line). Pass signature=true for just the signature + docstring (interface, no body).",
     "inputSchema": {"type": "object", "properties": dict(_P, name={"type": "string"}, line={"type": "integer"},
                                                         numbered={"type": "boolean"}, signature={"type": "boolean"},
                                                         max_lines={"type": "integer"}),
                     "required": ["path", "name"]}},
    {"name": "find_symbol",
     "description": "Where a symbol is defined across the tree (Python, Java, TypeScript): path:lines kind name — signature.",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string"}, "kind": {"type": "string"}, "glob": {"type": "string"},
         "substring": {"type": "boolean"}, "max_results": {"type": "integer"}}, "required": ["name"]}},
    {"name": "find_references",
     "description": "Where a name is used, each line tagged with its enclosing symbol — use INSTEAD of grep for "
                    "'who uses X'. A dotted name ('obj.method') matches only that access, cutting noise on common "
                    "method names. glob narrows files; exts adds extensions (e.g. ['.ts']).",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string"}, "glob": {"type": "string"},
         "exts": {"type": "array", "items": {"type": "string"}}, "max_results": {"type": "integer"}},
         "required": ["name"]}},
    {"name": "enclosing_symbol",
     "description": "The function/method/class that contains a given 1-based line — resolve a traceback, diff hunk "
                    "or grep hit to its symbol.",
     "inputSchema": {"type": "object", "properties": dict(_P, line={"type": "integer"}),
                     "required": ["path", "line"]}},
]
INSTRUCTIONS = ("symlens: read code by symbol, not by file — prefer it over reading whole files or grepping for "
                "symbols; it keeps large files out of context. Recipe: symbols_overview(path) [docs=true for a "
                "one-line-per-symbol map] instead of reading a file; symbol_body(path, name) [signature=true for just "
                "the interface] instead of reading a region; find_symbol(name) instead of grep+read to locate a "
                "definition; find_references(name) instead of grep for usages (a dotted 'obj.method' cuts noise); "
                "enclosing_symbol(path, line) to name the symbol at a line. Paths are relative to the server root.")


def call_tool(root, name, a):
    if name == "symbols_overview":
        return op_outline(root, a["path"], int(a.get("depth") or 0), docs=bool(a.get("docs")))
    if name == "symbol_body":
        return op_body(root, a["path"], a["name"], a.get("line"), bool(a.get("numbered")),
                       int(a.get("max_lines") or MAX_BODY_LINES), signature=bool(a.get("signature")))
    if name == "find_symbol":
        return op_find(root, a["name"], a.get("kind"), a.get("glob"), bool(a.get("substring")),
                       int(a.get("max_results") or MAX_RESULTS))
    if name == "find_references":
        exts = sorted(set(LANG_BY_EXT) | set(a.get("exts") or ())) if a.get("exts") else None
        return op_refs(root, a["name"], a.get("glob"), exts, int(a.get("max_results") or MAX_RESULTS))
    if name == "enclosing_symbol":
        return op_at(root, a["path"], a["line"])
    raise ValueError("unknown tool: %s" % name)


def serve(root, stdin=None, stdout=None):
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer

    def send(msg):
        stdout.write(json.dumps(msg, ensure_ascii=False).encode("utf-8") + b"\n")
        stdout.flush()

    for raw in stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except ValueError:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            continue
        mid, method = msg.get("id"), msg.get("method")
        if mid is None:                                   # notification
            continue
        params = msg.get("params") or {}
        if method == "initialize":
            result = {"protocolVersion": params.get("protocolVersion") or "2025-06-18",
                      "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": "symlens", "version": __version__},
                      "instructions": INSTRUCTIONS}
        elif method == "ping":
            result = {}
        elif method in ("resources/list", "prompts/list", "resources/templates/list"):
            result = {{"resources/list": "resources", "prompts/list": "prompts"}.get(method, "resourceTemplates"): []}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            try:
                text, err = call_tool(root, params.get("name"), params.get("arguments") or {}), False
            except (ValueError, KeyError, OSError) as exc:
                text, err = "error: %s" % exc, True
            result = {"content": [{"type": "text", "text": text}], "isError": err}
            save_cache()
        else:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found: %s" % method}})
            continue
        send({"jsonrpc": "2.0", "id": mid, "result": result})


# ───────────────────────────────────────────────────────────────────────────── CLI
def main(argv=None):
    ap = argparse.ArgumentParser(prog="symlens", description=__doc__.split("\n\n")[0])
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("--root", default=os.getcwd(), help="tree root (default: current directory)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("outline")
    p.add_argument("path")
    p.add_argument("--depth", type=int, default=0)
    p.add_argument("--docs", action="store_true", help="append each symbol's first docstring line")
    p = sub.add_parser("body")
    p.add_argument("path")
    p.add_argument("name")
    p.add_argument("--line", type=int)
    p.add_argument("--numbered", action="store_true")
    p.add_argument("--signature", action="store_true", help="just the signature + docstring, no body")
    p.add_argument("--max-lines", type=int, default=MAX_BODY_LINES)
    p = sub.add_parser("at")
    p.add_argument("path")
    p.add_argument("line", type=int)
    p = sub.add_parser("find")
    p.add_argument("name")
    p.add_argument("--kind")
    p.add_argument("--glob")
    p.add_argument("--substring", action="store_true")
    p.add_argument("--max", type=int, default=MAX_RESULTS)
    p = sub.add_parser("refs")
    p.add_argument("name")
    p.add_argument("--glob")
    p.add_argument("--ext", action="append", help="extra extension to search, e.g. --ext .ts")
    p.add_argument("--max", type=int, default=MAX_RESULTS)
    sub.add_parser("serve")
    a = ap.parse_args(argv)
    root = os.path.abspath(a.root)
    if a.cmd == "serve":
        serve(root)
        return 0
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    try:
        if a.cmd == "outline":
            print(op_outline(root, a.path, a.depth, docs=a.docs))
        elif a.cmd == "body":
            print(op_body(root, a.path, a.name, a.line, a.numbered, a.max_lines, signature=a.signature))
        elif a.cmd == "at":
            print(op_at(root, a.path, a.line))
        elif a.cmd == "find":
            print(op_find(root, a.name, a.kind, a.glob, a.substring, a.max))
        else:
            exts = sorted(set(LANG_BY_EXT) | set(a.ext or ())) if a.ext else None
            print(op_refs(root, a.name, a.glob, exts, a.max))
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    finally:
        save_cache()
    return 0


if __name__ == "__main__":
    sys.exit(main())

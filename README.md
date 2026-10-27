# symlens

Symbol-level code navigation for coding agents — a small, dependency-free alternative to Serena-style
tools. An agent asks for the outline of a file, the body of one method, the symbol enclosing a line, where a
symbol is defined, or where a name is used, instead of reading whole files. That keeps large codebases
(4000-line services, 1500-line test modules) out of the context window.

- **One file, standard library only** (Python ≥ 3.9). No language servers, no native modules, no network —
  works where application control blocks unsigned binaries.
- **Python** via `ast`; **Java** and **TypeScript / JavaScript** via small tokenizers that skip comments,
  strings, template and text blocks, regex and char literals, and match braces. Java: classes, interfaces,
  enums, records, annotations, methods, constructors, fields. TS/JS (`.ts .tsx .mts .cts .js .jsx .mjs .cjs`):
  classes, interfaces, enums, type aliases, namespaces, functions, methods, accessors, and arrow-function
  consts — module-level and class members, with locals inside function bodies left out.
- **Outlines carry the first docstring line**, and bodies can be fetched as **signature-only**.
- **CLI and MCP server** (stdio, JSON-RPC) with the same five operations.

## CLI

```
python symlens.py [--root DIR] outline <file> [--depth 1] [--docs]
python symlens.py [--root DIR] body <file> <name> [--line N] [--numbered] [--signature] [--max-lines 300]
python symlens.py [--root DIR] at <file> <line>
python symlens.py [--root DIR] find <name> [--kind method] [--glob '*Service.java'] [--substring]
python symlens.py [--root DIR] refs <name> [--glob ...] [--ext .ts]
```

`name` is `Cls.method`, `method`, `Outer.Inner`, or `func.inner`. Overloads are all returned; narrow with
`--line`. `--docs` appends each symbol's first docstring line; `--signature` returns just the signature and
docstring. `at` names the symbol enclosing a line (handy after a stack trace or a grep hit). A dotted `refs`
name (`self.size`, `cache.get`) matches only that qualified use; a bare name matches every use. `refs` tags
every hit with its enclosing symbol and skips the definition lines.

```
$ python symlens.py outline symlens.py --depth 1
symlens.py — 56 symbols
27-27 var __version__
37-49 class Sym — class Sym
53-66 function _py_sig — def _py_sig(lines, node)
...
$ python symlens.py body symlens.py Sym.name --signature
# symlens.py:43-45 method Sym.name — def name(self)
$ python symlens.py at symlens.py 44
symlens.py:43-45 method Sym.name — def name(self)
$ python symlens.py refs save_cache
symlens.py
  791 [serve] save_cache()
  855 [main] save_cache()
```

Outlines are cached on disk (`%LOCALAPPDATA%/symlens` or `~/.cache/symlens`, keyed by file size and mtime): the
first tree-wide `find` parses every file, later ones read the cache (1500 files: ~20 s cold, under 1 s warm).
`SYMLENS_NO_CACHE=1` disables it, `SYMLENS_CACHE_DIR` moves it.

## MCP (Claude Code and other clients)

Tools: `symbols_overview`, `symbol_body`, `enclosing_symbol`, `find_symbol`, `find_references`.

Project `.mcp.json`:

```json
{
  "mcpServers": {
    "symlens": {
      "command": "python",
      "args": ["/path/to/symlens.py", "--root", "/path/to/project", "serve"]
    }
  }
}
```

or `claude mcp add symlens -- python /path/to/symlens.py --root "$(pwd)" serve`.

## Excluding copies

A `.symlens.json` in the root lists globs (relative to the root) to skip — archives, mirrors, vendored code:

```json
{ "exclude": ["_archive/**", "mirror/**", "vendor/**"] }
```

Dot-directories, `node_modules`, `__pycache__`, `build`, `target`, `dist` and virtualenvs are always skipped.

## Limits

The Java and TS/JS readers are structural, not compilers. Java: members of anonymous and local classes and of
enum-constant bodies are not listed; a compact record constructor is not listed. TS/JS: a lexical outline, not a
type checker — overload signatures merge into one entry and functions assigned through patterns other than a plain
`const NAME = …` may be missed. Outlines are cached by file size and mtime (the MCP server keeps the cache for its
lifetime).

## Tests

```
python -m unittest discover -s tests
```

## License

MIT — see [LICENSE](LICENSE).

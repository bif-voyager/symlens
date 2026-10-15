# symlens

Symbol-level code navigation for coding agents — a small, dependency-free alternative to Serena-style
tools. An agent asks for the outline of a file, the body of one method, where a symbol is defined, or where a
name is used, instead of reading whole files. That keeps large codebases (4000-line services, 1500-line test
modules) out of the context window.

- **One file, standard library only** (Python ≥ 3.9). No language servers, no native modules, no network —
  works where application control blocks unsigned binaries.
- **Python** via `ast`; **Java** via a small tokenizer that skips comments, strings, text blocks and char
  literals and matches braces (classes, interfaces, enums, records, annotations, methods, constructors, fields).
- **CLI and MCP server** (stdio, JSON-RPC) with the same four operations.

## CLI

```
python symlens.py [--root DIR] outline <file> [--depth 1]
python symlens.py [--root DIR] body <file> <name> [--line N] [--numbered] [--max-lines 300]
python symlens.py [--root DIR] find <name> [--kind method] [--glob '*Service.java'] [--substring]
python symlens.py [--root DIR] refs <name> [--glob ...] [--ext .ts]
```

`name` is `Cls.method`, `method`, `Outer.Inner`, or `func.inner`. Overloads are all returned; narrow with
`--line`. `refs` tags every hit with its enclosing symbol and skips the definition lines.

```
$ python symlens.py outline symlens.py --depth 1
symlens.py — 43 symbols
27-27 var __version__
36-48 class Sym — class Sym
52-65 function _py_sig — def _py_sig(lines, node)
...
$ python symlens.py body symlens.py Sym.name
# symlens.py:42-44 method Sym.name — def name(self)
    @property
    def name(self):
        return self.qual.rsplit(".", 1)[-1]
$ python symlens.py refs save_cache
symlens.py
  566 [serve] save_cache()
  623 [main] save_cache()
```

Outlines are cached on disk (`%LOCALAPPDATA%/symlens` or `~/.cache/symlens`, keyed by file size and mtime): the
first tree-wide `find` parses every file, later ones read the cache (1500 files: ~20 s cold, under 1 s warm).
`SYMLENS_NO_CACHE=1` disables it, `SYMLENS_CACHE_DIR` moves it.

## MCP (Claude Code and other clients)

Tools: `symbols_overview`, `symbol_body`, `find_symbol`, `find_references`.

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

The Java reader is structural, not a compiler: members of anonymous and local classes and of enum-constant bodies
are not listed; a compact record constructor is not listed. Outlines are cached in memory by file size and mtime
(the MCP server keeps the cache for its lifetime).

## Tests

```
python -m unittest discover -s tests
```

## License

MIT — see [LICENSE](LICENSE).

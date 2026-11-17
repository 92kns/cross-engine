# engine-search

Search Gecko, Blink and WebKit source from one CLI, and compare how each engine implements something.

A single-file Python script with zero dependencies. It's a thin dispatcher on top of tools that already do each engine well:

| Engine | Backend |
| --- | --- |
| `gecko` (`firefox`) | [searchfox](https://searchfox.org/firefox-main/) JSON API, plus [searchfox-cli](https://github.com/padenot/searchfox-cli) for `define`/`calls-to`/`calls-from`/`field-layout` |
| `blink` | [chromium-search](https://github.com/92kns/chromium-search), scoped to `third_party/blink` |
| `chromium` (`chrome`) | chromium-search, all of chromium/src |
| `webkit` (`safari`) | [webkit-search](https://github.com/92kns/webkit-search) (searchfox wubkat, then Sourcegraph) |

## Install

```
git clone https://github.com/92kns/engine-search
git clone https://github.com/92kns/chromium-search
git clone https://github.com/92kns/webkit-search
ln -s "$PWD/engine-search/engine-search.py" ~/.local/bin/engine-search
engine-search doctor
```

Requires Python 3.11+. engine-search finds the other tools in this order:

1. `ENGINE_SEARCH_CHROMIUM` / `ENGINE_SEARCH_WEBKIT` (path to the script). If set, nothing else is tried.
2. `chromium-search` / `webkit-search` on `PATH`.
3. `../chromium-search/chromium-search.py` and `../webkit-search/webkit-search.py`, next to this checkout.

Gecko needs nothing extra. searchfox-cli is optional (looked up on `PATH`, `~/.mozbuild/bin`, `~/.cargo/bin`).

## Usage

```
engine-search compare <query>        # same query on every engine
engine-search gecko <query>          # Firefox, via searchfox
engine-search blink <args>           # chromium-search args, scoped to Blink
engine-search chromium <args>        # chromium-search args, whole tree
engine-search webkit <args>          # webkit-search args
engine-search doctor                 # which backends are installed and reachable
```

### compare

```
engine-search compare scrollend
engine-search compare '/ResizeObserver\w*Entry/' -L 5
engine-search compare '"scroll end"' case:yes --engines gecko,chromium
engine-search compare --defs ResizeObserverEntry
engine-search compare ScrollSnap --gecko-path layout --webkit-path Source/WebCore
```

The query has to be portable across engines: one term, a `"phrase"` or a `/regex/`, plus optional `case:yes`. Each engine gets it in its own syntax. For anything engine-specific (`lang:`, `class:`, `sym --kind`, ...), call that engine directly.

- `--engines` takes a comma list (default `gecko,blink,webkit`).
- `-L` is per engine (default 10).
- `--defs` shows only the definitions of a symbol.
- `--<engine>-path` limits one engine to a directory.

Engines run in parallel. If one fails or isn't installed, the others still print, and the failure is shown in that engine's header.

`--json` prints:

```json
{
  "query": {"text": "scrollend", "mode": "term", "case": false, "defs": false},
  "results": [
    {"engine": "gecko", "path": "...", "line": 117, "text": "...", "url": "...",
     "source": "searchfox", "kind": "Definitions (SCROLLEND_LOG)"}
  ],
  "errors": {"webkit": "webkit-search not found (...)"},
  "notes": {"webkit": ["no searchfox results; checking Sourcegraph (older index)"]}
}
```

`kind` is only present when the backend reports it: searchfox labels, or `symbol` for blink `--defs`. `source` names the index that answered (`searchfox`, `grimoire`, `sourcegraph`).

### Per engine

```
engine-search gecko 'case:yes ScrollEndEvent' --path layout --json
engine-search gecko define 'mozilla::dom::ResizeObserver'
engine-search blink 'class:ResizeObserver lang:cpp'
engine-search blink cat third_party/blink/renderer/core/resize_observer/resize_observer.h
engine-search webkit sym RenderBox --kind class
```

`blink` adds `--path third_party/blink` to search and find unless you pass `--path` or `--repo`.

## Freshness

All three indexes track main, so line numbers can differ by a few hours of commits. searchfox and source.chromium.org are usually hours fresh. webkit-search falls back to Sourcegraph, which can be days behind, and prints a note when it does. Older revisions are only available through `cat`/`ls`/`log --ref` on the engine tools.

## Config

| Variable | Default |
| --- | --- |
| `ENGINE_SEARCH_CHROMIUM` | (lookup) |
| `ENGINE_SEARCH_WEBKIT` | (lookup) |
| `ENGINE_SEARCH_GECKO_TREE` | `firefox-main` |
| `ENGINE_SEARCH_SEARCHFOX` | `https://searchfox.org` |
| `ENGINE_SEARCH_TIMEOUT` | `60` (seconds, per engine) |

## License

MIT

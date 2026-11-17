# engine-search

Search Gecko, Blink and WebKit source from one CLI, and compare how each engine implements something.

Stdlib-only Python 3.9+ (the macOS system `python3` works), no dependencies to install.

| Engine | Backend |
| --- | --- |
| `gecko` (`firefox`) | [searchfox](https://searchfox.org/firefox-main/) |
| `blink` | source.chromium.org, search and find scoped to `third_party/blink` |
| `chromium` (`chrome`) | source.chromium.org, all of chromium/src (also `--repo` for depot_tools and others) |
| `webkit` (`safari`) | searchfox (wubkat) on main, Sourcegraph for older refs and rich queries, GitHub for `cat` |

## Install

```
git clone https://github.com/92kns/engine-search
ln -s "$PWD/engine-search/engine-search.py" ~/.local/bin/engine-search
engine-search doctor
```

Optional: [searchfox-cli](https://github.com/padenot/searchfox-cli) enables `define`, `calls-to`, `calls-from` and `field-layout` for gecko and webkit. engine-search looks for it on `PATH`, `~/.mozbuild/bin` and `~/.cargo/bin`.

## Usage

```
engine-search compare <query>        # same query on every engine
engine-search gecko <query>          # Firefox, via searchfox
engine-search blink <args>           # like chromium, scoped to Blink
engine-search chromium <args>        # search, find, cat, ls, log, repos
engine-search webkit <args>          # search, sym, find, cat, ls, define, ...
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

Engines run in parallel. If one fails, the others still print, and the failure is shown in that engine's header.

`--json` prints:

```json
{
  "query": {"text": "scrollend", "mode": "term", "case": false, "defs": false},
  "results": [
    {"engine": "gecko", "path": "...", "line": 117, "text": "...", "url": "...",
     "source": "searchfox", "kind": "Definitions (SCROLLEND_LOG)"}
  ],
  "errors": {"webkit": "searchfox unavailable (...)"},
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

All three indexes track main, so line numbers can differ by a few hours of commits. searchfox and source.chromium.org are usually hours fresh. webkit falls back to Sourcegraph, which can be days behind, and prints a note when it does. Older revisions: `chromium cat|ls|log --ref <ref>` and `webkit search|cat|ls --ref <ref>`.

## Config

| Variable | Default |
| --- | --- |
| `ENGINE_SEARCH_GECKO_TREE` | `firefox-main` |
| `ENGINE_SEARCH_SEARCHFOX` | `https://searchfox.org` |
| `ENGINE_SEARCH_TIMEOUT` | `60` (seconds, gecko) |
| `CR_SEARCH_PROJECT` / `CR_SEARCH_REPO` | `chromium` / `chromium/src` |
| `SRC_ACCESS_TOKEN` | none (Sourcegraph token for webkit; raises rate limits) |

Run `engine-search chromium -h` or `engine-search webkit -h` for the rest.

## License

MIT. `chromium.py` is based on [jclay/chromium-search](https://github.com/jclay/chromium-search) (MIT, Joel Clay). See LICENSE.

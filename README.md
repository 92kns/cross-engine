# cross-engine

Pick a web platform topic and see how Gecko, Blink and WebKit each handle it: their code, feature flags, IDL, WPT expectations and recent commits, side by side.

```
$ cross-engine compare --idl ResizeObserverEntry
...
member                     spec    gecko   blink   webkit
target                     yes     yes     yes     yes
contentRect                yes     yes     yes     yes
borderBoxSize              yes     yes     yes     yes
contentBoxSize             yes     yes     yes     yes
devicePixelContentBoxSize  yes     yes     yes     -

$ cross-engine compare --wpt css/css-anchor-position/anchor-animation-dynamic-default.html
== gecko  1 test/subtest(s) expected not to pass
testing/web-platform/meta/.../anchor-animation-dynamic-default.html.ini:3: Animation with anchor() responds to position-anchor change: expected: FAIL

== blink  no expectations or failure baseline: passes

== webkit  baseline: 0 failing, 1 passing subtests
```

Stdlib-only Python 3.13+, no dependencies to install. If your `python3` is older (macOS ships 3.9), [uv](https://docs.astral.sh/uv/) fetches 3.13 for you: `uv run cross-engine.py ...`.

| Engine | Backend |
| --- | --- |
| `gecko` (`firefox`) | [searchfox](https://searchfox.org/firefox-main/) for search and find, GitHub (`mozilla-firefox/firefox`) for `cat` and `ls` |
| `blink` | source.chromium.org, search and find scoped to `third_party/blink` |
| `chromium` (`chrome`) | source.chromium.org, all of chromium/src (also `--repo` for depot_tools and others) |
| `webkit` (`safari`) | searchfox (wubkat) on main, Sourcegraph for older refs and rich queries, GitHub for `cat` |

## Install

```
git clone https://github.com/92kns/cross-engine
ln -s "$PWD/cross-engine/cross-engine.py" ~/.local/bin/cross-engine
cross-engine doctor
```

Optional: [searchfox-cli](https://github.com/padenot/searchfox-cli) enables `define`, `calls-to`, `calls-from` and `field-layout` for gecko and webkit, and for gecko also `calls-between`, `can-gc`, `function-at`, `spec-refs`, and `gecko sf <args>`, which passes any searchfox-cli arguments through (for `--blame`, release trees with `-R`, file-type filters). cross-engine looks for it on `PATH`, `~/.mozbuild/bin` and `~/.cargo/bin`.

## Usage

```
cross-engine compare [mode] <query>  # one topic, all three engines
cross-engine gecko <args>            # search, find, cat, ls, define, ...
cross-engine chromium <args>         # search, find, cat, ls, log, repos
cross-engine blink <args>            # like chromium, scoped to third_party/blink
cross-engine webkit <args>           # search, sym, find, cat, ls, define, ...
cross-engine doctor                  # check that each backend is reachable
```

Use `compare` to see the engines side by side, then the per-engine commands to dig into one.

### compare

| Mode | Shows | Example |
| --- | --- | --- |
| (none) | Code matches, plus "start here" docs and build files | `compare scrollend` |
| `--find` | File and directory names | `compare --find pgo --engines gecko,chromium,webkit` |
| `--defs` | Definitions of a symbol | `compare --defs ResizeObserverEntry` |
| `--flag` | Feature-flag status: on, experimental, or no flag | `compare --flag anchor-position` |
| `--idl` | IDL members per engine, against the spec's IDL | `compare --idl ViewTransition` |
| `--spec` | Code that cites a spec section | `compare --spec 'https://html.spec.whatwg.org/#navigate'` |
| `--wpt` | Each engine's expectations for a WPT test | `compare --wpt resize-observer/observe-001.html` |
| `--news` | Recent commits mentioning a term, or in a directory | `compare --news scrollend --since 3m` |
| `--history` | First and latest commits mentioning a term | `compare --history scrollend` |
| `--brief` | One Markdown page: flags, files, definitions, IDL | `compare --brief ResizeObserverEntry` |

Common flags:

- `--engines` takes a comma list (default `gecko,blink,webkit`). `blink` only covers `third_party/blink`, so for build, infra or docs topics use `--engines gecko,chromium,webkit`.
- `-L` is per engine (default 10).
- `--<engine>-path` limits one engine to a directory.
- `--json` for agents (see below).

Engines run in parallel. If one fails, the others still print, and the failure is shown in that engine's header.

#### Code search (no mode)

The query has to be portable across engines: one term, a `"phrase"` or a `/regex/`, plus optional `case:yes`. Each engine gets it in its own syntax. For anything engine-specific (`lang:`, `class:`, `sym --kind`, ...), call that engine directly.

Each engine's header lists up to three "start here" files: docs and build config among the matches (`docs/pgo.md`, `BUILD.gn`, `*.xcconfig`). For "how does X work" questions, those are usually better entry points than the code hits.

Third-party, vendored, generated and tooling code is hidden by default. This covers `third_party/` (except Blink), `Source/ThirdParty/`, `PerformanceTests/`, `Websites/`, dot-directories such as `.agents/`, and searchfox's third-party and generated groups. Each engine's header shows how much was hidden. `--all` shows everything.

#### --flag

Reads each engine's flag file from main: Gecko `StaticPrefList.yaml`, Blink `runtime_enabled_features.json5`, WebKit `UnifiedWebPreferences.yaml`. A flag matches when its name contains the term, ignoring case and punctuation. Real on/off switches are listed first; other prefs show as `(setting)`. When an engine has no match, the feature may have shipped and had its flag removed, or may not exist yet.

#### --idl

Finds `interface X` (including partial interfaces) in each engine's IDL, and lists the members. The `spec` column comes from WPT's `interfaces/*.idl`, which is generated from the spec. A member is shown in color when the engines disagree.

#### --wpt

Takes a test path relative to the WPT root, any engine's path to it, or a wpt.fyi URL. Shows:

- Gecko: `testing/web-platform/meta/<test>.ini` and its non-PASS expectations.
- Blink: `TestExpectations`, `NeverFixTests` and `SlowTests` lines, plus `-expected.txt` baselines and their failing-subtest count.
- WebKit: `TestExpectations` lines, plus the `-expected.txt` baseline's FAIL and PASS counts.

#### --news, --history

These use GitHub's commit search on `mozilla-firefox/firefox`, `chromium/chromium` and `WebKit/WebKit`. With `--<engine>-path`, `--news` lists commits in that directory instead, filtered to the term (`*` for all). Without a token, GitHub allows 10 searches a minute, and `--history` uses two per engine.

#### --json

```json
{
  "query": {"text": "scrollend", "type": "search", "mode": "term", "case": false},
  "results": [
    {"engine": "gecko", "path": "...", "line": 117, "text": "...", "url": "...",
     "source": "searchfox", "kind": "Definitions (SCROLLEND_LOG)"}
  ],
  "errors": {"webkit": "searchfox unavailable (...)"},
  "notes": {"webkit": ["no searchfox results; checking Sourcegraph (older index)"]},
  "start": {"gecko": ["build/docs/pgo.md"]},
  "summary": {"gecko": "..."}
}
```

`type` is the mode. `line` is 0 for file-name matches and commits. `kind` says what a row is (a searchfox label, `flag`, `setting`, `idl`, `commit`, `metadata`, `expectation`, `baseline`, ...). Modes add fields: `flag`/`value`/`status` for `--flag`, `sha`/`date`/`author`/`subject` for commits, and `members` per engine for `--idl`. `--brief --json` returns `{query, spec_idl, engines: {engine: {flags, files, definitions, idl}}}`.

### Using it from an agent

Paste this into your project's `AGENTS.md` or `CLAUDE.md`:

```markdown
## Cross-engine source lookups

`cross-engine` (https://github.com/92kns/cross-engine) answers "how do Gecko, Blink and
WebKit handle X". Prefer it over web searches for engine internals. Add `--json` for
structured output.

- Overview of a feature: `cross-engine compare --brief <Name>`
- Shipped or flagged? `cross-engine compare --flag <feature>`
- API differences: `cross-engine compare --idl <Interface>`
- Why does a WPT test fail only in Firefox? `cross-engine compare --wpt <test path>`
- Where to start reading: `cross-engine compare --find <name> --engines gecko,chromium,webkit`
- Read a file: `cross-engine gecko|chromium|webkit cat <path>`
```

### Per engine

```
cross-engine gecko 'mach python' --path build/pgo
cross-engine gecko find profileserver
cross-engine gecko cat build/pgo/profileserver.py
cross-engine gecko ls build/pgo
cross-engine gecko define 'mozilla::dom::ResizeObserver'
cross-engine blink 'class:ResizeObserver lang:cpp'
cross-engine blink cat third_party/blink/renderer/core/resize_observer/resize_observer.h
cross-engine webkit sym RenderBox --kind class
```

`gecko` search takes searchfox's free text: any words, plus `case:yes`, or a `/regex/`. `gecko ls` uses the GitHub API, which allows 60 listings an hour without a token.

`blink` adds `--path third_party/blink` to search and find unless you pass `--path` or `--repo`.

## Freshness

All three indexes track main, so line numbers can differ by a few hours of commits. searchfox and source.chromium.org are usually hours fresh. webkit falls back to Sourcegraph, which can be days behind, and prints a note when it does. Older revisions: `chromium cat|ls|log --ref <ref>` and `webkit search|cat|ls --ref <ref>`.

## Config

| Variable | Default |
| --- | --- |
| `CROSS_ENGINE_GECKO_TREE` | `firefox-main` |
| `CROSS_ENGINE_SEARCHFOX` | `https://searchfox.org` |
| `CROSS_ENGINE_TIMEOUT` | `60` (seconds, gecko) |
| `CR_SEARCH_PROJECT` / `CR_SEARCH_REPO` | `chromium` / `chromium/src` |
| `SRC_ACCESS_TOKEN` | none (Sourcegraph token for webkit; raises rate limits) |

Run `cross-engine chromium -h` or `cross-engine webkit -h` for the rest.

## License

MIT. `chromium.py` is based on [jclay/chromium-search](https://github.com/jclay/chromium-search) (MIT, Joel Clay). See LICENSE.

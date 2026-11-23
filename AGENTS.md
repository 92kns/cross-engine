- Stdlib-only Python 3.13+, no packaging. `cross-engine.py` has a PEP 723 header so `uv run` picks the right Python, and exits with a clear message on older versions. Three files:
  - `cross-engine.py`: CLI, `compare`, `doctor`, and the gecko searchfox client.
  - `chromium.py`: source.chromium.org (Grimoire) client and CLI, for blink/chromium.
    Based on jclay/chromium-search (MIT); keep its header and the LICENSE line.
  - `webkit.py`: WebKit client and CLI (searchfox wubkat first, Sourcegraph fallback,
    GitHub raw for cat).
- `cross-engine chromium|blink|webkit ...` calls `chromium.main(argv)` / `webkit.main(argv)`
  in-process. `compare` calls `chromium.search_all` and `webkit.search_files` from threads.
  Don't call `chromium.set_scope()` from compare: it sets module globals. Use a
  `file:^dir/` filter instead.
- gecko: `GET {searchfox}/{tree}/search?q=&path=&case=&regexp=`, `Accept: application/json`.
  Groups (normal/test/third_party/generated; skip `*`-prefixed keys) map a label
  ("Definitions (X)", "Uses (X)", "Textual Occurrences") to files with 1-based `lno`.
  define/calls-to/calls-from/field-layout go to `searchfox-cli` (optional), argv list, no shell.
- gecko cat: raw.githubusercontent.com `mozilla-firefox/firefox` (no auth). gecko ls: GitHub
  contents API (60/hour unauthenticated). gecko/webkit find: searchfox `q=path:<name>`, which
  returns a "Files" label with no `lines`.
- compare hides vendored code unless `--all`. Rows carry `vendored` (from `VENDORED_PATH` or
  searchfox's third_party/generated groups), and `run_engine` drops them and notes how many.
  chromium (not blink) adds `-file:third_party/` to the query instead, because vendored LLVM
  copies can fill every page of results.
- compare --flag (`flag_engine`): fetches the three flag files whole (`FLAG_FILES`) and parses
  them with small line-based parsers (`gecko_flags` tracks `#if` blocks, `blink_flags` reads
  `name:`/`status:` from JSON5, `webkit_flags` reads top-level keys and `defaultValue`). Status
  meanings are in `GECKO_VALUES`, `BLINK_STATUS`, `WEBKIT_STATUS`. source.chromium.org
  sometimes returns 401 for a moment; the Blink fetch retries once.
- compare modes (`MODES`, `cmd_compare`): each mode is one function per engine that returns an
  `EngineResult`, wrapped in `run_engine` so errors become an `errors` entry.
  - `--news`/`--history`: GitHub commit search (`commit_search`, `GITHUB_REPOS`); with a path,
    `path_history` (GitHub commits API, or Grimoire history for chromium/blink).
  - `--idl`: `IDL_DEF` regex per backend, then `idl_block` + `idl_members` on the fetched file.
    WPT's `interfaces/*.idl` copies are the spec column (`_is_spec_copy`). Grimoire mishandles
    `(^|...)` in patterns, so anchors stay outside groups.
  - `--spec`: `spec_regex` builds host + `\S*#` + fragment; `_rx` escapes only regex
    metacharacters (some backends reject `\#` and `\-`).
  - `--wpt`: `wpt_engine` first checks that the test exists in the engine's WPT copy, then
    reads Gecko meta `.ini`, Blink/WebKit `TestExpectations` and `-expected.txt` baselines.
  - `--brief`: runs flags, find, defs and IDL per engine in one thread pool.
- "start here" (`DOC_BUILD`, in `run_engine`): docs/build paths among the content hits.
- Grimoire treats bare text as regex: `query_for_chromium` escapes terms. Search and find
  only cover main; cat/ls/log take `--ref`.
- webkit: Sourcegraph line numbers are 0-based (add 1); raw files need a non-default
  User-Agent; never send auth to raw.githubusercontent.com.
- One engine failing must not fail `compare`; it becomes an `errors` entry.
- searchfox robots.txt disallows crawling: on-demand queries only.

## Dev commands

- Format: `uvx ruff@0.14.0 format *.py`
- Lint: `uvx ruff@0.14.0 check *.py`
- Smoke test: `python3 cross-engine.py doctor`

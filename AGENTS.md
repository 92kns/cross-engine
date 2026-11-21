- Stdlib-only Python 3.13+, no packaging. `engine-search.py` has a PEP 723 header so `uv run` picks the right Python, and exits with a clear message on older versions. Three files:
  - `engine-search.py`: CLI, `compare`, `doctor`, and the gecko searchfox client.
  - `chromium.py`: source.chromium.org (Grimoire) client and CLI, for blink/chromium.
    Based on jclay/chromium-search (MIT); keep its header and the LICENSE line.
  - `webkit.py`: WebKit client and CLI (searchfox wubkat first, Sourcegraph fallback,
    GitHub raw for cat).
- `engine-search chromium|blink|webkit ...` calls `chromium.main(argv)` / `webkit.main(argv)`
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
- Grimoire treats bare text as regex: `query_for_chromium` escapes terms. Search and find
  only cover main; cat/ls/log take `--ref`.
- webkit: Sourcegraph line numbers are 0-based (add 1); raw files need a non-default
  User-Agent; never send auth to raw.githubusercontent.com.
- One engine failing must not fail `compare`; it becomes an `errors` entry.
- searchfox robots.txt disallows crawling: on-demand queries only.

## Dev commands

- Format: `uvx ruff@0.14.0 format *.py`
- Lint: `uvx ruff@0.14.0 check *.py`
- Smoke test: `python3 engine-search.py doctor`

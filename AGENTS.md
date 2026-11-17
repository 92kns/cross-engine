- Single-file Python 3.11+ tool (`engine-search.py`), stdlib only, no packaging
- Thin dispatcher. Keep engine logic in the engine tools, not here:
  - gecko: searchfox JSON API directly (`searchfox_search`, tree `firefox-main`,
    `GET /{tree}/search?q=&path=&case=&regexp=` with `Accept: application/json`).
    Result groups (normal/test/third_party/generated; skip `*`-prefixed keys) map a
    label ("Definitions (X)", "Uses (X)", "Textual Occurrences") to files with 1-based `lno`.
    define/calls-to/calls-from/field-layout go to `searchfox-cli -R firefox-main`.
  - blink/chromium: `chromium-search` (92kns fork, needs `--path`/`--json` support).
    blink = `--path third_party/blink`. Grimoire treats bare text as regex, so
    `query_for_chromium` escapes terms. `--defs` uses `symbol:`.
  - webkit: `webkit-search`. Gets the query as-is; path becomes `file:^dir/`.
    `--defs` over-fetches and keeps rows whose `kind` is `Definitions (...)`.
- Tool lookup (`find_tool`): `ENGINE_SEARCH_CHROMIUM`/`ENGINE_SEARCH_WEBKIT` (explicit,
  no fallback), then PATH, then `../<tool>/<tool>.py` next to this repo.
  `.py` paths run with `sys.executable`.
- Subprocesses: argv lists only (no shell), `NO_COLOR=1`, `--json`, timeout
  `ENGINE_SEARCH_TIMEOUT` (60s). `note:` lines on stderr are kept as notes.
- One engine failing must not fail `compare`; it becomes an `errors` entry.
- searchfox robots.txt disallows crawling: on-demand queries only, no prefetching.

## Dev commands

- Format: `uvx ruff@0.14.0 format engine-search.py`
- Lint: `uvx ruff@0.14.0 check engine-search.py`
- Smoke test: `python3 engine-search.py doctor`

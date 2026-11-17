#!/usr/bin/env python3
# /// script
# requires-python = ">=3.13"
# dependencies = []
# ///
"""engine-search: search Gecko, Blink and WebKit source from one CLI.

A thin dispatcher. Gecko goes to searchfox (firefox-main) directly; Blink and
Chromium use the source.chromium.org API (chromium.py); WebKit uses searchfox
with a Sourcegraph fallback (webkit.py). `compare` runs
the same query on several engines in parallel.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info < (3, 13):  # noqa: UP036 (friendly error on old Pythons)
    sys.exit(
        f"engine-search needs Python 3.13+ (this is {sys.version.split()[0]}).\n"
        "Run it with uv instead: uv run engine-search.py ..."
    )

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chromium
import webkit

VERSION = "engine-search/0.1"
SEARCHFOX = os.environ.get("ENGINE_SEARCH_SEARCHFOX", "https://searchfox.org")
GECKO_TREE = os.environ.get("ENGINE_SEARCH_GECKO_TREE", "firefox-main")
TIMEOUT = float(os.environ.get("ENGINE_SEARCH_TIMEOUT", "60"))

ENGINES = ("gecko", "blink", "webkit")
ALIASES = {"firefox": "gecko", "chrome": "chromium", "safari": "webkit"}
# blink is chromium/src scoped to third_party/blink; chromium is the whole tree.
DEFAULT_PATHS = {"blink": "third_party/blink"}
SEARCHFOX_CLI_CMDS = ("define", "calls-to", "calls-from", "field-layout")
GROUP_ORDER = ("normal", "test", "third_party", "generated")


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

_color = sys.stdout.isatty() and "NO_COLOR" not in os.environ
_CODES = {"bold": "1", "dim": "2", "red": "31", "green": "32", "cyan": "36"}


def paint(text: str, *names: str) -> str:
    if not _color or not names:
        return text
    codes = ";".join(_CODES[n] for n in names)
    return f"\033[{codes}m{text}\033[0m"


def note(msg: str) -> None:
    sys.stderr.write(f"note: {msg}\n")


@dataclass
class Row:
    engine: str
    path: str
    line: int
    text: str
    url: str
    source: str
    kind: str | None = None

    def as_dict(self) -> dict:
        d = {
            "engine": self.engine,
            "path": self.path,
            "line": self.line,
            "text": self.text,
            "url": self.url,
            "source": self.source,
        }
        if self.kind:
            d["kind"] = self.kind
        return d


@dataclass
class EngineResult:
    engine: str
    rows: list[Row] = field(default_factory=list)
    error: str | None = None
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Query model shared by compare
# ---------------------------------------------------------------------------


@dataclass
class Query:
    text: str
    mode: str  # "term", "phrase" or "regex"
    case: bool = False


def parse_query(raw: str) -> Query:
    tokens = raw.strip()
    case = False
    stripped = re.sub(r"(^|\s)case:yes(?=\s|$)", " ", tokens)
    if stripped != tokens:
        case = True
    q = stripped.strip()
    if len(q) >= 2 and q.startswith("/") and q.endswith("/"):
        return Query(q[1:-1], "regex", case)
    if len(q) >= 2 and q.startswith('"') and q.endswith('"') and '"' not in q[1:-1]:
        return Query(q[1:-1], "phrase", case)
    if q and not re.search(r"\s", q) and ":" not in q.replace("::", ""):
        return Query(q, "term", case)
    raise ValueError(
        'compare takes one term, a "phrase" or a /regex/, plus optional '
        "case:yes. For engine-specific syntax, run `engine-search <engine> ...`."
    )


def query_for_webkit(q: Query, path: str) -> str:
    parts = ["case:yes"] if q.case else []
    if path:
        parts.append(f"file:^{path.strip('/')}/")
    if q.mode == "regex":
        parts.append(f"/{q.text}/")
    elif q.mode == "phrase":
        parts.append(f'"{q.text}"')
    else:
        parts.append(q.text)
    return " ".join(parts)


def query_for_chromium(q: Query) -> str:
    # Grimoire treats bare text as a regex, so escape literal terms.
    if q.mode == "regex":
        body = q.text
    elif q.mode == "phrase":
        body = f'"{q.text}"'
    else:
        body = re.escape(q.text)
    return f"case:yes {body}" if q.case else body


# ---------------------------------------------------------------------------
# Gecko: searchfox JSON API
# ---------------------------------------------------------------------------


def searchfox_search(
    text: str, *, regexp: bool = False, case: bool = False, path: str = ""
) -> dict:
    params = urllib.parse.urlencode(
        {
            "q": text,
            "path": path,
            "case": "true" if case else "false",
            "regexp": "true" if regexp else "false",
        }
    )
    url = f"{SEARCHFOX}/{GECKO_TREE}/search?{params}"
    req = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": VERSION}
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"searchfox returned {exc.code} {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"network error: {exc.reason}") from exc


def searchfox_rows(data: dict, limit: int, *, defs_of: str | None = None) -> list[Row]:
    rows: list[Row] = []
    seen: set[tuple[str, int]] = set()
    groups = [g for g in GROUP_ORDER if g in data]
    groups += [g for g in data if not g.startswith("*") and g not in groups]
    for group in groups:
        for label, files in data[group].items():
            if defs_of is not None and not _is_definition_of(label, defs_of):
                continue
            for f in files:
                for ln in f.get("lines", []):
                    key = (f["path"], ln["lno"])
                    if key in seen:
                        continue
                    seen.add(key)
                    rows.append(
                        Row(
                            engine="gecko",
                            path=f["path"],
                            line=ln["lno"],
                            text=ln.get("line", "").strip(),
                            url=f"{SEARCHFOX}/{GECKO_TREE}/source/{f['path']}#{ln['lno']}",
                            source="searchfox",
                            kind=label,
                        )
                    )
                    if len(rows) >= limit:
                        return rows
    return rows


def _is_definition_of(label: str, symbol: str) -> bool:
    m = re.match(r"Definitions \((.*)\)$", label)
    if not m:
        return False
    name = m.group(1)
    leaf = symbol.rsplit("::", 1)[-1]
    return name in (symbol, leaf) or name.endswith("::" + leaf)


def gecko_rows(q: Query, path: str, limit: int, defs: bool) -> EngineResult:
    res = EngineResult("gecko")
    data = searchfox_search(
        q.text, regexp=q.mode == "regex", case=q.case, path=path.strip("/")
    )
    res.rows = searchfox_rows(data, limit, defs_of=q.text if defs else None)
    if data.get("*timedout*"):
        res.notes.append("searchfox timed out; results may be partial")
    return res


# ---------------------------------------------------------------------------
# Blink / Chromium / WebKit
# ---------------------------------------------------------------------------


def find_searchfox_cli() -> str | None:
    found = shutil.which("searchfox-cli")
    if found:
        return found
    for d in ("~/.mozbuild/bin", "~/.cargo/bin"):
        p = Path(d).expanduser() / "searchfox-cli"
        if p.is_file():
            return str(p)
    return None


def chromium_rows(
    engine: str, q: Query, path: str, limit: int, defs: bool
) -> EngineResult:
    res = EngineResult(engine)
    query = f"symbol:{q.text}" if defs else query_for_chromium(q)
    if path:
        # A file: filter, not chromium.set_scope(), since compare runs engines
        # in parallel threads and set_scope changes module globals.
        query = f"file:^{re.escape(path.strip('/'))}/ {query}"
    for page in chromium.search_all(query, limit=limit, context_lines=1):
        for f in page.files:
            for sn in f.snippets:
                for ln in sn.lines:
                    if not ln.ranges or len(res.rows) >= limit:
                        continue
                    res.rows.append(
                        Row(
                            engine=engine,
                            path=f.path,
                            line=ln.line_number,
                            text=ln.line_text.strip(),
                            url=chromium.line_url(f.path, ln.line_number),
                            source="grimoire",
                            kind="symbol" if defs else None,
                        )
                    )
    return res


def webkit_rows(q: Query, path: str, limit: int, defs: bool) -> EngineResult:
    res = EngineResult("webkit")
    prefix = path.strip("/") + "/" if path.strip("/") else ""
    if defs:
        try:
            syms = webkit.searchfox_symbols(q.text)
        except webkit.SearchfoxUnavailable as exc:
            raise RuntimeError(f"searchfox unavailable ({exc})") from exc
        for sym in syms:
            if not sym.path.startswith(prefix):
                continue
            res.rows.append(
                Row(
                    engine="webkit",
                    path=sym.path,
                    line=sym.line,
                    text=sym.name,
                    url=webkit.line_url(sym.path, sym.line, "", "searchfox"),
                    source="searchfox",
                    kind=f"Definitions ({sym.name})",
                )
            )
            if len(res.rows) >= limit:
                break
        return res
    out = webkit.search_files(query_for_webkit(q, path), limit=limit)
    res.notes = out.alerts
    for f, _si, ln in webkit.iter_lines(out.files, limit):
        if not ln.ranges:
            continue
        res.rows.append(
            Row(
                engine="webkit",
                path=f.path,
                line=ln.line_number,
                text=ln.line_text.strip(),
                url=webkit.line_url(f.path, ln.line_number, f.commit, f.source),
                source=f.source,
                kind=f.label or None,
            )
        )
    return res


def search_engine(
    engine: str, q: Query, path: str, limit: int, defs: bool
) -> EngineResult:
    try:
        if engine == "gecko":
            return gecko_rows(q, path, limit, defs)
        if engine == "webkit":
            return webkit_rows(q, path, limit, defs)
        return chromium_rows(engine, q, path, limit, defs)
    except TimeoutError:
        return EngineResult(engine, error="timed out")
    except (RuntimeError, ValueError, OSError, urllib.error.URLError) as exc:
        return EngineResult(engine, error=str(exc))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def parse_engines(spec: str) -> list[str]:
    out = []
    for name in spec.split(","):
        name = ALIASES.get(name.strip(), name.strip())
        if name not in (*ENGINES, "chromium"):
            raise SystemExit(f"Error: unknown engine {name!r}")
        if name not in out:
            out.append(name)
    return out


def cmd_compare(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="engine-search compare", add_help=False)
    p.add_argument("query", nargs="*")
    p.add_argument("--engines", default=",".join(ENGINES))
    p.add_argument("-L", "--limit", type=int, default=10)
    p.add_argument("--json", action="store_true")
    p.add_argument("--defs", action="store_true")
    for e in (*ENGINES, "chromium"):
        p.add_argument(f"--{e}-path", default=None)
    p.add_argument("-h", "--help", action="store_true")
    opts = p.parse_args(argv)
    if opts.help or not opts.query:
        sys.stderr.write(COMPARE_USAGE)
        return 0 if opts.help else 1

    try:
        q = parse_query(" ".join(opts.query))
    except ValueError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1
    if opts.defs and q.mode != "term":
        sys.stderr.write("Error: --defs takes a single symbol name\n")
        return 1

    engines = parse_engines(opts.engines)
    paths = {e: getattr(opts, f"{e}_path") or DEFAULT_PATHS.get(e, "") for e in engines}
    with ThreadPoolExecutor(max_workers=len(engines)) as pool:
        futures = [
            pool.submit(search_engine, e, q, paths[e], opts.limit, opts.defs)
            for e in engines
        ]
        results = [f.result() for f in futures]

    if opts.json:
        out = {
            "query": {
                "text": q.text,
                "mode": q.mode,
                "case": q.case,
                "defs": opts.defs,
            },
            "results": [r.as_dict() for res in results for r in res.rows],
            "errors": {res.engine: res.error for res in results if res.error},
            "notes": {res.engine: res.notes for res in results if res.notes},
        }
        sys.stdout.write(json.dumps(out, indent=2) + "\n")
    else:
        print_compare(results, paths)
    return 0 if any(not r.error for r in results) else 1


def print_compare(results: list[EngineResult], paths: dict[str, str]) -> None:
    first = True
    for res in results:
        if not first:
            sys.stdout.write("\n")
        first = False
        scope = f" {paint(paths[res.engine] + '/', 'dim')}" if paths[res.engine] else ""
        if res.error:
            status = paint(f"error: {res.error}", "red")
        else:
            n = len(res.rows)
            status = paint(f"{n} result{'s' if n != 1 else ''}", "dim")
        sys.stdout.write(
            f"{paint('== ' + res.engine, 'bold', 'cyan')}{scope}  {status}\n"
        )
        for msg in res.notes:
            sys.stdout.write(paint(f"   note: {msg}", "dim") + "\n")
        for r in res.rows:
            loc = paint(f"{r.path}:{r.line}", "green")
            sys.stdout.write(f"{loc}: {r.text}\n")


def cmd_gecko(argv: list[str]) -> int:
    if argv and argv[0] in SEARCHFOX_CLI_CMDS:
        cli = find_searchfox_cli()
        if not cli:
            sys.stderr.write(
                "Error: searchfox-cli not found (needed for define/calls-to/"
                "calls-from/field-layout). Install: cargo install searchfox-cli\n"
            )
            return 1
        if len(argv) < 2:
            sys.stderr.write(f"Usage: engine-search gecko {argv[0]} <symbol>\n")
            return 1
        return subprocess.call([cli, "-R", GECKO_TREE, f"--{argv[0]}", *argv[1:]])

    p = argparse.ArgumentParser(prog="engine-search gecko", add_help=False)
    p.add_argument("query", nargs="*")
    p.add_argument("-L", "--limit", type=int, default=30)
    p.add_argument("--path", default="")
    p.add_argument("--json", action="store_true")
    p.add_argument("-h", "--help", action="store_true")
    opts = p.parse_args(argv)
    if opts.help or not opts.query:
        sys.stderr.write(GECKO_USAGE)
        return 0 if opts.help else 1
    try:
        q = parse_query(" ".join(opts.query))
    except ValueError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1
    res = search_engine("gecko", q, opts.path, opts.limit, False)
    if res.error:
        sys.stderr.write(f"Error: {res.error}\n")
        return 1
    for msg in res.notes:
        note(msg)
    if opts.json:
        sys.stdout.write(json.dumps([r.as_dict() for r in res.rows], indent=2) + "\n")
    elif not res.rows:
        sys.stderr.write("No results found\n")
    else:
        for r in res.rows:
            sys.stdout.write(f"{paint(f'{r.path}:{r.line}', 'green')}: {r.text}\n")
    return 0


def call_backend(main, argv: list[str]) -> int:
    try:
        main(argv)
    except SystemExit as exc:
        if isinstance(exc.code, str):
            sys.stderr.write(exc.code + "\n")
            return 1
        return exc.code or 0
    except BrokenPipeError:
        raise
    except (RuntimeError, ValueError, OSError, urllib.error.URLError) as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1
    return 0


def cmd_chromium(engine: str, argv: list[str]) -> int:
    if (
        engine == "blink"
        and argv
        and argv[0] not in ("cat", "ls", "log", "repos", "syntax")
        and "--path" not in argv
        and "--repo" not in argv
    ):
        argv = [*argv, "--path", DEFAULT_PATHS["blink"]]
    return call_backend(chromium.main, argv)


def cmd_doctor() -> int:
    ok = True
    py = sys.version_info
    sys.stdout.write(f"{'python':<15} {py.major}.{py.minor}.{py.micro}\n")
    cli = find_searchfox_cli()
    sys.stdout.write(
        f"{'searchfox-cli':<15} "
        + (paint(cli, "green") if cli else paint("missing (optional)", "dim"))
        + "\n"
    )
    sys.stdout.write("\nlive check (query: ResizeObserver, 1 result each)\n")
    q = Query("ResizeObserver", "term")
    engines = ("gecko", "blink", "webkit")
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(
            pool.map(
                lambda e: search_engine(e, q, DEFAULT_PATHS.get(e, ""), 1, False),
                engines,
            )
        )
    for res in results:
        if res.error:
            ok = False
            sys.stdout.write(f"  {res.engine:<8} {paint('FAIL ' + res.error, 'red')}\n")
        else:
            src = res.rows[0].source if res.rows else "no results"
            sys.stdout.write(f"  {res.engine:<8} {paint('ok', 'green')} ({src})\n")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

USAGE = """\
Usage: engine-search <engine|command> [args]

Engines (arguments pass through to the engine's tool):
  gecko <query>            Firefox, via searchfox (firefox-main)
  gecko define <symbol>    Also calls-to, calls-from, field-layout (needs searchfox-cli)
  blink <args>             Blink: like chromium, search/find scoped to third_party/blink
  chromium <args>          Chromium: search, find, cat, ls, log, repos (`chromium -h`)
  webkit <args>            WebKit: search, sym, find, cat, ls, define, ...
                           (`engine-search webkit -h`)

Commands:
  compare <query>          Same query on every engine, side by side
  doctor                   Check which backends are installed and reachable
  version                  Print the version

Aliases: firefox=gecko, chrome=chromium, safari=webkit.
Run `engine-search compare -h` for compare flags.

Examples:
  engine-search compare scrollend
  engine-search compare '/ResizeObserver\\w*/' -L 5
  engine-search compare --defs ResizeObserverEntry --json
  engine-search gecko 'case:yes ScrollEnd' --path layout/
  engine-search gecko define 'mozilla::dom::ResizeObserver'
  engine-search blink 'class:ResizeObserver lang:cpp'
  engine-search webkit sym RenderBox --kind class
"""

COMPARE_USAGE = """\
Usage: engine-search compare <query> [flags]

The query is one term, a "phrase" or a /regex/, plus optional case:yes.
Each engine gets the same query translated to its own syntax.

Flags:
  --engines <list>         Comma list (default: gecko,blink,webkit; also chromium)
  -L, --limit <n>          Results per engine (default: 10)
  --defs                   Definitions of a symbol only
  --json                   {query, results: [{engine,path,line,text,url,source,kind?}],
                            errors: {engine: msg}, notes: {engine: [msg]}}
  --gecko-path <dir>       Limit gecko to a directory
  --blink-path <dir>       Limit blink (default: third_party/blink)
  --chromium-path <dir>    Limit chromium
  --webkit-path <dir>      Limit webkit
"""

GECKO_USAGE = """\
Usage: engine-search gecko <query> [-L n] [--path dir] [--json]
       engine-search gecko define|calls-to|calls-from|field-layout <symbol>
           [searchfox-cli flags]

The query is one term, a "phrase" or a /regex/, plus optional case:yes.
"""


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        sys.stderr.write(USAGE)
        return 0 if argv else 1
    cmd, rest = ALIASES.get(argv[0], argv[0]), argv[1:]
    if cmd in ("version", "--version"):
        sys.stdout.write(VERSION + "\n")
        return 0
    if cmd == "compare":
        return cmd_compare(rest)
    if cmd == "doctor":
        return cmd_doctor()
    if cmd == "gecko":
        return cmd_gecko(rest)
    if cmd == "webkit":
        return call_backend(webkit.main, rest)
    if cmd in ("blink", "chromium"):
        return cmd_chromium(cmd, rest)
    sys.stderr.write(f"Error: unknown engine or command {argv[0]!r}\n\n{USAGE}")
    return 1


if __name__ == "__main__":
    try:
        code = main(sys.argv[1:])
        sys.stdout.flush()  # surface a broken pipe here, not at exit
        sys.exit(code)
    except KeyboardInterrupt:
        sys.exit(130)
    except BrokenPipeError:
        # Output was piped to something like `head` that exited early.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(0)

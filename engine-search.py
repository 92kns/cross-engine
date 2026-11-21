#!/usr/bin/env python3
# /// script
# requires-python = ">=3.13"
# dependencies = []
# ///
"""engine-search: search Gecko, Blink and WebKit source from one CLI.

Gecko goes to searchfox (firefox-main) directly; Blink and Chromium use the
source.chromium.org API (chromium.py); WebKit uses searchfox with a Sourcegraph
fallback (webkit.py). `compare` runs the same query on several engines in
parallel.
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
from functools import partial
from pathlib import Path

if sys.version_info < (3, 13):  # noqa: UP036 (friendly error on old Pythons)
    sys.exit(
        f"engine-search needs Python 3.13+ (this is {sys.version.split()[0]}).\n"
        "Run it with uv instead: uv run engine-search.py ..."
    )

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chromium
import webkit

VERSION = "engine-search/0.2"
SEARCHFOX = os.environ.get("ENGINE_SEARCH_SEARCHFOX", "https://searchfox.org")
GECKO_TREE = os.environ.get("ENGINE_SEARCH_GECKO_TREE", "firefox-main")
GECKO_GITHUB = "mozilla-firefox/firefox"  # official git repo; raw files and ls
WEBKIT_TREE = "wubkat"
TIMEOUT = float(os.environ.get("ENGINE_SEARCH_TIMEOUT", "60"))

ENGINES = ("gecko", "blink", "webkit")
ALIASES = {"firefox": "gecko", "chrome": "chromium", "safari": "webkit"}
# blink is chromium/src scoped to third_party/blink; chromium is the whole tree.
DEFAULT_PATHS = {"blink": "third_party/blink"}
SEARCHFOX_CLI_CMDS = ("define", "calls-to", "calls-from", "field-layout")
GROUP_ORDER = ("normal", "test", "third_party", "generated")
# searchfox puts these result groups aside; compare hides them by default.
VENDORED_GROUPS = ("third_party", "generated")
# Vendored code and benchmark copies that drown out engine code in compare.
VENDORED_PATH = re.compile(
    r"(^|/)third_party/(?!blink/)"
    r"|^(Source/ThirdParty|PerformanceTests|Websites)/"
    r"|^__GENERATED__/"
)


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


def write_json(data: object) -> None:
    sys.stdout.write(json.dumps(data, indent=2) + "\n")


@dataclass
class Row:
    engine: str
    path: str
    line: int  # 0 for file-name matches
    text: str
    url: str
    source: str
    kind: str | None = None
    vendored: bool = False

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

    def location(self) -> str:
        return f"{self.path}:{self.line}" if self.line else self.path


@dataclass
class EngineResult:
    engine: str
    rows: list[Row] = field(default_factory=list)
    error: str | None = None
    notes: list[str] = field(default_factory=list)


def is_vendored(path: str) -> bool:
    return bool(VENDORED_PATH.search(path))


# ---------------------------------------------------------------------------
# Query model shared by compare
# ---------------------------------------------------------------------------


@dataclass
class Query:
    text: str
    mode: str  # "term", "phrase" or "regex"
    case: bool = False


_CASE = re.compile(r"(^|\s)case:yes(?=\s|$)")


def split_case(raw: str) -> tuple[str, bool]:
    stripped = _CASE.sub(" ", raw)
    return stripped.strip(), stripped != raw


def parse_query(raw: str) -> Query:
    q, case = split_case(raw.strip())
    if len(q) >= 2 and q.startswith("/") and q.endswith("/"):
        return Query(q[1:-1], "regex", case)
    if len(q) >= 2 and q.startswith('"') and q.endswith('"') and '"' not in q[1:-1]:
        return Query(q[1:-1], "phrase", case)
    if q and not re.search(r"\s", q) and ":" not in q.replace("::", ""):
        return Query(q, "term", case)
    raise ValueError(
        'compare takes one term, a "phrase" or a /regex/, plus optional case:yes.\n'
        "For other queries, search one engine: "
        "engine-search gecko|chromium|webkit '<query>'.\n"
        "To compare file names instead, use: engine-search compare --find <name>"
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
# searchfox (Gecko, and WebKit file-name search)
# ---------------------------------------------------------------------------


def searchfox_search(
    text: str,
    *,
    tree: str = GECKO_TREE,
    regexp: bool = False,
    case: bool = False,
    path: str = "",
) -> dict:
    params = urllib.parse.urlencode(
        {
            "q": text,
            "path": path,
            "case": "true" if case else "false",
            "regexp": "true" if regexp else "false",
        }
    )
    req = urllib.request.Request(
        f"{SEARCHFOX}/{tree}/search?{params}",
        headers={"Accept": "application/json", "User-Agent": VERSION},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"searchfox returned {exc.code} {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"network error: {exc.reason}") from exc


def searchfox_rows(
    data: dict,
    *,
    engine: str = "gecko",
    tree: str = GECKO_TREE,
    defs_of: str | None = None,
    files_only: bool = False,
) -> list[Row]:
    """Flatten searchfox JSON. `files_only` returns file-name matches only."""
    rows: list[Row] = []
    seen: set[tuple[str, int]] = set()
    groups = [g for g in GROUP_ORDER if g in data]
    groups += [g for g in data if not g.startswith("*") and g not in groups]
    for group in groups:
        if not isinstance(data[group], dict):
            continue
        vendored = group in VENDORED_GROUPS
        for label, files in data[group].items():
            if files_only != (label == "Files"):
                continue
            if defs_of is not None and not _is_definition_of(label, defs_of):
                continue
            for f in files:
                path = f["path"]
                lines = f.get("lines") or ([{"lno": 0}] if files_only else [])
                for ln in lines:
                    key = (path, ln["lno"])
                    if key in seen:
                        continue
                    seen.add(key)
                    anchor = f"#{ln['lno']}" if ln["lno"] else ""
                    rows.append(
                        Row(
                            engine=engine,
                            path=path,
                            line=ln["lno"],
                            text=ln.get("line", "").strip(),
                            url=f"{SEARCHFOX}/{tree}/source/{path}{anchor}",
                            source="searchfox",
                            kind=label,
                            vendored=vendored or is_vendored(path),
                        )
                    )
    return rows


def _is_definition_of(label: str, symbol: str) -> bool:
    m = re.match(r"Definitions \((.*)\)$", label)
    if not m:
        return False
    name = m.group(1)
    leaf = symbol.rsplit("::", 1)[-1]
    return name in (symbol, leaf) or name.endswith("::" + leaf)


def gecko_rows(q: Query, path: str, defs: bool) -> EngineResult:
    res = EngineResult("gecko")
    data = searchfox_search(
        q.text, regexp=q.mode == "regex", case=q.case, path=path.strip("/")
    )
    res.rows = searchfox_rows(data, defs_of=q.text if defs else None)
    if data.get("*timedout*"):
        res.notes.append("searchfox timed out; results may be partial")
    return res


def searchfox_find(engine: str, tree: str, name: str, path: str) -> EngineResult:
    res = EngineResult(engine)
    data = searchfox_search(f"path:{name}", tree=tree, path=path.strip("/"))
    res.rows = searchfox_rows(data, engine=engine, tree=tree, files_only=True)
    return res


# ---------------------------------------------------------------------------
# Gecko files: GitHub (mozilla-firefox/firefox)
# ---------------------------------------------------------------------------


def gecko_cat(path: str, ref: str | None) -> list[str]:
    path = webkit._safe_path(path)
    ref = webkit._safe_ref(ref) if ref else "main"
    data = webkit._github_raw(path, ref, GECKO_GITHUB)
    if data is None:
        raise RuntimeError("could not reach raw.githubusercontent.com")
    return webkit.decode_text(data, path)


def gecko_ls(path: str, ref: str | None) -> list[tuple[str, str]]:
    path = path.strip("/")
    if path:
        path = webkit._safe_path(path)
    ref = webkit._safe_ref(ref) if ref else "main"
    url = (
        f"https://api.github.com/repos/{GECKO_GITHUB}/contents/"
        f"{urllib.parse.quote(path)}?ref={urllib.parse.quote(ref, safe='')}"
    )
    # No auth: unauthenticated requests are limited to 60 an hour per IP.
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": VERSION},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise RuntimeError(f"Directory not found: {path or '/'} at {ref}") from exc
        if exc.code in (403, 429):
            raise RuntimeError(
                "GitHub API rate limit hit (60 listings an hour without a token). "
                "Try again later, or use `gecko find`."
            ) from exc
        raise RuntimeError(f"GitHub returned {exc.code} {exc.reason}") from exc
    if not isinstance(data, list):
        raise RuntimeError(f"Not a directory: {path} (use cat)")
    return [(e["name"], e.get("type", "file")) for e in data]


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


def _scope(path: str) -> str:
    # A file: filter, not chromium.set_scope(), since compare runs engines in
    # parallel threads and set_scope changes module globals.
    return f"file:^{re.escape(path.strip('/'))}/ " if path.strip("/") else ""


def chromium_rows(
    engine: str, q: Query, path: str, fetch: int, defs: bool, show_all: bool
) -> EngineResult:
    res = EngineResult(engine)
    query = _scope(path) + (f"symbol:{q.text}" if defs else query_for_chromium(q))
    if not show_all and not path.strip("/").startswith("third_party"):
        # Vendored copies (LLVM, ANGLE, ...) can fill every page; drop them on
        # the server rather than after the fetch.
        query += " -file:third_party/"
        res.notes.append("third_party/ excluded (--all)")
    for page in chromium.search_all(query, limit=fetch, context_lines=1):
        for f in page.files:
            for sn in f.snippets:
                for ln in sn.lines:
                    if not ln.ranges or len(res.rows) >= fetch:
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
                            vendored=is_vendored(f.path),
                        )
                    )
    return res


def chromium_find(engine: str, name: str, path: str) -> EngineResult:
    res = EngineResult(engine)
    payload = chromium.build_suggest_payload(_scope(path) + name, max_suggestions=50)
    for p in chromium.fetch_suggest(chromium.DEFAULT_API_KEY, payload, TIMEOUT):
        res.rows.append(
            Row(
                engine=engine,
                path=p,
                line=0,
                text="",
                url=chromium.file_url(p),
                source="grimoire",
                kind="dir" if p.endswith("/") else "file",
                vendored=is_vendored(p),
            )
        )
    return res


def webkit_rows(q: Query, path: str, fetch: int, defs: bool) -> EngineResult:
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
                    vendored=is_vendored(sym.path),
                )
            )
        return res
    out = webkit.search_files(query_for_webkit(q, path), limit=fetch)
    res.notes = out.alerts
    for f, _si, ln in webkit.iter_lines(out.files, fetch):
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
                vendored=is_vendored(f.path),
            )
        )
    return res


def run_engine(job, engine: str, limit: int, show_all: bool) -> EngineResult:
    """Run one engine's job, turn failures into an error, and trim the rows."""
    try:
        res = job()
    except TimeoutError:
        return EngineResult(engine, error="timed out")
    except (RuntimeError, ValueError, OSError, urllib.error.URLError) as exc:
        return EngineResult(engine, error=str(exc))
    if not show_all:
        kept = [r for r in res.rows if not r.vendored]
        hidden = len(res.rows) - len(kept)
        res.rows = kept
        if hidden:
            res.notes.append(f"hid {hidden} third-party/vendored matches (--all)")
    res.rows = res.rows[:limit]
    return res


def search_engine(
    engine: str, q: Query, path: str, limit: int, defs: bool, show_all: bool = True
) -> EngineResult:
    # Over-fetch so there's enough left after hiding vendored code. searchfox
    # returns everything in one response anyway.
    fetch = limit if show_all else max(limit * 5, 200)
    if engine == "gecko":
        job = partial(gecko_rows, q, path, defs)
    elif engine == "webkit":
        job = partial(webkit_rows, q, path, fetch, defs)
    else:
        job = partial(chromium_rows, engine, q, path, fetch, defs, show_all)
    return run_engine(job, engine, limit, show_all)


def find_engine(
    engine: str, name: str, path: str, limit: int, show_all: bool = True
) -> EngineResult:
    if engine == "gecko":
        job = partial(searchfox_find, "gecko", GECKO_TREE, name, path)
    elif engine == "webkit":
        job = partial(searchfox_find, "webkit", WEBKIT_TREE, name, path)
    else:
        job = partial(chromium_find, engine, name, path)
    return run_engine(job, engine, limit, show_all)


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
    p.add_argument("--find", action="store_true")
    p.add_argument("--all", action="store_true", dest="show_all")
    for e in (*ENGINES, "chromium"):
        p.add_argument(f"--{e}-path", default=None)
    p.add_argument("-h", "--help", action="store_true")
    opts = p.parse_args(argv)
    if opts.help or not opts.query:
        sys.stderr.write(COMPARE_USAGE)
        return 0 if opts.help else 1
    if opts.defs and opts.find:
        sys.stderr.write("Error: use --defs or --find, not both\n")
        return 1

    raw = " ".join(opts.query)
    if opts.find:
        q = Query(raw, "path")
    else:
        try:
            q = parse_query(raw)
        except ValueError as exc:
            sys.stderr.write(f"Error: {exc}\n")
            return 1
        if opts.defs and q.mode != "term":
            sys.stderr.write("Error: --defs takes a single symbol name\n")
            return 1

    engines = parse_engines(opts.engines)
    paths = {e: getattr(opts, f"{e}_path") or DEFAULT_PATHS.get(e, "") for e in engines}
    with ThreadPoolExecutor(max_workers=len(engines)) as pool:
        if opts.find:
            futures = [
                pool.submit(find_engine, e, q.text, paths[e], opts.limit, opts.show_all)
                for e in engines
            ]
        else:
            futures = [
                pool.submit(
                    search_engine,
                    e,
                    q,
                    paths[e],
                    opts.limit,
                    opts.defs,
                    opts.show_all,
                )
                for e in engines
            ]
        results = [f.result() for f in futures]

    for res in results:
        if res.engine == "blink" and not res.rows and not res.error:
            res.notes.append(
                "blink only covers third_party/blink; "
                "use --engines gecko,chromium,webkit for build or infra code"
            )

    if opts.json:
        mode = "find" if opts.find else "defs" if opts.defs else "search"
        write_json(
            {
                "query": {"text": q.text, "mode": q.mode, "case": q.case, "type": mode},
                "results": [r.as_dict() for res in results for r in res.rows],
                "errors": {res.engine: res.error for res in results if res.error},
                "notes": {res.engine: res.notes for res in results if res.notes},
            }
        )
    else:
        print_compare(results, paths)
    return 0 if any(not r.error for r in results) else 1


def print_compare(results: list[EngineResult], paths: dict[str, str]) -> None:
    for i, res in enumerate(results):
        if i:
            sys.stdout.write("\n")
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
            loc = paint(r.location(), "green")
            sys.stdout.write(f"{loc}: {r.text}\n" if r.text else f"{loc}\n")


def cmd_gecko(argv: list[str]) -> int:
    sub = argv[0] if argv else ""
    if sub in SEARCHFOX_CLI_CMDS:
        cli = find_searchfox_cli()
        if not cli:
            sys.stderr.write(
                "Error: searchfox-cli not found (needed for define/calls-to/"
                "calls-from/field-layout). Install: cargo binstall searchfox-cli\n"
            )
            return 1
        if len(argv) < 2:
            sys.stderr.write(f"Usage: engine-search gecko {sub} <symbol>\n")
            return 1
        return subprocess.call([cli, "-R", GECKO_TREE, f"--{sub}", *argv[1:]])

    p = argparse.ArgumentParser(prog="engine-search gecko", add_help=False)
    p.add_argument("args", nargs="*")
    p.add_argument("-L", "--limit", type=int, default=None)
    p.add_argument("--path", default="")
    p.add_argument("--ref", default=None)
    p.add_argument("-n", "--number", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("-h", "--help", action="store_true")
    opts = p.parse_args(argv)
    if opts.help or not opts.args:
        sys.stderr.write(GECKO_USAGE)
        return 0 if opts.help else 1

    rest = " ".join(opts.args[1:])
    try:
        if sub == "cat":
            if not rest:
                sys.stderr.write("Usage: engine-search gecko cat <path> [--ref]\n")
                return 1
            lines = gecko_cat(rest, opts.ref)
            if opts.json:
                write_json(
                    {
                        "path": rest,
                        "ref": opts.ref or "main",
                        "content": "\n".join(lines),
                    }
                )
            for i, line in enumerate([] if opts.json else lines, 1):
                sys.stdout.write(f"{i:6}\t{line}\n" if opts.number else line + "\n")
            return 0
        if sub == "ls":
            entries = sorted(
                gecko_ls(rest, opts.ref), key=lambda e: (e[1] != "dir", e[0])
            )
            if opts.json:
                write_json([{"name": n, "type": t} for n, t in entries])
                return 0
            for name, kind in entries:
                sys.stdout.write(
                    (paint(name + "/", "bold", "cyan") if kind == "dir" else name)
                    + "\n"
                )
            return 0
    except (RuntimeError, ValueError, OSError, urllib.error.URLError) as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1

    if opts.ref:
        note("--ref only applies to cat and ls; searchfox only indexes main")
    if sub == "find":
        if not rest:
            sys.stderr.write("Usage: engine-search gecko find <name>\n")
            return 1
        res = find_engine("gecko", rest, opts.path, opts.limit or 30)
    else:
        raw = rest if sub == "search" else " ".join(opts.args)
        text, case = split_case(raw)
        regexp = len(text) > 2 and text.startswith("/") and text.endswith("/")
        q = Query(text[1:-1] if regexp else text, "regex" if regexp else "text", case)
        res = run_engine(
            lambda: gecko_rows(q, opts.path, False), "gecko", opts.limit or 30, True
        )
    if res.error:
        sys.stderr.write(f"Error: {res.error}\n")
        return 1
    for msg in res.notes:
        note(msg)
    if opts.json:
        write_json([r.as_dict() for r in res.rows])
    elif not res.rows:
        sys.stderr.write("No results found\n")
    else:
        for r in res.rows:
            loc = paint(r.location(), "green")
            sys.stdout.write(f"{loc}: {r.text}\n" if r.text else f"{loc}\n")
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
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(
            pool.map(
                lambda e: search_engine(e, q, DEFAULT_PATHS.get(e, ""), 1, False),
                ENGINES,
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

Engines:
  gecko <args>             Firefox: search, find, cat, ls, define, ... (`gecko -h`)
  chromium <args>          Chromium: search, find, cat, ls, log, repos (`chromium -h`)
  blink <args>             Like chromium, with search/find scoped to third_party/blink
  webkit <args>            WebKit: search, sym, find, cat, ls, define, ... (`webkit -h`)

Commands:
  compare <query>          Same query on every engine, side by side
  compare --find <name>    Compare file and directory names across engines
  doctor                   Check that each backend is reachable
  version                  Print the version

Aliases: firefox=gecko, chrome=chromium, safari=webkit.
Run `engine-search compare -h` for compare flags.

Examples:
  engine-search compare scrollend
  engine-search compare --find pgo --engines gecko,chromium,webkit
  engine-search compare '/ResizeObserver\\w*/' -L 5
  engine-search compare --defs ResizeObserverEntry --json
  engine-search gecko 'mach python' --path build/pgo
  engine-search gecko cat build/pgo/profileserver.py
  engine-search gecko define 'mozilla::dom::ResizeObserver'
  engine-search blink 'class:ResizeObserver lang:cpp'
  engine-search webkit sym RenderBox --kind class
"""

COMPARE_USAGE = """\
Usage: engine-search compare <query> [flags]
       engine-search compare --find <name> [flags]

The query is one term, a "phrase" or a /regex/, plus optional case:yes.
Each engine gets the same query translated to its own syntax.

Flags:
  --engines <list>         Comma list (default: gecko,blink,webkit; also chromium).
                           blink only covers third_party/blink. For build, infra
                           or docs topics, use gecko,chromium,webkit.
  -L, --limit <n>          Results per engine (default: 10)
  --find                   Match file and directory names, not file contents.
                           A good first step for "how does X work" questions.
  --defs                   Definitions of a symbol only
  --all                    Include third-party, vendored and generated code
                           (hidden by default: third_party/, Source/ThirdParty/,
                           PerformanceTests/, Websites/, searchfox's third-party
                           and generated groups)
  --json                   {query, results: [{engine,path,line,text,url,source,kind?}],
                            errors: {engine: msg}, notes: {engine: [msg]}}.
                           line is 0 for file-name matches.
  --gecko-path <dir>       Limit gecko to a directory
  --blink-path <dir>       Limit blink (default: third_party/blink)
  --chromium-path <dir>    Limit chromium
  --webkit-path <dir>      Limit webkit
"""

GECKO_USAGE = """\
Usage: engine-search gecko [search] <query> [-L n] [--path dir] [--json]
       engine-search gecko find <name> [-L n] [--path dir] [--json]
       engine-search gecko cat <path> [--ref ref] [-n] [--json]
       engine-search gecko ls [dir] [--ref ref] [--json]
       engine-search gecko define|calls-to|calls-from|field-layout <symbol>
           [searchfox-cli flags]

search goes to searchfox (firefox-main) and takes searchfox's free text: any
words, plus optional case:yes, or a /regex/. find matches file names. cat reads
from GitHub (mozilla-firefox/firefox); ls uses the GitHub API, which allows 60
listings an hour without a token. define and friends need searchfox-cli.
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

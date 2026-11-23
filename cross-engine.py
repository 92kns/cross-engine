#!/usr/bin/env python3
# /// script
# requires-python = ">=3.13"
# dependencies = []
# ///
"""cross-engine: search Gecko, Blink and WebKit source from one CLI.

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
from datetime import date, timedelta
from functools import partial
from pathlib import Path

if sys.version_info < (3, 13):  # noqa: UP036 (friendly error on old Pythons)
    sys.exit(
        f"cross-engine needs Python 3.13+ (this is {sys.version.split()[0]}).\n"
        "Run it with uv instead: uv run cross-engine.py ..."
    )

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chromium
import webkit

VERSION = "cross-engine/0.2"
SEARCHFOX = os.environ.get("CROSS_ENGINE_SEARCHFOX", "https://searchfox.org")
GECKO_TREE = os.environ.get("CROSS_ENGINE_GECKO_TREE", "firefox-main")
GECKO_GITHUB = "mozilla-firefox/firefox"  # official git repo; raw files and ls
WEBKIT_TREE = "wubkat"
TIMEOUT = float(os.environ.get("CROSS_ENGINE_TIMEOUT", "60"))

ENGINES = ("gecko", "blink", "webkit")
ALIASES = {"firefox": "gecko", "chrome": "chromium", "safari": "webkit"}
# blink is chromium/src scoped to third_party/blink; chromium is the whole tree.
DEFAULT_PATHS = {"blink": "third_party/blink"}
SEARCHFOX_CLI_CMDS = ("define", "calls-to", "calls-from", "field-layout")
GROUP_ORDER = ("normal", "test", "third_party", "generated")
# searchfox puts these result groups aside; compare hides them by default.
VENDORED_GROUPS = ("third_party", "generated")
DOC_BUILD = re.compile(
    r"\.(md|rst)$|(^|/)docs?/|(^|/)(BUILD\.gn|moz\.build|CMakeLists\.txt)$"
    r"|\.(gni|configure|xcconfig|cmake|mozbuild)$"
)
# Vendored code and benchmark copies that drown out engine code in compare.
VENDORED_PATH = re.compile(
    r"(^|/)third_party/(?!blink/)"
    r"|^(Source/ThirdParty|PerformanceTests|Websites)/"
    r"|^__GENERATED__/"
    r"|^\."  # tooling and agent docs: .agents/, .github/, ...
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
    extra: dict | None = None  # mode-specific fields, e.g. a flag's status

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
        if self.extra:
            d.update(self.extra)
        return d

    def location(self) -> str:
        return f"{self.path}:{self.line}" if self.line else self.path


@dataclass
class EngineResult:
    engine: str
    rows: list[Row] = field(default_factory=list)
    error: str | None = None
    notes: list[str] = field(default_factory=list)
    summary: str = ""  # one-line verdict, e.g. for --wpt
    start: list[str] = field(default_factory=list)  # docs/build hits (search)
    data: dict = field(default_factory=dict)  # mode-specific, e.g. IDL members


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
        "cross-engine gecko|chromium|webkit '<query>'.\n"
        "To compare file names instead, use: cross-engine compare --find <name>"
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


def _suggest(path: str, name: str) -> list[str]:
    payload = chromium.build_suggest_payload(_scope(path) + name, max_suggestions=50)
    return chromium.fetch_suggest(chromium.DEFAULT_API_KEY, payload, TIMEOUT)


def chromium_find(engine: str, name: str, path: str) -> EngineResult:
    res = EngineResult(engine)
    found = _suggest(path, name)
    snake = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name).lower()
    if not found and snake != name:
        # Blink files are snake_case: ResizeObserverEntry -> resize_observer_entry.
        found = _suggest(path, snake)
    for p in found:
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
            res.notes.append(
                f"hid {hidden} third-party, vendored or tooling matches (--all)"
            )
    # Docs and build files are the best entry points for "how does X work",
    # but they rarely rank first. Collect them before trimming.
    for r in res.rows:
        if (
            r.line
            and DOC_BUILD.search(r.path)
            and not re.match(r"\.|.*/\.", r.path)  # skip .agents/, .github/, ...
            and r.path not in res.start
        ):
            res.start.append(r.path)
    res.start = res.start[:3]
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
# Feature flags (compare --flag)
# ---------------------------------------------------------------------------

FLAG_FILES = {
    "gecko": "modules/libpref/init/StaticPrefList.yaml",
    "blink": "third_party/blink/renderer/platform/runtime_enabled_features.json5",
    "webkit": "Source/WTF/Scripts/Preferences/UnifiedWebPreferences.yaml",
}
FLAG_FILES["chromium"] = FLAG_FILES["blink"]

# What each engine's status values mean, for the summary column.
GECKO_VALUES = {
    "true": "on",
    "false": "off",
    "@IS_NIGHTLY_BUILD@": "Nightly only",
    "@IS_NOT_NIGHTLY_BUILD@": "off in Nightly",
    "@IS_EARLY_BETA_OR_EARLIER@": "Nightly and early Beta",
    "@IS_NOT_RELEASE_OR_BETA@": "Nightly and Dev Edition",
    "@IS_NIGHTLY_OR_DEV_EDITION@": "Nightly and Dev Edition",
    "@IS_ANDROID@": "Android only",
    "@IS_NOT_ANDROID@": "not on Android",
    "@IS_NOT_MOBILE@": "desktop only",
}
BLINK_STATUS = {
    "stable": "on",
    "experimental": "experimental web platform features flag",
    "test": "tests only",
    "": "off (no status)",
}
WEBKIT_STATUS = {
    "stable": "on",
    "mature": "on (mature)",
    "preview": "Safari Technology Preview",
    "testable": "tests only",
    "developer": "developer setting",
    "unstable": "off (unstable)",
    "internal": "internal setting",
    "embedder": "embedder setting",
}


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def gecko_flags(lines: list[str], term: str) -> list[dict]:
    """Parse StaticPrefList.yaml: `- name:` entries, tracking #if blocks."""
    out: list[dict] = []
    conds: list[str] = []
    cur: dict | None = None
    for i, line in enumerate(lines, 1):
        directive = re.match(r"#(if|ifdef|ifndef|elif|else|endif)\b\s*(.*)", line)
        if directive:
            kind, arg = directive.groups()
            if kind in ("if", "ifdef", "ifndef"):
                conds.append(f"#{kind} {arg}".strip())
            elif kind in ("elif", "else") and conds:
                conds[-1] = f"#{kind} {arg}".strip() + f" (of {conds[-1]})"
            elif kind == "endif" and conds:
                conds.pop()
            continue
        m = re.match(r"- name: (\S+)", line)
        if m:
            cur = None
            if term in _norm(m.group(1)):
                cur = {
                    "name": m.group(1),
                    "line": i,
                    "value": "",
                    "type": "",
                    "cond": list(conds),
                }
                out.append(cur)
            continue
        if cur is not None:
            t = re.match(r"\s+type:\s*(\S+)", line)
            if t:
                cur["type"] = t.group(1)
            v = re.match(r"\s+value:\s*(.*)", line)
            if v:
                cur["value"] = v.group(1).strip()
                cur["cond"] = list(conds)
    for e in out:
        raw = e["value"]
        e["status"] = GECKO_VALUES.get(raw) or (f"value {raw}" if raw else "?")
        if e["cond"]:
            e["status"] += f"  [{'; '.join(e['cond'])}]"
    return out


def blink_flags(lines: list[str], term: str) -> list[dict]:
    """Parse runtime_enabled_features.json5 feature entries."""
    text = "\n".join(lines)
    starts = [
        (m.start(), m.group(1), text.count("\n", 0, m.start()) + 1)
        for m in re.finditer(r'^\s*name:\s*"([^"]+)"', text, re.M)
    ]
    out = []
    for idx, (pos, name, lno) in enumerate(starts):
        if term not in _norm(name):
            continue
        end = starts[idx + 1][0] if idx + 1 < len(starts) else len(text)
        body = text[pos:end]
        sm = re.search(r'\bstatus:\s*(?:"(\w*)"|\{([^}]*)\})', body)
        if sm and sm.group(2) is not None:
            pairs = re.findall(r'"(\w+)":\s*"(\w*)"', sm.group(2))
            status = ", ".join(f"{k}: {BLINK_STATUS.get(v, v)}" for k, v in pairs)
            value = "{" + ", ".join(f"{k}: {v}" for k, v in pairs) + "}"
        else:
            value = sm.group(1) if sm else ""
            status = BLINK_STATUS.get(value, value)
        trial = re.search(r'origin_trial_feature_name:\s*"([^"]+)"', body)
        if trial:
            status += f"  [origin trial {trial.group(1)}]"
        out.append({"name": name, "line": lno, "value": value, "status": status})
    return out


def webkit_flags(lines: list[str], term: str) -> list[dict]:
    """Parse UnifiedWebPreferences.yaml top-level entries."""
    out: list[dict] = []
    cur: dict | None = None
    in_default = False
    for i, line in enumerate(lines, 1):
        top = re.match(r"^(\w+):\s*$", line)
        if top:
            cur = None
            in_default = False
            if term in _norm(top.group(1)):
                cur = {
                    "name": top.group(1),
                    "line": i,
                    "value": "",
                    "type": "",
                    "defaults": [],
                }
                out.append(cur)
            continue
        if cur is None:
            continue
        ty = re.match(r"^  type:\s*(\S+)", line)
        if ty:
            cur["type"] = ty.group(1)
        st = re.match(r"^  status:\s*(\S+)", line)
        if st:
            cur["value"] = st.group(1)
        if re.match(r"^  defaultValue:", line):
            in_default = True
            continue
        if in_default:
            if re.match(r"^  \S", line) or not line.strip():
                in_default = False
            else:
                kv = re.match(r"^\s+(\S+):\s*(\S.*)$", line)
                if kv:
                    cur["defaults"].append(f"{kv.group(1)}: {kv.group(2)}")
    for e in out:
        v = e["value"]
        if v.endswith("_feature_status"):
            # Set per platform by the build (e.g. Webgpu_feature_status).
            e["status"] = f"set per build ({v})"
        else:
            e["status"] = WEBKIT_STATUS.get(v, v or "?")
        if e["defaults"]:
            e["status"] += f"  [{', '.join(e['defaults'])}]"
    return out


def flag_engine(engine: str, term: str, limit: int) -> EngineResult:
    path = FLAG_FILES[engine]
    norm = _norm(term)
    if not norm:
        return EngineResult(engine, error="empty flag name")
    try:
        if engine == "gecko":
            lines = gecko_cat(path, None)
            entries = gecko_flags(lines, norm)
        elif engine == "webkit":
            lines = webkit.fetch_file_contents(path)
            entries = webkit_flags(lines, norm)
        else:
            try:
                lines = chromium.fetch_file_contents(path)
            except RuntimeError:
                # source.chromium.org sometimes answers 401 for a moment.
                lines = chromium.fetch_file_contents(path)
            entries = blink_flags(lines, norm)
    except (RuntimeError, ValueError, OSError, urllib.error.URLError) as exc:
        return EngineResult(engine, error=str(exc))
    res = EngineResult(engine)

    def is_switch(e: dict) -> bool:
        return engine in ("blink", "chromium") or "bool" in e.get("type", "").lower()

    # Real on/off switches first, then shorter (more specific) names.
    entries.sort(key=lambda e: (not is_switch(e), len(e["name"])))
    for e in entries[:limit]:
        if engine == "gecko":
            url = f"{SEARCHFOX}/{GECKO_TREE}/source/{path}#{e['line']}"
        elif engine == "webkit":
            url = webkit.line_url(path, e["line"], "", "searchfox")
        else:
            url = chromium.line_url(path, e["line"])
        res.rows.append(
            Row(
                engine=engine,
                path=path,
                line=e["line"],
                text=e["name"],
                url=url,
                source="github" if engine != "blink" else "grimoire",
                kind="flag" if is_switch(e) else "setting",
                extra={"flag": e["name"], "value": e["value"], "status": e["status"]},
            )
        )
    if len(entries) > limit:
        res.notes.append(f"{len(entries) - limit} more matching flags (-L)")
    if not entries:
        res.notes.append(
            "no matching flag: it may have shipped and had its flag removed, "
            f"or not exist yet. Try `compare {term}` to look for code."
        )
    return res


def print_flags(results: list[EngineResult]) -> None:
    for i, res in enumerate(results):
        if i:
            sys.stdout.write("\n")
        path = FLAG_FILES[res.engine]
        head = paint("== " + res.engine, "bold", "cyan") + " " + paint(path, "dim")
        if res.error:
            sys.stdout.write(f"{head}  {paint('error: ' + res.error, 'red')}\n")
            continue
        sys.stdout.write(head + "\n")
        for msg in res.notes:
            sys.stdout.write(paint(f"   note: {msg}", "dim") + "\n")
        width = max((len(r.text) for r in res.rows), default=0)
        for r in res.rows:
            name = paint(r.text.ljust(width), "green")
            status = r.extra["status"]
            if r.kind == "setting":
                status = paint("(setting) ", "dim") + status
            sys.stdout.write(f"{name}  {status}  {paint(':' + str(r.line), 'dim')}\n")


# ---------------------------------------------------------------------------
# Commit history (compare --news, --history) via GitHub and Grimoire
# ---------------------------------------------------------------------------

GITHUB_REPOS = {
    "gecko": GECKO_GITHUB,
    "blink": "chromium/chromium",
    "chromium": "chromium/chromium",
    "webkit": "WebKit/WebKit",
}


def github_api(path: str, params: dict[str, str]) -> object:
    url = f"https://api.github.com/{path}?{urllib.parse.urlencode(params)}"
    # No auth, on purpose. Unauthenticated limits: 10 searches a minute and
    # 60 other requests an hour, per IP.
    req = urllib.request.Request(
        url, headers={"Accept": "application/vnd.github+json", "User-Agent": VERSION}
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 429):
            raise RuntimeError(
                "GitHub API rate limit hit (10 searches a minute without a token); "
                "try again in a minute"
            ) from exc
        if exc.code == 422:
            raise RuntimeError("GitHub rejected the query") from exc
        raise RuntimeError(f"GitHub returned {exc.code} {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"network error: {exc.reason}") from exc


def parse_since(text: str | None) -> str:
    """Turn 2w / 30d / 3m / 1y / YYYY-MM-DD into an ISO date ("" = no limit)."""
    if not text:
        return ""
    m = re.fullmatch(r"(\d+)([dwmy])", text)
    if m:
        days = int(m.group(1)) * {"d": 1, "w": 7, "m": 30, "y": 365}[m.group(2)]
        return (date.today() - timedelta(days=days)).isoformat()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    raise ValueError("--since takes 2w, 30d, 3m, 1y or YYYY-MM-DD")


def commit_row(
    engine: str, sha: str, day: str, author: str, subject: str, url: str, kind: str
) -> Row:
    return Row(
        engine=engine,
        path="",
        line=0,
        text=subject,
        url=url,
        source="github" if "github.com" in url else "grimoire",
        kind=kind,
        extra={"sha": sha, "date": day, "author": author, "subject": subject},
    )


def commit_search(
    engine: str, term: str, since: str, order: str, n: int, kind: str = "commit"
) -> tuple[list[Row], int]:
    q = f"repo:{GITHUB_REPOS[engine]} {term}"
    if since:
        q += f" committer-date:>={since}"
    data = github_api(
        "search/commits",
        {"q": q, "sort": "committer-date", "order": order, "per_page": str(n)},
    )
    rows = []
    for it in data.get("items", []):
        c = it["commit"]
        rows.append(
            commit_row(
                engine,
                it["sha"],
                c["committer"]["date"][:10],
                c["author"]["name"],
                c["message"].split("\n", 1)[0],
                it["html_url"],
                kind,
            )
        )
    return rows, int(data.get("total_count", 0))


def path_history(engine: str, path: str, since: str, n: int) -> list[Row]:
    path = path.strip("/")
    if engine in ("blink", "chromium"):
        rows = []
        for e in chromium.fetch_history(path, "refs/heads/main", n):
            day = e.get("commitTime", "")[:10]
            if since and day < since:
                continue
            sha = e.get("commitId", "")
            rows.append(
                commit_row(
                    engine,
                    sha,
                    day,
                    e.get("author", {}).get("name", ""),
                    e.get("commitMessage", "").split("\n", 1)[0],
                    f"https://chromium.googlesource.com/chromium/src/+/{sha}",
                    "commit",
                )
            )
        return rows
    params = {"path": path, "per_page": str(n)}
    if since:
        params["since"] = f"{since}T00:00:00Z"
    data = github_api(f"repos/{GITHUB_REPOS[engine]}/commits", params)
    return [
        commit_row(
            engine,
            it["sha"],
            it["commit"]["committer"]["date"][:10],
            it["commit"]["author"]["name"],
            it["commit"]["message"].split("\n", 1)[0],
            it["html_url"],
            "commit",
        )
        for it in data
    ]


def news_engine(
    engine: str, term: str, user_path: str | None, since: str, limit: int
) -> EngineResult:
    res = EngineResult(engine)
    if user_path:
        rows = path_history(engine, user_path, since, 100)
        if term != "*":
            rows = [r for r in rows if term.lower() in r.text.lower()]
        res.rows = rows[:limit]
        count = f"{len(rows)}+" if len(rows) >= 100 else str(len(rows))
        res.summary = f"{count} commits in {user_path.strip('/')}/" + (
            f" mentioning {term!r}" if term != "*" else ""
        )
        return res
    if term == "*":
        raise RuntimeError("`*` needs a path, e.g. --gecko-path layout/generic")
    res.rows, total = commit_search(engine, term, since, "desc", limit)
    res.summary = f"{total} commits mention {term!r}" + (
        f" since {since}" if since else ""
    )
    if engine == "blink":
        res.notes.append(
            "commit search covers all of chromium/src (--blink-path to scope)"
        )
    return res


def history_engine(engine: str, term: str) -> EngineResult:
    res = EngineResult(engine)
    first, total = commit_search(engine, term, "", "asc", 3, "first")
    latest, _ = commit_search(engine, term, "", "desc", 3, "latest")
    seen = {r.extra["sha"] for r in first}
    res.rows = first + [r for r in latest if r.extra["sha"] not in seen]
    if first:
        res.summary = (
            f"{total} commits mention {term!r}; first {first[0].extra['date']}, "
            f"latest {latest[0].extra['date'] if latest else '?'}"
        )
    else:
        res.summary = f"no commits mention {term!r}"
    return res


# ---------------------------------------------------------------------------
# IDL (compare --idl)
# ---------------------------------------------------------------------------

# `\S*\]` allows `] interface X` and `[Exposed=Window] interface X` on one line.
# (Grimoire mishandles `(^|...)`, so keep the anchor outside the group.)
IDL_DEF = r"^\s*(\S*\]\s*)?(partial\s+)?(callback\s+)?interface\s+(mixin\s+)?{name}\b"


def _is_spec_copy(path: str) -> bool:
    # Each engine vendors WPT's interfaces/*.idl, which is the spec's own IDL.
    return "/interfaces/" in path and ("wpt" in path or "web-platform-tests" in path)


def idl_members(block: str) -> list[str]:
    text = re.sub(r"//[^\n]*", "", block)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    prev = None
    while prev != text:  # strip [extended attributes], innermost first
        prev = text
        text = re.sub(r"\[[^\[\]]*\]", "", text)
    if "{" not in text:
        return []
    body = text.split("{", 1)[1].rsplit("}", 1)[0]
    out: list[str] = []
    for stmt in body.split(";"):
        st = " ".join(stmt.split())
        if not st:
            continue
        if st.startswith("constructor"):
            name = "constructor()"
        elif (m := re.search(r"\battribute\b.*?(\w+)$", st)) or (
            st.startswith("const ") and (m := re.search(r"(\w+)\s*=", st))
        ):
            name = m.group(1)
        elif m := re.search(r"(\w+)\s*\(", st):
            name = m.group(1) + "()"
        elif m := re.match(
            r"(async iterable|iterable|maplike|setlike|stringifier)", st
        ):
            name = m.group(1)
        else:
            continue
        if name not in out:
            out.append(name)
    return out


def _fetch_lines(engine: str, path: str) -> list[str]:
    if engine == "gecko":
        return gecko_cat(path, None)
    if engine == "webkit":
        return webkit.fetch_file_contents(path)
    return chromium.fetch_file_contents(path)


def idl_block(lines: list[str], start: int) -> str:
    """The interface block that starts at 1-based line `start`."""
    out = []
    for line in lines[start - 1 : start + 400]:
        out.append(line)
        if re.match(r"\s*};", line):
            break
    return "\n".join(out)


def idl_engine(engine: str, name: str) -> EngineResult:
    res = EngineResult(engine)
    regex = IDL_DEF.format(name=name)
    if engine in ("gecko", "webkit"):
        tree = GECKO_TREE if engine == "gecko" else WEBKIT_TREE
        ext = ".webidl" if engine == "gecko" else ".idl"
        data = searchfox_search(regex, tree=tree, regexp=True, case=True, path=ext)
        hits = [
            (r.path, r.line) for r in searchfox_rows(data, engine=engine, tree=tree)
        ]
    else:
        scope = _scope("third_party/blink") if engine == "blink" else ""
        hits = []
        for page in chromium.search_all(
            f"{scope}case:yes file:\\.idl$ {regex}", limit=30, context_lines=1
        ):
            for f in page.files:
                for sn in f.snippets:
                    hits += [(f.path, ln.line_number) for ln in sn.lines if ln.ranges]
    members: list[str] = []
    for path, line in hits:
        spec = _is_spec_copy(path)
        if spec:
            res.data.setdefault("spec", (path, line))
            continue
        if is_vendored(path):
            continue
        block = idl_block(_fetch_lines(engine, path), line)
        found = idl_members(block)
        members += [m for m in found if m not in members]
        url = (
            f"{SEARCHFOX}/{GECKO_TREE}/source/{path}#{line}"
            if engine == "gecko"
            else webkit.line_url(path, line, "", "searchfox")
            if engine == "webkit"
            else chromium.line_url(path, line)
        )
        header = block.split("{", 1)[0].strip().splitlines()[-1] if block else ""
        res.rows.append(
            Row(
                engine=engine,
                path=path,
                line=line,
                text=header,
                url=url,
                source="searchfox" if engine != "blink" else "grimoire",
                kind="idl",
                extra={"members": found},
            )
        )
        if len(res.rows) >= 4:
            break
    res.data["members"] = members
    res.summary = (
        f"{len(members)} members in {len(res.rows)} file(s)"
        if res.rows
        else f"no interface {name} in this engine's IDL"
    )
    return res


def spec_members(results: list[EngineResult]) -> tuple[str, list[str]]:
    for res in results:
        if "spec" in res.data:
            path, line = res.data["spec"]
            try:
                block = idl_block(_fetch_lines(res.engine, path), line)
            except (RuntimeError, OSError, urllib.error.URLError):
                continue
            return path, idl_members(block)
    return "", []


def print_idl(results: list[EngineResult], spec_path: str, spec: list[str]) -> None:
    for res in results:
        head = paint("== " + res.engine, "bold", "cyan")
        if res.error:
            sys.stdout.write(f"{head}  {paint('error: ' + res.error, 'red')}\n")
            continue
        sys.stdout.write(f"{head}  {paint(res.summary, 'dim')}\n")
        for r in res.rows:
            sys.stdout.write(f"{paint(r.location(), 'green')}: {r.text}\n")
    engines = [r for r in results if not r.error]
    names: list[str] = list(spec)
    for res in engines:
        names += [m for m in res.data.get("members", []) if m not in names]
    if not names:
        return
    cols = (["spec"] if spec else []) + [r.engine for r in engines]
    width = max(len(n) for n in names)
    sys.stdout.write("\n" + paint("member".ljust(width), "bold"))
    sys.stdout.write("  " + "  ".join(c.ljust(6) for c in cols) + "\n")
    for n in names:
        marks = (["yes" if n in spec else "-"] if spec else []) + [
            "yes" if n in r.data.get("members", []) else "-" for r in engines
        ]
        differs = len(set(marks)) > 1
        cells = "  ".join(
            paint(m.ljust(6), "green" if m == "yes" else "red")
            if differs
            else m.ljust(6)
            for m in marks
        )
        sys.stdout.write(f"{n.ljust(width)}  {cells}\n")
    if spec:
        sys.stdout.write(paint(f"\nspec column: {spec_path}", "dim") + "\n")


# ---------------------------------------------------------------------------
# Spec links (compare --spec)
# ---------------------------------------------------------------------------


def _rx(text: str) -> str:
    # Escape only what regex engines treat specially. re.escape also escapes
    # characters like '#' and '-', which some backends reject.
    return re.sub(r"([.^$*+?()\[\]{}|\\/])", r"\\\1", text)


def spec_regex(url: str) -> str:
    u = urllib.parse.urlsplit(url if "://" in url else "https://" + url)
    if not u.netloc:
        raise ValueError("--spec takes a spec URL, e.g. https://drafts.csswg.org/x/#y")
    if u.fragment:
        # Engines cite single-page and multipage URLs; match host + fragment.
        return _rx(u.netloc) + r"\S*#" + _rx(u.fragment) + r"([^\w-]|$)"
    return _rx(u.netloc + u.path.rstrip("/"))


# ---------------------------------------------------------------------------
# WPT expectations (compare --wpt)
# ---------------------------------------------------------------------------

WPT_ROOTS = {
    "gecko": "testing/web-platform/tests/",
    "blink": "third_party/blink/web_tests/external/wpt/",
    "chromium": "third_party/blink/web_tests/external/wpt/",
    "webkit": "LayoutTests/imported/w3c/web-platform-tests/",
}
NOT_PASS = re.compile(r"\b(FAIL|TIMEOUT|ERROR|CRASH|NOTRUN|PRECONDITION_FAILED)\b")


def wpt_test_path(raw: str) -> str:
    t = raw.strip()
    if "wpt.fyi/results/" in t:
        t = t.split("wpt.fyi/results/", 1)[1].split("?", 1)[0]
    for root in (
        *WPT_ROOTS.values(),
        "external/wpt/",
        "imported/w3c/web-platform-tests/",
        "web-platform-tests/",
    ):
        if root in t:
            t = t.split(root, 1)[1]
    t = t.strip("/")
    if not t or ".." in t.split("/"):
        raise ValueError("--wpt takes a test path, e.g. css/css-grid/grid-001.html")
    return t


def _wpt_base(test: str) -> str:
    return re.sub(r"\.(html|htm|xhtml|xht|svg|js)$", "", test)


def wpt_gecko(test: str) -> EngineResult:
    res = EngineResult("gecko")
    meta = f"testing/web-platform/meta/{test}.ini"
    try:
        lines = gecko_cat(meta, None)
    except RuntimeError as exc:
        if "not found" not in str(exc).lower():
            raise
        res.summary = "no metadata file: expected to pass"
        return res
    subtest = test.rsplit("/", 1)[-1]
    flagged: dict[str, int] = {}
    in_expected = False
    for i, line in enumerate(lines, 1):
        # [test] at column 0, [subtest] indented two spaces.
        m = re.match(r"\s*\[(.*)\]\s*$", line)
        if m and not line.startswith("    "):
            subtest = m.group(1)
            in_expected = False
            continue
        if re.match(r"\s*expected:", line):
            in_expected = True
        elif re.match(r"\s*\w+:", line) and not re.match(r"\s*if ", line):
            in_expected = False
        if in_expected and NOT_PASS.search(line) and subtest not in flagged:
            flagged[subtest] = i
    for name, i in flagged.items():
        res.rows.append(
            Row(
                engine="gecko",
                path=meta,
                line=i,
                text=f"{name}: {lines[i - 1].strip()}",
                url=f"{SEARCHFOX}/{GECKO_TREE}/source/{meta}#{i}",
                source="github",
                kind="metadata",
            )
        )
    res.summary = (
        f"{len(flagged)} test/subtest(s) expected not to pass"
        if flagged
        else "metadata file, but no failing expectations (prefs or conditions)"
    )
    return res


def _count_baseline(lines: list[str]) -> tuple[int, int]:
    fails = sum(1 for ln in lines if re.match(r"\[?(FAIL|TIMEOUT|NOTRUN)\b", ln))
    passes = sum(1 for ln in lines if re.match(r"\[?PASS\b", ln))
    return fails, passes


def wpt_chromium(engine: str, test: str, limit: int) -> EngineResult:
    res = EngineResult(engine)
    q = f"file:(TestExpectations|NeverFixTests|SlowTests)$ {re.escape(test)}"
    for page in chromium.search_all(q, limit=limit, context_lines=1):
        for f in page.files:
            for sn in f.snippets:
                for ln in sn.lines:
                    if ln.ranges:
                        res.rows.append(
                            Row(
                                engine=engine,
                                path=f.path,
                                line=ln.line_number,
                                text=ln.line_text.strip(),
                                url=chromium.line_url(f.path, ln.line_number),
                                source="grimoire",
                                kind="expectation",
                            )
                        )
    base = _wpt_base(test)
    name = base.rsplit("/", 1)[-1] + "-expected.txt"
    payload = chromium.build_suggest_payload(
        "file:^third_party/blink/web_tests/ " + name, max_suggestions=50
    )
    baselines = [
        p
        for p in chromium.fetch_suggest(chromium.DEFAULT_API_KEY, payload, TIMEOUT)
        if p.endswith(base + "-expected.txt")
    ]
    generic = WPT_ROOTS["blink"] + base + "-expected.txt"
    verdict = []
    if generic in baselines:
        fails, passes = _count_baseline(chromium.fetch_file_contents(generic))
        verdict.append(f"baseline: {fails} failing, {passes} passing subtests")
    for p in baselines:
        res.rows.append(
            Row(
                engine=engine,
                path=p,
                line=0,
                text="",
                url=chromium.file_url(p),
                source="grimoire",
                kind="baseline",
            )
        )
    if res.rows and not verdict:
        verdict.append(f"{len(res.rows)} expectation line(s) or platform baselines")
    res.summary = "; ".join(verdict) or "no expectations or failure baseline: passes"
    return res


def wpt_webkit(test: str, limit: int) -> EngineResult:
    res = EngineResult("webkit")
    out = webkit.search_files(f"file:TestExpectations {test}", limit=limit)
    for f, _si, ln in webkit.iter_lines(out.files, limit):
        if ln.ranges:
            res.rows.append(
                Row(
                    engine="webkit",
                    path=f.path,
                    line=ln.line_number,
                    text=ln.line_text.strip(),
                    url=webkit.line_url(f.path, ln.line_number, f.commit, f.source),
                    source=f.source,
                    kind="expectation",
                )
            )
    base = _wpt_base(test)
    generic = WPT_ROOTS["webkit"] + base + "-expected.txt"
    try:
        fails, passes = _count_baseline(webkit.fetch_file_contents(generic))
        verdict = f"baseline: {fails} failing, {passes} passing subtests"
        res.rows.append(
            Row(
                engine="webkit",
                path=generic,
                line=0,
                text="",
                url=webkit.line_url(generic, 1, "", "searchfox").split("#")[0],
                source="github",
                kind="baseline",
            )
        )
    except RuntimeError:
        verdict = "no text baseline (a reftest, or not imported)"
    if res.rows and any(r.kind == "expectation" for r in res.rows):
        verdict += (
            f"; {sum(r.kind == 'expectation' for r in res.rows)} expectation line(s)"
        )
    res.summary = verdict
    return res


def wpt_engine(engine: str, raw: str, limit: int) -> EngineResult:
    test = wpt_test_path(raw)
    try:
        _fetch_lines(engine, WPT_ROOTS[engine] + test)
    except RuntimeError as exc:
        if "not found" in str(exc).lower():
            return EngineResult(
                engine, summary="test not found in this engine's WPT copy"
            )
        raise
    if engine == "gecko":
        return wpt_gecko(test)
    if engine == "webkit":
        return wpt_webkit(test, limit)
    return wpt_chromium(engine, test, limit)


# ---------------------------------------------------------------------------
# Brief (compare --brief): one page per feature
# ---------------------------------------------------------------------------


def brief(term: str, engines: list[str], limit: int) -> dict[str, dict]:
    want_idl = bool(re.fullmatch(r"[A-Z]\w+", term))
    q = Query(term, "term")
    jobs = {}
    with ThreadPoolExecutor(max_workers=len(engines) * 4) as pool:
        for e in engines:
            jobs[(e, "flags")] = pool.submit(flag_engine, e, term, limit)
            jobs[(e, "files")] = pool.submit(
                find_engine, e, term, DEFAULT_PATHS.get(e, ""), limit, False
            )
            jobs[(e, "definitions")] = pool.submit(
                search_engine, e, q, DEFAULT_PATHS.get(e, ""), limit, True, False
            )
            if want_idl:
                jobs[(e, "idl")] = pool.submit(
                    run_engine, partial(idl_engine, e, term), e, limit, True
                )
        return {
            e: {part: jobs[(e, part)].result() for (en, part) in jobs if en == e}
            for e in engines
        }


def print_brief(term: str, parts: dict[str, dict]) -> None:
    out = [f"# {term} across engines", ""]
    spec_path, spec = spec_members([p["idl"] for p in parts.values() if "idl" in p])
    for engine, sections in parts.items():
        out.append(f"## {engine}")
        for part, res in sections.items():
            label = part.capitalize()
            if res.error:
                out.append(f"- **{label}:** error: {res.error}")
                continue
            if part == "flags":
                items = [
                    f"`{r.text}` {r.extra['status']} ([source]({r.url}))"
                    for r in res.rows[:5]
                ]
                out.append("- **Flags:** " + ("; ".join(items) or "no matching flag"))
            elif part == "idl":
                out.append(f"- **IDL:** {res.summary}")
                out += [f"  - [`{r.location()}`]({r.url}) `{r.text}`" for r in res.rows]
            else:
                out.append(f"- **{label}:**" + ("" if res.rows else " none found"))
                out += [
                    f"  - [`{r.location()}`]({r.url})"
                    + (f" `{r.text[:80]}`" if r.text else "")
                    for r in res.rows[:5]
                ]
        out.append("")
    idl_res = [p["idl"] for p in parts.values() if "idl" in p and not p["idl"].error]
    names = list(spec)
    for res in idl_res:
        names += [m for m in res.data.get("members", []) if m not in names]
    if names:
        cols = (["spec"] if spec else []) + [r.engine for r in idl_res]
        out += ["## IDL members", "", "| member | " + " | ".join(cols) + " |"]
        out.append("|---" * (len(cols) + 1) + "|")
        for n in names:
            marks = (["yes" if n in spec else "-"] if spec else []) + [
                "yes" if n in r.data.get("members", []) else "-" for r in idl_res
            ]
            out.append(f"| `{n}` | " + " | ".join(marks) + " |")
        if spec_path:
            out += ["", f"Spec column from `{spec_path}` (WPT's copy of the spec IDL)."]
    sys.stdout.write("\n".join(out).rstrip() + "\n")


def brief_json(term: str, parts: dict[str, dict]) -> dict:
    spec_path, spec = spec_members([p["idl"] for p in parts.values() if "idl" in p])
    return {
        "query": {"text": term, "type": "brief"},
        "spec_idl": {"path": spec_path, "members": spec} if spec_path else None,
        "engines": {
            e: {
                part: {
                    "results": [r.as_dict() for r in res.rows],
                    "error": res.error,
                    "notes": res.notes,
                    "summary": res.summary,
                    **(
                        {"members": res.data.get("members", [])}
                        if part == "idl"
                        else {}
                    ),
                }
                for part, res in sections.items()
            }
            for e, sections in parts.items()
        },
    }


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


MODES = ("defs", "find", "flag", "news", "history", "idl", "spec", "wpt", "brief")


def cmd_compare(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="cross-engine compare", add_help=False)
    p.add_argument("query", nargs="*")
    p.add_argument("--engines", default=",".join(ENGINES))
    p.add_argument("-L", "--limit", type=int, default=10)
    p.add_argument("--json", action="store_true")
    for m in MODES:
        p.add_argument(f"--{m}", action="store_true")
    p.add_argument("--since", default=None)
    p.add_argument("--all", action="store_true", dest="show_all")
    for e in (*ENGINES, "chromium"):
        p.add_argument(f"--{e}-path", default=None)
    p.add_argument("-h", "--help", action="store_true")
    opts = p.parse_args(argv)
    raw = " ".join(opts.query).strip()
    if opts.help or not raw:
        sys.stderr.write(COMPARE_USAGE)
        return 0 if opts.help else 1
    chosen = [m for m in MODES if getattr(opts, m)]
    if len(chosen) > 1:
        sys.stderr.write(
            f"Error: pick one mode, not {' and '.join('--' + m for m in chosen)}\n"
        )
        return 1
    mode = chosen[0] if chosen else "search"
    if opts.since and mode != "news":
        sys.stderr.write("Error: --since only applies to --news\n")
        return 1

    engines = parse_engines(opts.engines)
    user_paths = {e: getattr(opts, f"{e}_path") for e in engines}
    paths = {e: user_paths[e] or DEFAULT_PATHS.get(e, "") for e in engines}
    limit, show_all = opts.limit, opts.show_all

    try:
        if mode in ("search", "defs"):
            q = parse_query(raw)
            if mode == "defs" and q.mode != "term":
                raise ValueError("--defs takes a single symbol name")
        elif mode == "spec":
            q = Query(spec_regex(raw), "regex")
        elif mode == "idl" and not re.fullmatch(r"\w+", raw):
            raise ValueError("--idl takes an interface name, e.g. ResizeObserverEntry")
        else:
            q = Query(raw, mode)
        since = parse_since(opts.since)
        if mode == "wpt":
            wpt_test_path(raw)
    except ValueError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1

    if mode == "brief":
        parts = brief(raw, engines, min(limit, 5))
        if opts.json:
            write_json(brief_json(raw, parts))
        else:
            print_brief(raw, parts)
        return 0

    def job(e: str) -> EngineResult:
        if mode in ("search", "defs", "spec"):
            return search_engine(e, q, paths[e], limit, mode == "defs", show_all)
        if mode == "find":
            return find_engine(e, raw, paths[e], limit, show_all)
        if mode == "flag":
            return flag_engine(e, raw, limit)
        if mode == "news":
            return run_engine(
                partial(news_engine, e, raw, user_paths[e], since, limit),
                e,
                limit,
                True,
            )
        if mode == "history":
            return run_engine(partial(history_engine, e, raw), e, 10, True)
        if mode == "idl":
            return run_engine(partial(idl_engine, e, raw), e, limit, True)
        return run_engine(partial(wpt_engine, e, raw, limit), e, limit, True)

    with ThreadPoolExecutor(max_workers=len(engines)) as pool:
        results = list(pool.map(job, engines))

    for res in results:
        if (
            res.engine == "blink"
            and mode in ("search", "defs", "find", "spec")
            and not res.rows
            and not res.error
        ):
            res.notes.append(
                "blink only covers third_party/blink; "
                "use --engines gecko,chromium,webkit for build or infra code"
            )

    spec_path, spec = spec_members(results) if mode == "idl" else ("", [])
    if opts.json:
        out = {
            "query": {"text": q.text if mode != "spec" else raw, "type": mode},
            "results": [r.as_dict() for res in results for r in res.rows],
            "errors": {res.engine: res.error for res in results if res.error},
            "notes": {res.engine: res.notes for res in results if res.notes},
        }
        if mode in ("search", "defs"):
            out["query"].update(mode=q.mode, case=q.case)
            out["start"] = {res.engine: res.start for res in results if res.start}
        if mode == "spec":
            out["query"]["regex"] = q.text
        if any(res.summary for res in results):
            out["summary"] = {res.engine: res.summary for res in results if res.summary}
        if mode == "idl":
            out["members"] = {
                res.engine: res.data.get("members", []) for res in results
            }
            if spec_path:
                out["members"]["spec"] = spec
                out["spec_idl"] = spec_path
        write_json(out)
    elif mode == "flag":
        print_flags(results)
    elif mode == "idl":
        print_idl(results, spec_path, spec)
    else:
        print_compare(
            results, paths if mode in ("search", "defs", "find", "spec") else {}
        )
    return 0 if any(not r.error for r in results) else 1


def print_compare(results: list[EngineResult], paths: dict[str, str]) -> None:
    for i, res in enumerate(results):
        if i:
            sys.stdout.write("\n")
        scope = paths.get(res.engine, "")
        scope = f" {paint(scope + '/', 'dim')}" if scope else ""
        if res.error:
            status = paint(f"error: {res.error}", "red")
        elif res.summary:
            status = paint(res.summary, "dim")
        else:
            n = len(res.rows)
            status = paint(f"{n} result{'s' if n != 1 else ''}", "dim")
        sys.stdout.write(
            f"{paint('== ' + res.engine, 'bold', 'cyan')}{scope}  {status}\n"
        )
        for msg in res.notes:
            sys.stdout.write(paint(f"   note: {msg}", "dim") + "\n")
        if res.start:
            sys.stdout.write(
                paint("   start here: ", "dim") + ", ".join(res.start) + "\n"
            )
        for r in res.rows:
            if r.kind in ("commit", "first", "latest"):
                tag = f"{r.kind:<7}" if r.kind != "commit" else ""
                sha = paint(r.extra["sha"][:10], "green")
                who = paint(f"({r.extra['author']})", "dim")
                sys.stdout.write(f"{tag}{r.extra['date']} {sha} {r.text} {who}\n")
                continue
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
            sys.stderr.write(f"Usage: cross-engine gecko {sub} <symbol>\n")
            return 1
        return subprocess.call([cli, "-R", GECKO_TREE, f"--{sub}", *argv[1:]])

    p = argparse.ArgumentParser(prog="cross-engine gecko", add_help=False)
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
                sys.stderr.write("Usage: cross-engine gecko cat <path> [--ref]\n")
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
            sys.stderr.write("Usage: cross-engine gecko find <name>\n")
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
Usage: cross-engine <engine|command> [args]

Engines:
  gecko <args>             Firefox: search, find, cat, ls, define, ... (`gecko -h`)
  chromium <args>          Chromium: search, find, cat, ls, log, repos (`chromium -h`)
  blink <args>             Like chromium, with search/find scoped to third_party/blink
  webkit <args>            WebKit: search, sym, find, cat, ls, define, ... (`webkit -h`)

Commands:
  compare <query>          Same query on every engine, side by side
  compare --find <name>    Compare file and directory names across engines
  compare --flag <feature> Is it shipped, experimental or missing in each engine?
  compare --idl <Iface>    IDL members per engine, against the spec
  compare --wpt <test>     Each engine's expectations for a WPT test
  compare --news <term>    Recent commits per engine (--since 2w)
  compare --brief <name>   One page: flags, files, definitions, IDL
  doctor                   Check that each backend is reachable
  version                  Print the version

Aliases: firefox=gecko, chrome=chromium, safari=webkit.
Run `cross-engine compare -h` for compare flags.

Examples:
  cross-engine compare scrollend
  cross-engine compare --find pgo --engines gecko,chromium,webkit
  cross-engine compare --flag anchor-position
  cross-engine compare --idl ResizeObserverEntry
  cross-engine compare --wpt css/css-anchor-position/anchor-scroll-001.html
  cross-engine compare --news scrollend --since 3m
  cross-engine compare --spec 'https://html.spec.whatwg.org/#navigate'
  cross-engine compare '/ResizeObserver\\w*/' -L 5
  cross-engine compare --defs ResizeObserverEntry --json
  cross-engine gecko 'mach python' --path build/pgo
  cross-engine gecko cat build/pgo/profileserver.py
  cross-engine gecko define 'mozilla::dom::ResizeObserver'
  cross-engine blink 'class:ResizeObserver lang:cpp'
  cross-engine webkit sym RenderBox --kind class
"""

COMPARE_USAGE = """\
Usage: cross-engine compare [mode] <query> [flags]

Pick a topic and see all three engines. Without a mode, compare searches code:
the query is one term, a "phrase" or a /regex/, plus optional case:yes, and
each engine gets it in its own syntax.

Modes:
  (none)                   Code search. Prints "start here" docs/build hits.
  --find <name>            File and directory names. A good first step for
                           "how does X work" questions.
  --defs <symbol>          Definitions of a symbol only
  --flag <feature>         Feature-flag status: on, experimental, or no flag?
                           Matches flag names containing the term, ignoring
                           case and punctuation. Real on/off switches first.
  --idl <Interface>        Each engine's IDL members side by side, with the
                           spec's IDL (from WPT) as a reference column
  --spec <url>             Code that cites a spec section (host + #fragment)
  --wpt <test>             Each engine's expectations for a WPT test: Gecko
                           metadata, Blink and WebKit expectations and baselines.
                           Takes a test path, any engine's path, or a wpt.fyi URL.
  --news <term>            Recent commits mentioning the term. With
                           --<engine>-path, commits in that directory instead
                           (use * as the term to list them all).
  --history <term>         First and latest commits mentioning the term
  --brief <feature>        One Markdown page: flags, files, definitions and IDL

Flags:
  --engines <list>         Comma list (default: gecko,blink,webkit; also chromium).
                           blink only covers third_party/blink. For build, infra
                           or docs topics, use gecko,chromium,webkit.
  -L, --limit <n>          Results per engine (default: 10)
  --since <when>           --news only: 2w, 30d, 3m, 1y or YYYY-MM-DD
  --all                    Include third-party, vendored, generated and tooling
                           code (hidden by default: third_party/,
                           Source/ThirdParty/, PerformanceTests/, Websites/,
                           dot-directories, searchfox's third-party and
                           generated groups)
  --json                   {query, results: [{engine,path,line,text,url,source,kind?}],
                            errors, notes, summary?}. line is 0 for file-name
                           matches. Modes add fields (flag/status, sha/date,
                           members), and --brief returns {engines: {...}}.
  --gecko-path <dir>       Limit gecko to a directory
  --blink-path <dir>       Limit blink (default: third_party/blink)
  --chromium-path <dir>    Limit chromium
  --webkit-path <dir>      Limit webkit

--news and --history use GitHub's commit search: 10 searches a minute without
a token, and --history uses two per engine.
"""

GECKO_USAGE = """\
Usage: cross-engine gecko [search] <query> [-L n] [--path dir] [--json]
       cross-engine gecko find <name> [-L n] [--path dir] [--json]
       cross-engine gecko cat <path> [--ref ref] [-n] [--json]
       cross-engine gecko ls [dir] [--ref ref] [--json]
       cross-engine gecko define|calls-to|calls-from|field-layout <symbol>
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

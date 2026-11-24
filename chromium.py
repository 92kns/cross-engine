# Based on chromium-search (https://github.com/jclay/chromium-search).
# Copyright (c) 2026 Joel Clay. MIT License, see LICENSE.
"""Chromium backend for cross-engine (source.chromium.org / Grimoire API).

Run it as `cross-engine chromium ...` or `cross-engine blink ...`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from typing import Any, TextIO

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass
class MatchRange:
    start: int
    end: int


@dataclass
class SnippetLine:
    line_text: str
    line_number: int
    ranges: list[MatchRange] = field(default_factory=list)


@dataclass
class Snippet:
    lines: list[SnippetLine] = field(default_factory=list)


@dataclass
class FileResult:
    path: str
    snippets: list[Snippet] = field(default_factory=list)


@dataclass
class SearchResult:
    files: list[FileResult] = field(default_factory=list)
    estimated_result_count: str = "0"
    next_page_token: str = ""


# ---------------------------------------------------------------------------
# Multipart request builder
# ---------------------------------------------------------------------------

# Public key that the source.chromium.org web app sends from every browser.
# Not a secret. Override with CR_SEARCH_API_KEY.
DEFAULT_API_KEY = "AIzaSyCqPSptx9mClE5NU4cpfzr6cgdO_phV1lM"
BASE_API_URL = "https://grimoireoss-pa.clients6.google.com"

PROJECT = os.environ.get("CR_SEARCH_PROJECT", "chromium")
REPO = os.environ.get("CR_SEARCH_REPO", "chromium/src")
PATH_PREFIX = ""


def set_scope(project: str, repo: str, path_prefix: str = "") -> None:
    global PROJECT, REPO, PATH_PREFIX
    PROJECT, REPO = project, repo
    PATH_PREFIX = path_prefix.strip("/") + "/" if path_prefix.strip("/") else ""


def repository_scope() -> dict[str, Any]:
    return {"root": {"ossProject": PROJECT, "repositoryName": REPO}}


def random_id() -> str:
    raw = secrets.token_bytes(9)
    out = ""
    for b in raw:
        out += _base36(b)
    return out[:12]


def _base36(n: int) -> str:
    if n == 0:
        return "0"
    chars = ""
    while n:
        n, r = divmod(n, 36)
        chars += "0123456789abcdefghijklmnopqrstuvwxyz"[r]
    return chars


def build_boundary() -> str:
    return f"batch{int(time.time() * 1000)}{random_id()}"


def build_payload(
    query: str,
    page_size: int = 100,
    page_token: str = "",
    context_lines: int = 1,
) -> dict[str, Any]:
    return {
        "queryString": query,
        "searchOptions": {
            "enableDiagnostics": False,
            "exhaustive": False,
            "numberOfContextLines": 1,
            "pageSize": min(page_size, 100),
            "pageToken": page_token,
            "pathPrefix": PATH_PREFIX,
            "repositoryScope": repository_scope(),
            "retrieveMultibranchResults": True,
            "savedQuery": "",
            "scoringModel": "",
            "showPersonalizedResults": False,
            "suppressGitLegacyResults": False,
        },
        "snippetOptions": {
            "numberOfContextLines": context_lines,
            "minSnippetLinesPerFile": max(2 * context_lines + 10, 10),
            "minSnippetLinesPerPage": max(2 * context_lines + 20, 60),
        },
    }


def build_request(
    boundary: str,
    api_key: str,
    payload: dict[str, Any],
    *,
    path: str = "/v1/contents/search",
) -> tuple[str, str]:
    url = f"{BASE_API_URL}/batch?%24ct=multipart%2Fmixed%3B%20boundary%3D{boundary}"
    body = "\r\n".join(
        [
            f"--{boundary}",
            "Content-Type: application/http",
            f"Content-ID: <response-{boundary}+gapiRequest@googleapis.com>",
            "",
            f"POST {path}?alt=json&key={api_key}",
            f"sessionid: {random_id()}",
            f"actionid: {random_id()}",
            "X-JavaScript-User-Agent: google-api-javascript-client/1.1.0",
            "X-Requested-With: XMLHttpRequest",
            "Content-Type: application/json",
            "X-Goog-Encode-Response-If-Executable: base64",
            "",
            json.dumps(payload),
            f"--{boundary}--",
            "",
        ]
    )
    return url, body


def extract_json(response_text: str) -> Any:
    m = re.search(r"\{[\s\S]*\}", response_text)
    if not m:
        raise RuntimeError("Could not parse API response: no JSON found")
    return json.loads(m.group(0))


def _send_batch(
    build: Callable[[str], tuple[str, str]], timeout: float
) -> dict[str, Any]:
    """POST a /batch request and return the inner JSON, raising on API errors.

    `build(boundary)` returns (url, body). The outer response is 200 even when
    the inner request fails, and the inner one is often a 401 that clears on a
    fresh request (new boundary and session ids), so retry those a few times.
    """
    for attempt in range(4):
        url, body = build(build_boundary())
        req = urllib.request.Request(
            url,
            data=body.encode(),
            headers={
                "Content-Type": "text/plain; charset=UTF-8",
                "Origin": "https://source.chromium.org",
                "Referer": "https://source.chromium.org/",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                raise RuntimeError(f"API request failed: {resp.status} {resp.reason}")
            raw = extract_json(resp.read().decode())
        err = raw.get("error") if isinstance(raw, dict) else None
        if not err:
            return raw
        if err.get("code") != 401 or attempt == 3:
            raise RuntimeError(err.get("message", "API error"))
        time.sleep(0.3 * (attempt + 1))
    raise AssertionError("unreachable")


def batch_get(path_and_query: str, timeout: float = 15.0) -> dict[str, Any]:
    """Run a GET through the same multipart /batch envelope used for search."""
    key = os.environ.get("CR_SEARCH_API_KEY", DEFAULT_API_KEY)

    def build(boundary: str) -> tuple[str, str]:
        url = f"{BASE_API_URL}/batch?%24ct=multipart%2Fmixed%3B%20boundary%3D{boundary}"
        body = "\r\n".join(
            [
                f"--{boundary}",
                "Content-Type: application/http",
                f"Content-ID: <response-{boundary}+gapiRequest@googleapis.com>",
                "",
                f"GET {path_and_query}&key={key}",
                "X-JavaScript-User-Agent: google-api-javascript-client/1.1.0",
                "X-Requested-With: XMLHttpRequest",
                "",
                f"--{boundary}--",
                "",
            ]
        )
        return url, body

    return _send_batch(build, timeout)


def fetch_history(path: str, ref: str, limit: int) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode(
        {
            "logForPath": "true",
            "logForPathWithPagination": "true",
            "maxLogEntries": str(limit),
            "path": path.strip("/"),
            "repositoryKey.ossProject": PROJECT,
            "repositoryKey.repositoryName": REPO,
            "starts": ref,
        }
    )
    return batch_get(f"/v1/history/list?{query}").get("commitLogEntries", [])


def fetch_repositories() -> list[dict[str, Any]]:
    data = batch_get(
        f"/v1/ossProjects/{urllib.parse.quote(PROJECT)}?multibranchEnabled=true"
    )
    return data.get("repositories", [])


# ---------------------------------------------------------------------------
# gRPC-Web transport (File Service)
# ---------------------------------------------------------------------------


def build_source_root(ref: str = "refs/heads/main") -> list[Any]:
    return [
        [None, REPO, None, None, PROJECT],
        None,
        ref,
        ref,
    ]


def fetch_grpc_web(
    api_key: str,
    body: list[Any],
    timeout: float,
    *,
    service: str = "devtools.grimoire.FileService",
    method: str = "GetContentsStreaming",
) -> Any:
    headers_str = (
        f"X-Goog-Api-Key:{api_key}\n"
        "Content-Type:application/json+protobuf\n"
        "X-User-Agent:grpc-web-javascript/0.1"
    )
    encoded_headers = urllib.parse.quote(headers_str, safe="")
    url = f"{BASE_API_URL}/$rpc/{service}/{method}?$httpHeaders={encoded_headers}"

    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "Origin": "https://source.chromium.org",
            "Referer": "https://source.chromium.org/",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status != 200:
            raise RuntimeError(f"API request failed: {resp.status}")
        return json.loads(resp.read().decode())


# ---------------------------------------------------------------------------
# Suggest client
# ---------------------------------------------------------------------------


def build_suggest_payload(
    query: str,
    max_suggestions: int = 10,
) -> dict[str, Any]:
    return {
        "queryString": query,
        "suggestOptions": {
            "enableDiagnostics": False,
            "maxSuggestions": max_suggestions,
            "pathPrefix": PATH_PREFIX,
            "repositoryScope": repository_scope(),
            "retrieveMultibranchResults": True,
            "savedQuery": "",
            "showPersonalizedResults": False,
            "suppressGitLegacyResults": False,
        },
    }


def fetch_suggest(
    api_key: str,
    payload: dict[str, Any],
    timeout: float,
) -> list[str]:
    raw = _send_batch(
        lambda b: build_request(b, api_key, payload, path="/v1/contents/suggest"),
        timeout,
    )
    return [
        s["fileSpec"]["path"]
        for s in raw.get("suggestions", [])
        if "fileSpec" in s and "path" in s["fileSpec"]
    ]


# ---------------------------------------------------------------------------
# File contents client
# ---------------------------------------------------------------------------


def fetch_file_contents(
    path: str,
    ref: str = "refs/heads/main",
    api_key: str | None = None,
    timeout: float = 15.0,
) -> list[str]:
    key = api_key or os.environ.get("CR_SEARCH_API_KEY", DEFAULT_API_KEY)
    body: list[Any] = [
        [build_source_root(ref), path],
        1,
        None,
        1,
        None,
        None,
        None,
        None,
        1,
    ]
    try:
        response = fetch_grpc_web(key, body, timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise RuntimeError(f"File not found: {path} at {ref}") from exc
        raise RuntimeError(f"Request failed ({exc.code} {exc.reason})") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error: {exc.reason}") from exc
    try:
        content = response[0][0][1][2]
    except (IndexError, TypeError) as exc:
        raise RuntimeError(f"No content returned for {path} at {ref}") from exc
    if not isinstance(content, str):
        raise RuntimeError(f"No content returned for {path} at {ref}")
    return content.split("\n")


ENTRY_TYPES = {1: "file", 3: "dir", 5: "submodule", 6: "symlink"}


def fetch_directory(
    path: str, ref: str = "refs/heads/main", timeout: float = 15.0
) -> list[tuple[str, str]]:
    """Return (name, type) for each immediate child of a directory."""
    key = os.environ.get("CR_SEARCH_API_KEY", DEFAULT_API_KEY)
    path = path.strip("/")
    try:
        response = fetch_grpc_web(key, [[build_source_root(ref), path], 1], timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise RuntimeError(f"Directory not found: {path or '/'} at {ref}") from exc
        raise RuntimeError(f"Request failed ({exc.code} {exc.reason})") from exc
    try:
        entries = response[0][0][0][0]
    except (IndexError, TypeError) as exc:
        raise RuntimeError(f"Not a directory: {path}") from exc
    out = [
        (e[0].rsplit("/", 1)[-1], ENTRY_TYPES.get(e[4], "other"))
        for e in entries
        if isinstance(e, list) and len(e) > 4
    ]
    if len(out) == 1 and out[0][1] == "file" and entries[0][0] == path:
        raise RuntimeError(f"Not a directory: {path} (use cat)")
    return out


# ---------------------------------------------------------------------------
# Search client
# ---------------------------------------------------------------------------


def parse_response(raw: dict[str, Any]) -> SearchResult:
    files: list[FileResult] = []
    for sr in raw.get("searchResults", []):
        fsr = sr.get("fileSearchResult")
        if not fsr:
            continue
        snippets: list[Snippet] = []
        for raw_snippet in fsr.get("snippets", []):
            lines: list[SnippetLine] = []
            for rl in raw_snippet.get("snippetLines", []):
                ranges = [
                    MatchRange(start=r.get("start", 0), end=r.get("end", 0))
                    for r in rl.get("ranges", [])
                ]
                lines.append(
                    SnippetLine(
                        line_text=rl.get("lineText", ""),
                        line_number=int(rl.get("lineNumber", "0")),
                        ranges=ranges,
                    )
                )
            snippets.append(Snippet(lines=lines))
        files.append(FileResult(path=fsr["fileSpec"]["path"], snippets=snippets))
    return SearchResult(
        files=files,
        estimated_result_count=raw.get("estimatedResultCount", "0"),
        next_page_token=raw.get("nextPageToken", ""),
    )


def fetch_search(
    api_key: str,
    payload: dict[str, Any],
    timeout: float,
) -> SearchResult:
    return parse_response(
        _send_batch(lambda b: build_request(b, api_key, payload), timeout)
    )


def search_all(
    query: str,
    limit: int = 25,
    context_lines: int = 1,
    api_key: str | None = None,
    timeout: float = 15.0,
) -> Generator[SearchResult]:
    key = api_key or os.environ.get("CR_SEARCH_API_KEY", DEFAULT_API_KEY)
    page_token = ""
    match_count = 0

    while match_count < limit:
        payload = build_payload(
            query,
            page_size=100,
            page_token=page_token,
            context_lines=context_lines,
        )
        result = fetch_search(key, payload, timeout)
        yield result

        for f in result.files:
            for s in f.snippets:
                for ln in s.lines:
                    if ln.ranges:
                        match_count += 1

        if not result.next_page_token or not result.files:
            break
        page_token = result.next_page_token


# ---------------------------------------------------------------------------
# Styling helpers
# ---------------------------------------------------------------------------

_force_no_color = False


def set_no_color(value: bool) -> None:
    global _force_no_color
    _force_no_color = value


def colors_enabled() -> bool:
    return _is_tty() and "NO_COLOR" not in os.environ and not _force_no_color


def _is_tty() -> bool:
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


_ANSI: dict[str, str] = {
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "cyan": "\033[36m",
    "yellow": "\033[33m",
    "reset": "\033[0m",
}


def style(text: str, *styles: str) -> str:
    if not colors_enabled():
        return text
    prefix = "".join(_ANSI.get(s, "") for s in styles)
    return f"{prefix}{text}{_ANSI['reset']}" if prefix else text


# ---------------------------------------------------------------------------
# Spinner
# ---------------------------------------------------------------------------

BRAILLE = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


class Spinner:
    def __init__(self, message: str, stream: TextIO = sys.stderr) -> None:
        self._message = message
        self._stream = stream
        self._active = False
        self._thread: threading.Thread | None = None

    def start(self) -> Spinner:
        if not (hasattr(self._stream, "isatty") and self._stream.isatty()):
            return self
        self._active = True
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()
        return self

    def _spin(self) -> None:
        i = 0
        while self._active:
            self._stream.write(f"\r{BRAILLE[i % len(BRAILLE)]} {self._message}")
            self._stream.flush()
            i += 1
            time.sleep(0.08)

    def stop(self) -> None:
        self._active = False
        if self._thread:
            self._thread.join()
        if hasattr(self._stream, "isatty") and self._stream.isatty():
            self._stream.write("\r\033[2K")
            self._stream.flush()


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def file_url(path: str) -> str:
    return f"https://source.chromium.org/{PROJECT}/{REPO}/+/main:{path}"


def line_url(path: str, line: int) -> str:
    return f"{file_url(path)};l={line}"


def highlight_line(line: SnippetLine) -> str:
    if not colors_enabled() or not line.ranges:
        return line.line_text
    result = ""
    pos = 0
    for r in line.ranges:
        result += line.line_text[pos : r.start]
        result += style(line.line_text[r.start : r.end], "bold", "red")
        pos = r.end
    result += line.line_text[pos:]
    return result


def count_matches(results: list[SearchResult]) -> int:
    count = 0
    for r in results:
        for f in r.files:
            for s in f.snippets:
                count += sum(1 for ln in s.lines if ln.ranges)
    return count


def format_grouped(
    results: list[SearchResult],
    context: bool,
    limit: int,
) -> str:
    lines: list[str] = []
    match_count = 0

    for result in results:
        for file in result.files:
            file_lines: list[str] = []
            file_has_output = False
            prev_snippet_end = -1

            for snippet in file.snippets:
                has_match = any(ln.ranges for ln in snippet.lines)
                if not has_match:
                    continue

                if (
                    prev_snippet_end >= 0
                    and snippet.lines
                    and snippet.lines[0].line_number > prev_snippet_end + 1
                ):
                    file_lines.append(style("    ...", "dim"))

                for ln in snippet.lines:
                    if ln.ranges:
                        if match_count >= limit:
                            break
                        match_count += 1
                        num = style(str(ln.line_number).rjust(5), "dim")
                        file_lines.append(f"{num}:  {highlight_line(ln)}")
                        file_has_output = True
                    elif context:
                        num = style(str(ln.line_number).rjust(5), "dim")
                        file_lines.append(style(f"{num}   {ln.line_text}", "dim"))

                if snippet.lines:
                    prev_snippet_end = snippet.lines[-1].line_number

                if match_count >= limit:
                    break

            if file_has_output:
                lines.append(style(file.path, "bold", "cyan"))
                lines.extend(file_lines)
                lines.append("")

            if match_count >= limit:
                break
        if match_count >= limit:
            break

    return "\n".join(lines)


def format_flat(
    results: list[SearchResult],
    context: bool,
    limit: int,
) -> str:
    lines: list[str] = []
    match_count = 0

    for result in results:
        for file in result.files:
            for snippet in file.snippets:
                has_match = any(ln.ranges for ln in snippet.lines)
                if not has_match:
                    continue

                for ln in snippet.lines:
                    if ln.ranges:
                        if match_count >= limit:
                            break
                        match_count += 1
                        lines.append(f"{file.path}:{ln.line_number}: {ln.line_text}")
                    elif context:
                        lines.append(f"{file.path}:{ln.line_number}- {ln.line_text}")
                if context and match_count < limit:
                    lines.append("--")

                if match_count >= limit:
                    break
            if match_count >= limit:
                break
        if match_count >= limit:
            break

    if context and lines and lines[-1] == "--":
        lines.pop()

    return "\n".join(lines)


JsonField = str  # one of: path, line, snippet, context, matchRanges, url


def format_json(
    results: list[SearchResult],
    fields: list[JsonField],
    limit: int,
) -> str:
    wants_context = "context" in fields
    entries: list[dict[str, Any]] = []
    match_count = 0

    for result in results:
        for file in result.files:
            if wants_context:
                for snippet in file.snippets:
                    match_lines = [ln for ln in snippet.lines if ln.ranges]
                    if not match_lines:
                        continue
                    if match_count >= limit:
                        break
                    match_count += len(match_lines)

                    entry: dict[str, Any] = {}
                    primary = match_lines[0]
                    for f in fields:
                        if f == "path":
                            entry["path"] = file.path
                        elif f == "line":
                            entry["line"] = primary.line_number
                        elif f == "snippet":
                            entry["snippet"] = primary.line_text
                        elif f == "context":
                            entry["context"] = [ln.line_text for ln in snippet.lines]
                        elif f == "matchRanges":
                            entry["matchRanges"] = [
                                {"start": r.start, "end": r.end} for r in primary.ranges
                            ]
                        elif f == "url":
                            entry["url"] = line_url(file.path, primary.line_number)
                    entries.append(entry)

                    if match_count >= limit:
                        break
            else:
                for snippet in file.snippets:
                    for ln in snippet.lines:
                        if not ln.ranges:
                            continue
                        if match_count >= limit:
                            break
                        match_count += 1

                        entry = {}
                        for f in fields:
                            if f == "path":
                                entry["path"] = file.path
                            elif f == "line":
                                entry["line"] = ln.line_number
                            elif f == "snippet":
                                entry["snippet"] = ln.line_text
                            elif f == "matchRanges":
                                entry["matchRanges"] = [
                                    {"start": r.start, "end": r.end} for r in ln.ranges
                                ]
                            elif f == "url":
                                entry["url"] = line_url(file.path, ln.line_number)
                        entries.append(entry)
                    if match_count >= limit:
                        break
            if match_count >= limit:
                break
        if match_count >= limit:
            break

    return json.dumps(entries, indent=2)


def summary_line(results: list[SearchResult], limit: int) -> str:
    total = count_matches(results)
    if total == 0:
        return ""

    estimated = results[0].estimated_result_count if results else "0"
    est = int(estimated)
    shown = min(total, limit)

    if est > total:
        return f"Showing {shown} of ~{est:,} results"
    if total > limit:
        return f"Showing {shown} of {total} results"
    return f"Showing {shown} results"


# ---------------------------------------------------------------------------
# Syntax reference
# ---------------------------------------------------------------------------

SYNTAX_REFERENCE = """\
Chromium Code Search — Query Syntax Reference

FILTERS
  case:yes              Case-sensitive search (default: case-insensitive)
  class:<name>          Search for class definitions
  comment:<text>        Search within comments only
  content:<text>        Search within file contents only (exclude file paths)
  file:<pattern>        Filter by file path (supports wildcards: file:*.cc)
  function:<name>       Search for function definitions
  lang:<language>       Filter by programming language
  pcre:<pattern>        Use PCRE regex syntax for search
  symbol:<name>         Search for symbol definitions
  usage:<name>          Search for usages of a symbol

LANG VALUES
  c                     C source files
  cc, c++, cpp          C++ source files
  css                   CSS stylesheets
  go, golang            Go source files
  gn                    GN build files
  html                  HTML files
  java                  Java source files
  javascript, js        JavaScript files
  json                  JSON files
  kotlin                Kotlin source files
  markdown, md          Markdown files
  mojom                 Mojo interface definitions
  objective-c, objc     Objective-C source files
  proto, protobuf       Protocol Buffer definitions
  python, py            Python source files
  rust, rs              Rust source files
  shell, sh, bash       Shell scripts
  sql                   SQL files
  swift                 Swift source files
  textproto, textpb     Text-format Protocol Buffers
  typescript, ts        TypeScript files
  xml                   XML files
  yaml, yml             YAML files

OPERATORS
  AND                   Both terms must match (default for space-separated terms)
  OR                    Either term may match

ADDITIONAL SYNTAX
  "exact phrase"        Match an exact phrase
  -term                 Exclude results matching term
  -filter:value         Negate a filter (e.g., -file:test -lang:java)
  (a OR b) c            Group expressions with parentheses
  \\special              Escape special characters

EXAMPLES
  base::span                              Simple search
  lang:cpp case:yes base::span            Case-sensitive C++ search
  file:*_test.cc base::span               Search only in test files
  lang:cpp -file:test base::span          C++ files, exclude tests
  class:TabStrip                          Find class definitions
  function:AddTabAt lang:cpp              Find function definitions in C++
  "base::span<uint8_t>"                   Exact phrase search
  symbol:kMaxTabs                         Find symbol definitions
  usage:TabStrip                          Find usages of a symbol
  (AddTab OR RemoveTab) lang:cpp          Grouped OR query
"""


# ---------------------------------------------------------------------------
# Search command
# ---------------------------------------------------------------------------


def run_search(
    query: str,
    *,
    limit: int = 30,
    context_lines: int = 0,
    json_fields: str | None = None,
    no_color: bool = False,
) -> None:
    if no_color:
        set_no_color(True)

    api_context = max(context_lines, 1)
    spinner = Spinner("Searching...").start()
    results: list[SearchResult] = []

    try:
        for page in search_all(query, limit=limit, context_lines=api_context):
            results.append(page)
    finally:
        spinner.stop()

    has_results = any(r.files for r in results)
    if not has_results:
        if json_fields is not None:
            sys.stdout.write("[]\n")
        sys.stderr.write("No results found\n")
        return

    if json_fields is not None:
        fields = [f.strip() for f in json_fields.split(",") if f.strip()]
        default_fields: list[JsonField] = ["path", "line", "snippet", "url"]
        sys.stdout.write(
            format_json(results, fields if fields else default_fields, limit) + "\n"
        )
    elif _is_tty():
        summary = summary_line(results, limit)
        if summary:
            sys.stderr.write(summary + "\n\n")
        sys.stdout.write(format_grouped(results, context_lines > 0, limit) + "\n")
    else:
        sys.stdout.write(format_flat(results, context_lines > 0, limit) + "\n")


# ---------------------------------------------------------------------------
# Find command
# ---------------------------------------------------------------------------


def _write_json(data: Any) -> None:
    sys.stdout.write(json.dumps(data, indent=2) + "\n")


def run_find(
    query: str, *, limit: int = 10, as_json: bool = False, no_color: bool = False
) -> None:
    if no_color:
        set_no_color(True)
    key = os.environ.get("CR_SEARCH_API_KEY", DEFAULT_API_KEY)
    spinner = Spinner("Finding...").start()
    try:
        payload = build_suggest_payload(query, max_suggestions=limit)
        paths = fetch_suggest(key, payload, timeout=15.0)
    finally:
        spinner.stop()
    if as_json:
        _write_json([{"path": p, "url": file_url(p)} for p in paths])
    for p in paths if not as_json else []:
        sys.stdout.write(p + "\n")
    if not paths:
        sys.stderr.write("No results found\n")


# ---------------------------------------------------------------------------
# Cat command
# ---------------------------------------------------------------------------


def run_cat(
    path: str,
    *,
    ref: str = "refs/heads/main",
    number_lines: bool = False,
    as_json: bool = False,
) -> None:
    spinner = Spinner("Fetching...").start()
    try:
        lines = fetch_file_contents(path, ref=ref)
    finally:
        spinner.stop()
    if as_json:
        _write_json({"path": path, "ref": ref, "content": "\n".join(lines)})
        return
    for i, line in enumerate(lines, 1):
        if number_lines:
            sys.stdout.write(f"{i:6}\t{line}\n")
        else:
            sys.stdout.write(line + "\n")


# ---------------------------------------------------------------------------
# ls / log / repos commands
# ---------------------------------------------------------------------------


def run_ls(path: str, *, ref: str = "refs/heads/main", as_json: bool = False) -> None:
    spinner = Spinner("Listing...").start()
    try:
        entries = fetch_directory(path, ref=ref)
    finally:
        spinner.stop()
    entries.sort(key=lambda e: (e[1] != "dir", e[0]))
    if as_json:
        _write_json([{"name": n, "type": t} for n, t in entries])
        return
    for name, kind in entries:
        if kind == "dir":
            sys.stdout.write(style(name + "/", "bold", "cyan") + "\n")
        elif kind in ("submodule", "symlink"):
            sys.stdout.write(f"{name}  {style('(' + kind + ')', 'dim')}\n")
        else:
            sys.stdout.write(name + "\n")


def run_log(
    path: str, *, ref: str = "refs/heads/main", limit: int = 10, as_json: bool = False
) -> None:
    spinner = Spinner("Fetching history...").start()
    try:
        entries = fetch_history(path, ref, limit)
    finally:
        spinner.stop()
    rows = [
        {
            "commit": e.get("commitId", ""),
            "time": e.get("commitTime", ""),
            "author": e.get("author", {}).get("name", ""),
            "subject": e.get("commitMessage", "").split("\n", 1)[0],
            "position": e.get("metadata", {}).get("Cr-Commit-Position", ""),
            "review": e.get("metadata", {}).get("Reviewed-on", ""),
        }
        for e in entries[:limit]
    ]
    if as_json:
        _write_json(rows)
        return
    if not rows:
        sys.stderr.write("No history found\n")
        return
    for r in rows:
        date = r["time"][:10]
        sys.stdout.write(
            f"{style(r['commit'][:12], 'yellow')} {date} "
            f"{style(r['author'], 'dim')}  {r['subject']}\n"
        )


def run_repos(*, as_json: bool = False) -> None:
    repos = fetch_repositories()
    rows = [
        {
            "name": r.get("name", ""),
            "semantic": bool(r.get("hasSemanticIndex")),
            "lastCommit": r.get("lastCommitTime", "")[:19],
        }
        for r in repos
    ]
    if as_json:
        _write_json(rows)
        return
    for r in rows:
        flag = " (xrefs)" if r["semantic"] else ""
        sys.stdout.write(f"{r['name']}{style(flag, 'dim')}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


USAGE = """\
Usage: cross-engine chromium [command] <query> [flags]

Commands:
  search <query>   Search Chromium source code
  find <name>      Find files by name
  cat <path>       Print file contents
  ls [path]        List a directory
  log <path>       Commit history for a file or directory
  repos            List repositories in the project
  syntax           Print query syntax reference
  <query>          Implicit search (no subcommand needed)

Flags (search):
  -L, --limit <n>         Max results (default: 30; find: 10; log: 10)
  -C, --context <n>       Context lines around matches (default: 0)
      --json [fields]     JSON output (fields: path,line,snippet,context,url)
                          find/cat/ls/log/repos: --json takes no fields

Flags (cat, ls, log):
      --ref <ref>         Git ref (default: refs/heads/main). search and find
                          always use the current main index.
  -n                      Number output lines (cat)

Flags (scope):
      --project <name>    Code search project (default: chromium; env CR_SEARCH_PROJECT)
      --repo <name>       Repository (default: chromium/src; env CR_SEARCH_REPO)
      --path <prefix>     Limit search/find to a directory prefix

Flags (all):
      --no-color          Disable colored output
  -h, --help              Show this help

Examples:
  cross-engine chromium base::span
  cross-engine chromium 'lang:cpp class:WebContents'
  cross-engine chromium 'usage:TabStripModel -file:out/' -C 3
  cross-engine chromium 'file:*_test.cc base::span' -L 10
  cross-engine chromium 'function:CreateForTesting lang:cpp' --json
  cross-engine chromium ScrollEnd --path third_party/blink/renderer/core
  cross-engine chromium gclient_eval --repo chromium/tools/depot_tools
  cross-engine chromium find web_contents.h -L 20
  cross-engine chromium cat content/public/browser/web_contents.h
  cross-engine chromium cat chrome/browser/BUILD.gn --ref refs/tags/144.0.7559.98
  cross-engine chromium ls third_party/crossbench
  cross-engine chromium log base/containers/span.h -L 5
  cross-engine chromium repos
  cross-engine chromium syntax
"""


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="cross-engine chromium",
        description="Search Chromium source code",
        add_help=False,
    )
    parser.add_argument("args", nargs="*", help=argparse.SUPPRESS)
    parser.add_argument("-L", "--limit", type=int, default=None)
    parser.add_argument("-C", "--context", type=int, default=0, metavar="N")
    parser.add_argument("--json", nargs="?", const="", default=None, dest="json_fields")
    parser.add_argument("--ref", default=None)
    parser.add_argument("-n", "--number", action="store_true")
    parser.add_argument("--project", default=PROJECT)
    parser.add_argument("--repo", default=REPO)
    parser.add_argument("--path", default="")
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument("-h", "--help", action="store_true")
    opts = parser.parse_args(argv)

    if opts.help:
        sys.stderr.write(USAGE)
        return

    if opts.no_color:
        set_no_color(True)
    set_scope(opts.project, opts.repo, opts.path)
    as_json = opts.json_fields is not None
    ref = opts.ref or "refs/heads/main"

    positionals: list[str] = opts.args
    subcommand = positionals[0] if positionals else ""
    rest = " ".join(positionals[1:])

    if subcommand == "syntax":
        sys.stdout.write(SYNTAX_REFERENCE)
        return

    if subcommand == "repos":
        run_repos(as_json=as_json)
        return

    if subcommand == "ls":
        run_ls(rest, ref=ref, as_json=as_json)
        return

    if subcommand == "log":
        if not rest:
            sys.stderr.write(
                "Usage: cross-engine chromium log <path> [--ref <ref>] [-L n]\n"
            )
            sys.exit(1)
        run_log(rest, ref=ref, limit=opts.limit or 10, as_json=as_json)
        return

    if subcommand == "cat":
        if not rest:
            sys.stderr.write(
                "Usage: cross-engine chromium cat <path> [--ref <ref>] [-n]\n"
            )
            sys.exit(1)
        run_cat(rest, ref=ref, number_lines=opts.number, as_json=as_json)
        return

    if opts.ref:
        sys.stderr.write(
            "note: --ref only applies to cat/ls/log; "
            "search and find use the current main index\n"
        )

    if subcommand == "find":
        if not rest:
            sys.stderr.write("Usage: cross-engine chromium find <name> [-L n]\n")
            sys.exit(1)
        run_find(rest, limit=opts.limit or 10, as_json=as_json)
        return

    query = rest if subcommand == "search" else " ".join(positionals)
    if not query:
        sys.stderr.write(USAGE)
        sys.exit(1)

    run_search(
        query,
        limit=opts.limit or 30,
        context_lines=opts.context,
        json_fields=opts.json_fields,
    )

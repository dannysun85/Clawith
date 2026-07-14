#!/usr/bin/env python3
"""Safely switch the production upstream and audit privacy-safe Nginx logs."""

from __future__ import annotations

import os
import re
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


REDACTED_ACCESS_LOG = "access_log /var/log/nginx/access.log astra_no_args;"
REDACTED_LOG_FORMAT = (
    "log_format astra_no_args 'time=$time_iso8601 request_id=$request_id "
    "status=$status bytes=$body_bytes_sent request_time=$request_time "
    "upstream_status=$upstream_status upstream_time=$upstream_response_time';\n"
)
SAFE_LOG_VARIABLES = {
    "$body_bytes_sent",
    "$request_id",
    "$request_time",
    "$status",
    "$time_iso8601",
    "$upstream_response_time",
    "$upstream_status",
}
SAFE_ACCESS_LOG_ARGUMENTS = ("/var/log/nginx/access.log", "astra_no_args")
MAP_ENTRY_NAME = "__map_entry__"


@dataclass(frozen=True)
class _Token:
    value: str
    start: int
    end: int


@dataclass(frozen=True)
class _Block:
    identifier: int
    name: str
    opener: _Token
    parents: tuple[int, ...]


@dataclass(frozen=True)
class _Directive:
    name: str
    arguments: tuple[_Token, ...]
    start: int
    end: int
    blocks: tuple[int, ...]


@dataclass(frozen=True)
class _ParsedConfig:
    blocks: tuple[_Block, ...]
    directives: tuple[_Directive, ...]


def _directive_name(token: _Token) -> str:
    """Return a plain directive name or fail closed on Nginx escaping.

    Nginx removes quotes and backslash escapes while parsing configuration
    tokens. Keeping those raw characters here would let names such as
    ``"access_log"`` or an escaped ``include`` evade the security audit even though
    Nginx executes them as ordinary directives.
    """

    if any(character in token.value for character in ("'", '"', "\\")):
        raise ValueError("quoted or escaped Nginx directive names are not allowed")
    return token.value


def _statement_name(token: _Token, stack: list[_Block]) -> str:
    """Return a directive name while recognizing data rows inside `map` blocks.

    A `map` body is a table rather than an ordinary directive-only block, so a
    valid source string such as ``''`` can be the first token of a terminated
    statement. Quoted or escaped names remain forbidden everywhere else. An
    `include`-shaped map token is also rejected because it could expand input
    that this single-file cutover cannot audit safely.
    """

    if stack and stack[-1].name == "map":
        map_context = tuple(block.name for block in stack[:-1])
        if map_context not in {(), ("http",), ("stream",)}:
            raise ValueError("Nginx map entry has an invalid context")
        # Every terminated statement directly inside a map body is table data,
        # even when its first token happens to be the name of a real directive
        # such as access_log or proxy_pass. Treating only quoted keys as data
        # lets those ordinary keys escape the map boundary during rewrite/audit.
        normalized = re.sub(r"['\"\\\\]", "", token.value).casefold()
        if normalized == "include":
            raise ValueError("Nginx map include is not allowed")
        return MAP_ENTRY_NAME
    return _directive_name(token)


def _tokenize_nginx(text: str) -> list[_Token]:
    """Tokenize active Nginx syntax while preserving source offsets."""

    tokens: list[_Token] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char.isspace():
            index += 1
            continue
        if char == "#":
            newline = text.find("\n", index)
            index = len(text) if newline == -1 else newline + 1
            continue
        if char in "{};":
            tokens.append(_Token(char, index, index + 1))
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            start = index
            index += 1
            escaped = False
            while index < len(text):
                current = text[index]
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == quote:
                    index += 1
                    break
                index += 1
            else:
                raise ValueError("unterminated quoted Nginx token")
            tokens.append(_Token(text[start:index], start, index))
            continue

        start = index
        escaped = False
        while index < len(text):
            current = text[index]
            if escaped:
                escaped = False
                index += 1
                continue
            if current == "\\":
                escaped = True
                index += 1
                continue
            if current.isspace() or current in "{};#":
                break
            index += 1
        if start == index:
            raise ValueError(f"unsupported Nginx syntax at offset {index}")
        tokens.append(_Token(text[start:index], start, index))
    return tokens


def _parse_nginx(text: str) -> _ParsedConfig:
    """Parse enough Nginx structure to reason about directives and block scope."""

    blocks: list[_Block] = []
    directives: list[_Directive] = []
    stack: list[_Block] = []
    directive_tokens: list[_Token] = []
    next_block_identifier = 1

    for token in _tokenize_nginx(text):
        if token.value == ";":
            if not directive_tokens:
                raise ValueError("empty Nginx directive")
            directives.append(
                _Directive(
                    name=_statement_name(directive_tokens[0], stack),
                    arguments=tuple(directive_tokens[1:]),
                    start=directive_tokens[0].start,
                    end=token.end,
                    blocks=tuple(block.identifier for block in stack),
                )
            )
            directive_tokens = []
            continue
        if token.value == "{":
            if not directive_tokens:
                raise ValueError("Nginx block has no directive name")
            block_name = _directive_name(directive_tokens[0])
            if stack and stack[-1].name == "map":
                raise ValueError("nested Nginx blocks are not allowed inside map")
            if block_name == "map":
                map_context = tuple(block.name for block in stack)
                if map_context not in {(), ("http",), ("stream",)}:
                    raise ValueError("Nginx map entry has an invalid context")
            block = _Block(
                identifier=next_block_identifier,
                name=block_name,
                opener=token,
                parents=tuple(parent.identifier for parent in stack),
            )
            next_block_identifier += 1
            blocks.append(block)
            stack.append(block)
            directive_tokens = []
            continue
        if token.value == "}":
            if directive_tokens:
                raise ValueError("unterminated Nginx directive before closing brace")
            if not stack:
                raise ValueError("unmatched Nginx closing brace")
            stack.pop()
            continue
        directive_tokens.append(token)

    if stack:
        raise ValueError("unterminated Nginx block")
    if directive_tokens:
        # `nginx -T 2>&1` may append a diagnostic sentence without a semicolon.
        # It is safe to ignore only when no structural token is present.
        if any(token.value in {"server", "access_log", "include"} for token in directive_tokens):
            raise ValueError("unterminated security-relevant Nginx directive")
    return _ParsedConfig(tuple(blocks), tuple(directives))


def _server_blocks(parsed: _ParsedConfig) -> list[_Block]:
    return [block for block in parsed.blocks if block.name == "server"]


def _directive_in_server(
    directive: _Directive,
    server: _Block,
    *,
    direct_only: bool = False,
) -> bool:
    if server.identifier not in directive.blocks:
        return False
    return not direct_only or directive.blocks[-1] == server.identifier


def _is_safe_access_log(directive: _Directive) -> bool:
    return tuple(token.value for token in directive.arguments) == SAFE_ACCESS_LOG_ARGUMENTS


def _log_variables(raw_format: str) -> set[str]:
    # Nginx accepts both $name and ${name}. The deployed format deliberately
    # uses only the first spelling; rejecting braces prevents an unsafe
    # variable from hiding beside an otherwise complete safe-variable set.
    if "${" in raw_format:
        raise ValueError("braced Nginx log variables are not allowed")
    return set(re.findall(r"\$[a-zA-Z0-9_]+", raw_format))


def _audit_parsed_config(text: str, parsed: _ParsedConfig) -> tuple[int, int]:
    if any(directive.name == "include" for directive in parsed.directives):
        raise ValueError("Nginx include cannot be audited safely in the target site")

    servers = _server_blocks(parsed)
    if not servers:
        raise ValueError("no active Nginx server blocks found")

    safe_logs = 0
    for server in servers:
        scoped_directives = [
            directive
            for directive in parsed.directives
            if _directive_in_server(directive, server)
        ]
        access_logs = [
            directive
            for directive in scoped_directives
            if directive.name == "access_log"
        ]
        if any(not _is_safe_access_log(directive) for directive in access_logs):
            raise ValueError("unsafe Nginx access_log remains in a server block")
        direct_safe_logs = [
            directive
            for directive in access_logs
            if _directive_in_server(directive, server, direct_only=True)
        ]
        if len(direct_safe_logs) != 1:
            raise ValueError(
                "every Nginx server block must define exactly one direct safe access_log"
            )
        safe_logs += 1

    formats = [
        directive
        for directive in parsed.directives
        if directive.name == "log_format"
        and directive.arguments
        and directive.arguments[0].value == "astra_no_args"
    ]
    if formats:
        if len(formats) != 1:
            raise ValueError("expected exactly one astra_no_args log_format")
        raw_format = text[formats[0].start : formats[0].end]
        variables = _log_variables(raw_format)
        if variables != SAFE_LOG_VARIABLES:
            raise ValueError("astra_no_args log_format contains unsafe variables")
    return len(servers), safe_logs


def _extract_effective_source(text: str, source_path: str) -> str:
    marker = f"# configuration file {source_path}:"
    matches = [match.start() for match in re.finditer(rf"^{re.escape(marker)}$", text, re.MULTILINE)]
    if len(matches) != 1:
        raise ValueError(
            f"expected exactly one expanded Nginx source marker; found {len(matches)}"
        )
    content_start = text.find("\n", matches[0])
    if content_start == -1:
        raise ValueError("expanded Nginx source marker has no content")
    next_marker = re.search(r"^# configuration file [^:]+:$", text[content_start + 1 :], re.MULTILINE)
    content_end = (
        len(text)
        if next_marker is None
        else content_start + 1 + next_marker.start()
    )
    return text[content_start + 1 : content_end]


def audit_effective_config(
    text: str,
    source_path: str | None = None,
) -> tuple[int, int]:
    """Audit an expanded `nginx -T` snapshot without returning its contents."""

    parsed = _parse_nginx(text)
    formats = [
        directive
        for directive in parsed.directives
        if directive.name == "log_format"
        and directive.arguments
        and directive.arguments[0].value == "astra_no_args"
    ]
    if len(formats) != 1:
        raise ValueError("effective Nginx config must define astra_no_args exactly once")
    raw_format = text[formats[0].start : formats[0].end]
    variables = _log_variables(raw_format)
    if variables != SAFE_LOG_VARIABLES:
        raise ValueError("astra_no_args log_format contains unsafe variables")

    if source_path is None:
        return _audit_parsed_config(text, parsed)
    source_text = _extract_effective_source(text, source_path)
    return _audit_parsed_config(source_text, _parse_nginx(source_text))


def active_upstream_port(text: str) -> str:
    """Return the single active Astra blue/green upstream port."""

    parsed = _parse_nginx(text)
    ports: list[str] = []
    for directive in parsed.directives:
        if directive.name != "proxy_pass" or len(directive.arguments) != 1:
            continue
        match = re.fullmatch(
            r"http://127\.0\.0\.1:(3008|3009)",
            directive.arguments[0].value,
        )
        if match:
            ports.append(match.group(1))
    if len(ports) != 1:
        raise ValueError(
            "expected exactly one active Astra blue/green upstream; "
            f"found {len(ports)}"
        )
    return ports[0]


def _insertion_point(text: str, server: _Block) -> tuple[int, str]:
    line_start = text.rfind("\n", 0, server.opener.start) + 1
    prefix = text[line_start : server.opener.start]
    indent_match = re.match(r"[ \t]*", prefix)
    indent = (indent_match.group(0) if indent_match else "") + "    "
    line_end = text.find("\n", server.opener.end)
    if line_end != -1:
        remainder = text[server.opener.end : line_end]
        if not remainder.strip() or remainder.lstrip().startswith("#"):
            return line_end + 1, f"{indent}{REDACTED_ACCESS_LOG}\n"
    return server.opener.end, f"\n{indent}{REDACTED_ACCESS_LOG}\n"


def _directive_removal_span(text: str, directive: _Directive) -> tuple[int, int]:
    """Remove a standalone directive line without accumulating blank indentation."""

    line_start = text.rfind("\n", 0, directive.start) + 1
    newline = text.find("\n", directive.end)
    line_end = len(text) if newline == -1 else newline + 1
    before = text[line_start : directive.start]
    after = text[directive.end : newline if newline != -1 else len(text)]
    if not before.strip() and not after.strip():
        return line_start, line_end
    return directive.start, directive.end


def configure_site(text: str, old_port: str, candidate_port: str) -> tuple[str, int]:
    """Return a cutover config whose every server block has privacy-safe logging."""

    if old_port == candidate_port:
        raise ValueError("old and candidate Nginx ports must differ")
    if {old_port, candidate_port} != {"3008", "3009"}:
        raise ValueError("production Nginx ports must be 3008 and 3009")

    parsed = _parse_nginx(text)
    if any(directive.name == "include" for directive in parsed.directives):
        raise ValueError("Nginx include cannot be rewritten safely in the target site")
    old_value = f"http://127.0.0.1:{old_port}"
    candidate_value = f"http://127.0.0.1:{candidate_port}"
    old_matches = [
        directive
        for directive in parsed.directives
        if directive.name == "proxy_pass"
        and len(directive.arguments) == 1
        and directive.arguments[0].value == old_value
    ]
    candidate_matches = [
        directive
        for directive in parsed.directives
        if directive.name == "proxy_pass"
        and len(directive.arguments) == 1
        and directive.arguments[0].value == candidate_value
    ]
    if len(old_matches) == 1 and not candidate_matches:
        argument = old_matches[0].arguments[0]
        text = text[: argument.start] + candidate_value + text[argument.end :]
    elif not old_matches and len(candidate_matches) == 1:
        pass
    else:
        raise ValueError(
            "expected exactly one old or already-installed candidate upstream; "
            f"found old={len(old_matches)} candidate={len(candidate_matches)}"
        )

    parsed = _parse_nginx(text)
    access_logs = [
        directive for directive in parsed.directives if directive.name == "access_log"
    ]
    removal_spans = [_directive_removal_span(text, directive) for directive in access_logs]
    for start, end in sorted(removal_spans, reverse=True):
        text = text[:start] + text[end:]

    parsed = _parse_nginx(text)
    servers = _server_blocks(parsed)
    if not servers:
        raise ValueError("no active Nginx server blocks found")
    insertions = [_insertion_point(text, server) for server in servers]
    for offset, content in sorted(insertions, reverse=True):
        text = text[:offset] + content + text[offset:]

    final_parsed = _parse_nginx(text)
    server_count, safe_log_count = _audit_parsed_config(text, final_parsed)
    if safe_log_count != server_count:
        raise ValueError("not every Nginx server block received a safe access log")
    return text, server_count


def _atomic_write(path: Path, content: str) -> None:
    """Atomically replace a config target while preserving symlink and metadata."""

    target = path.resolve() if path.is_symlink() else path
    metadata = target.stat() if target.exists() else None
    mode = stat.S_IMODE(metadata.st_mode) if metadata else 0o644
    fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, mode)
        if metadata:
            temporary_metadata = temporary_path.stat()
            if (temporary_metadata.st_uid, temporary_metadata.st_gid) != (
                metadata.st_uid,
                metadata.st_gid,
            ):
                os.chown(temporary_path, metadata.st_uid, metadata.st_gid)
        os.replace(temporary_path, target)
        directory_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary_path.unlink(missing_ok=True)


def install_configuration(
    site_path: Path,
    old_port: str,
    candidate_port: str,
    log_format_path: Path,
) -> int:
    configured, server_count = configure_site(
        site_path.read_text(encoding="utf-8"),
        old_port,
        candidate_port,
    )
    # Persist the safe log format first. A process or host crash between these
    # two durable replaces can therefore leave either the old site with the new
    # safe format, or the fully updated pair, but never a new site that depends
    # on an old unsafe format.
    _atomic_write(log_format_path, REDACTED_LOG_FORMAT)
    _atomic_write(site_path, configured)
    return server_count


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: configure_production_nginx.py COMMAND ...")

    command = sys.argv[1]
    if command == "install" and len(sys.argv) == 6:
        count = install_configuration(
            Path(sys.argv[2]),
            sys.argv[3],
            sys.argv[4],
            Path(sys.argv[5]),
        )
        print(f"configured_privacy_safe_server_blocks={count}")
        return
    if command == "active-port" and len(sys.argv) == 3:
        print(active_upstream_port(Path(sys.argv[2]).read_text(encoding="utf-8")))
        return
    if command == "audit-effective" and len(sys.argv) in {3, 4}:
        text = sys.stdin.read() if sys.argv[2] == "-" else Path(sys.argv[2]).read_text(encoding="utf-8")
        source_path = sys.argv[3] if len(sys.argv) == 4 else None
        server_count, safe_log_count = audit_effective_config(text, source_path)
        print(f"audited_server_blocks={server_count} safe_access_logs={safe_log_count}")
        return
    raise SystemExit(
        "usage: configure_production_nginx.py "
        "install SITE OLD_PORT CANDIDATE_PORT LOG_FORMAT | "
        "active-port SITE | audit-effective PATH_OR_DASH [SOURCE_PATH]"
    )


if __name__ == "__main__":
    main()

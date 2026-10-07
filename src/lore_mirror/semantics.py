"""Bounded, local-only mail and patch facts, never an approval/merge oracle.

Raw bytes remain the caller's source of truth. MIME decoding is a derived view;
``variant`` hashes the *entire* input, including headers and line endings.
The only subprocess is a fixed, non-network Git ``patch-id --stable`` command.
No code is applied, repositories cloned, or mail sent by this module.
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from collections import defaultdict
from functools import lru_cache
from datetime import timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any

try:
    from liblore import __version__ as _liblore_version
    from liblore.series import Trailer as _LoreTrailer
    from liblore.series import parse_patch_subject as _lore_subject
except ImportError:
    _liblore_version = None
    _LoreTrailer = _lore_subject = None

MAX_RAW_BYTES = 32 * 1024 * 1024
MAX_PARTS = 4096
MAX_SERIES_MESSAGES = 10000
MAX_SERIES_PATCHES = 10000
MAX_SUBJECT_CHARS = 16384
MAX_TRAILERS = 4096
_ID = re.compile(r"^[^\s<>\x00-\x1f\x7f]+$")
_REF = re.compile(r"<([^<>\s]+)>")
_TRAILER = re.compile(
    r"^(?P<quote>[ \t>]*)(?P<name>Signed-off-by|Reviewed-by|Acked-by|"
    r"Tested-by|Reported-by|Suggested-by|Co-developed-by|Reported-and-tested-by|"
    r"Fixes|Link|Closes|Cc|Change-Id|Base-commit):\s*(?P<value>.*)$", re.I
)


def normalize_message_id(value: str) -> str:
    """Remove one matching angle-bracket pair; retain case and reject ambiguity."""
    if not isinstance(value, str):
        raise ValueError("Message-ID must be a string")
    value = value.strip()
    if value.startswith("<") and value.endswith(">"):
        value = value[1:-1]
    if not value or len(value) > 998 or not _ID.fullmatch(value):
        raise ValueError("Expected one nonempty Message-ID, not a query or ID list")
    return value


def _ids(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        found = _REF.findall(value)
        if not found:
            try:
                found = [normalize_message_id(value)]
            except ValueError:
                continue
        for item in found:
            try:
                item = normalize_message_id(item)
            except ValueError:
                continue
            if item not in seen:
                if len(result) >= MAX_PARTS:
                    raise ValueError("Message exceeds 4096 referenced IDs")
                result.append(item)
                seen.add(item)
    return result



def message_header_ids(values: list[str]) -> list[str]:
    """One exact identity per Message-ID field; RFC comments are not IDs.

    References/In-Reply-To deliberately keep their separate multi-ID parser.
    Malformed fields containing multiple angle-bracket IDs fail closed here.
    """
    result=[]
    for value in values:
        text=[];depth=0;quoted=False;escaped=False
        for char in value:
            if escaped:
                if not depth:text.append(char)
                escaped=False;continue
            if char=="\\" and (depth or quoted):
                if not depth:text.append(char)
                escaped=True;continue
            if depth:
                if char=="(":depth+=1
                elif char==")":depth-=1
                continue
            if char=='"':quoted=not quoted;text.append(char)
            elif char=="(" and not quoted:depth=1
            else:text.append(char)
        if depth or quoted or escaped:continue
        try:identity=normalize_message_id("".join(text))
        except ValueError:continue
        if identity not in result:
            if len(result)>=MAX_PARTS:raise ValueError("Message exceeds 4096 Message-ID fields")
            result.append(identity)
    return result


def _date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        date = parsedate_to_datetime(value)
        # A missing zone is unknown, not an invitation to assume local time.
        if date.tzinfo is None:
            return None
        return date.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError):
        return None


def parse_subject(subject: str) -> dict[str, Any]:
    """Extract conservative subject facts; normalized title is a search hint only."""
    if len(subject) > MAX_SUBJECT_CHARS:
        raise ValueError("Subject exceeds 16384 characters")
    text = subject.strip()
    reply = False
    while re.match(r"^(?:re|aw|sv):\s*", text, re.I):
        text = re.sub(r"^(?:re|aw|sv):\s*", "", text, count=1, flags=re.I)
        reply = True
    tags: list[str] = []
    while text.startswith("["):
        match = re.match(r"\[([^\]]{0,512})\]\s*", text)
        if not match:
            break
        tags.append(match.group(1))
        text = text[match.end():]
    tokens = " ".join(tags)
    is_patch = bool(re.search(r"\bPATCH\b", tokens, re.I))
    version_match = re.search(r"\bv(\d+)\b", tokens, re.I)
    counter = re.search(r"\b(\d+)\s*/\s*(\d+)\b", tokens)
    version = int(version_match.group(1)) if version_match else 1
    ordinal = total = None
    warnings: list[str] = []
    if counter:
        ordinal, total = map(int, counter.groups())
        if total < 1 or total > MAX_SERIES_PATCHES or ordinal > total:
            warnings.append("invalid_or_excessive_patch_counter")
            ordinal = total = None
    if version < 1 or version > MAX_SERIES_PATCHES:
        warnings.append("invalid_or_excessive_version")
        version = 1
    result = {
        "title": text.strip(), "normalized_title": " ".join(text.casefold().split()),
        "tags": tags, "is_patch": is_patch, "is_reply": reply,
        "version": version, "version_explicit": version_match is not None,
        "ordinal": ordinal, "total": total, "is_cover": is_patch and ordinal == 0,
        "rfc": bool(re.search(r"\bRFC\b", tokens, re.I)),
        "resend": bool(re.search(r"\bRESEND\b", tokens, re.I)),
        "warnings": warnings,
    }
    if _lore_subject is not None:
        try:
            parsed = _lore_subject(subject)
            if (1 <= parsed.version <= MAX_SERIES_PATCHES and 0 <= parsed.counter <= parsed.expected
                    and 1 <= parsed.expected <= MAX_SERIES_PATCHES and not warnings):
                result.update(title=parsed.title, normalized_title=" ".join(parsed.title.casefold().split()),
                              is_patch=parsed.is_patch, is_reply=parsed.is_reply, version=parsed.version,
                              version_explicit=not parsed.version_inferred,
                              ordinal=None if parsed.counters_inferred else parsed.counter,
                              total=None if parsed.counters_inferred else parsed.expected,
                              is_cover=parsed.is_cover, parser="liblore " + str(_liblore_version))
        except (ValueError, TypeError, RecursionError):
            result["warnings"].append("liblore_subject_fallback")
    result.setdefault("parser", "stdlib_conservative_fallback")
    return result


def _decode_git_path(value: str) -> str:
    value = value.strip()
    if not (value.startswith('"') and value.endswith('"')):
        return value
    value = value[1:-1]
    data = bytearray()
    index = 0
    escapes = {"n": 10, "r": 13, "t": 9, "b": 8, "f": 12, "v": 11,
               "a": 7, "\\": 92, '"': 34}
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            index += 1
            octal = re.match(r"[0-7]{1,3}", value[index:])
            if octal:
                data.append(int(octal.group(), 8) & 255)
                index += len(octal.group())
                continue
            escaped = value[index]
            if escaped in escapes:
                data.append(escapes[escaped])
            else:
                data.extend(escaped.encode("utf-8", "replace"))
        else:
            data.extend(char.encode("utf-8", "replace"))
        index += 1
    return data.decode("utf-8", "replace")


def _clean_path(value: str, prefixed: bool = False) -> str | None:
    if len(value) > 16384:
        return None
    path = _decode_git_path(value)
    if path == "/dev/null":
        return None
    if prefixed and (path.startswith("a/") or path.startswith("b/")):
        path = path[2:]
    if not path or path.startswith("/") or ".." in path.split("/") or "\x00" in path:
        return None
    return path


def extract_diff_paths(body: str) -> list[str]:
    """Extract changed paths, including rename ends, ignoring quoted/hunk text.

    Extended rename or old/new headers supersede the sometimes ambiguous
    space-separated ``diff --git`` path hints.
    """
    result: set[str] = set()
    hints: set[str] = set()
    exact: set[str] = set()
    in_hunk = False
    in_diff = False
    had_hunk = False
    remaining: tuple[int, int] | None = None
    for line in body.splitlines():
        pairs: list[tuple[str, bool]] = []
        is_hint = False
        if line.startswith("diff --git "):
            result.update(exact or hints)
            hints, exact = set(), set()
            in_hunk, in_diff, remaining = False, True, None
            had_hunk = False
            is_hint = True
            tail = line[len("diff --git "):]
            if tail.startswith('"'):
                tokens = re.findall(r'"(?:[^"\\]|\\.)*"|\S+', tail)
                if len(tokens) == 2:
                    pairs = [(part, True) for part in tokens]
            else:
                boundaries = [match.start() for match in re.finditer(" b/", tail)]
                equal = [index for index in boundaries if tail[2:index] == tail[index + 3:]]
                if len(equal) == 1 or len(boundaries) == 1:
                    index = equal[0] if len(equal) == 1 else boundaries[0]
                    pairs = [(tail[:index], True), (tail[index + 1:], True)]
        elif line.startswith("@@ "):
            in_hunk = True
            had_hunk = True
            hunk = re.match(r"@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@", line)
            remaining = (int(hunk.group(1) or 1), int(hunk.group(2) or 1)) if hunk else None
        elif in_hunk:
            if remaining is not None and line[:1] in {" ", "-", "+"}:
                old, new = remaining
                old -= line[0] in {" ", "-"}
                new -= line[0] in {" ", "+"}
                remaining = (old, new)
                if old <= 0 and new <= 0:
                    in_hunk = False
        elif not (in_diff and had_hunk) and line.startswith(("--- ", "+++ ")):
            pairs = [(line[4:].split("\t", 1)[0], True)]
        elif in_diff and not had_hunk and line.startswith(("rename from ", "rename to ", "copy from ", "copy to ")):
            pairs = [(line.split(" ", 2)[2], False)]
        for value, prefixed in pairs:
            path = _clean_path(value, prefixed)
            if path is not None:
                (hints if is_hint else exact).add(path)
    result.update(exact or hints)
    return sorted(result)


def _patch_text(body: str) -> str | None:
    match = re.search(r"(?m)^diff --git ", body)
    if match:
        return body[match.start():]
    match = re.search(r"(?m)^--- [^\n]+\n\+\+\+ [^\n]+\n@@ ", body)
    return body[match.start():] if match else None


def stable_patch_id(patch: str | bytes) -> str | None:
    """Return Git's stable patch-id or None; fixed command and bounded input/time.

    A stable patch-id is equivalence evidence, not proof of application. No repo
    or caller-controlled Git options/configuration are used. Ambiguous multi-ID
    output is deliberately rejected. Temporary output files avoid unbounded RAM
    capture, and only 4 KiB of output is read.
    """
    data = patch.encode("utf-8", "replace") if isinstance(patch, str) else patch
    if not isinstance(data, bytes) or len(data) > MAX_RAW_BYTES:
        raise ValueError("Patch must be bytes or text no larger than 32 MiB")
    executable = shutil.which("git")
    if not executable:
        return None
    env = {"PATH": os.path.dirname(executable), "LC_ALL": "C", "HOME": "/nonexistent",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}
    try:
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as error:
            result = subprocess.run(
                [executable, "--no-pager", "--no-replace-objects", "patch-id", "--stable"],
                input=data, stdout=output, stderr=error, cwd="/", env=env,
                timeout=5, check=False,
            )
            output.seek(0)
            raw = output.read(4097)
        if result.returncode or len(raw) > 4096:
            return None
        lines = raw.decode("ascii", "replace").splitlines()
        if len(lines) == 1 and re.fullmatch(r"[0-9a-f]{40,64} [0-9a-f]{40,64}", lines[0]):
            return lines[0].split()[0]
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def _trailers(body: str, message: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    lines = body.splitlines()
    diff_at = next((i for i, line in enumerate(lines) if line.startswith("diff --git ")), len(lines))
    facts = message["subject_info"]
    for index, line in enumerate(lines):
        match = _TRAILER.match(line)
        if not match:
            continue
        if len(result) >= MAX_TRAILERS:
            message["trailers_truncated"] = True
            message["parse_warnings"].append("trailers_exceed_4096_limit")
            break
        quoted = bool(match.group("quote").strip())
        value = match.group("value")
        _, email = parseaddr(value)
        context = "\n".join(lines[max(0, index - 2):min(len(lines), index + 3)])
        retraction = bool(re.search(r"\b(?:retract\w*|withdraw\w*|drop|remove)\b", context, re.I))
        copied = bool(message["patch"] or (email and email.casefold() != message["from_email"].casefold()))
        classification = ("quoted" if quoted else "retraction_context" if retraction else
                          "embedded_claim" if copied else "unverified_claim")
        scope = "cover" if facts["is_cover"] else "reply" if facts["is_reply"] else "patch" if message["patch"] else "message"
        record = {
            "name": match.group("name"), "value": value, "source_message_id": message["message_id"],
            "source_variant": message["variant"], "line": index + 1, "source_line": line,
            "scope": scope, "target_message_ids": message["in_reply_to"] if scope == "reply" else [],
            "quoted": quoted, "copied_or_embedded": copied, "retraction_context": retraction,
            "classification": classification, "in_diff": index >= diff_at,
            "verified_approval": False, "authentication": "not_checked",
        }
        if _LoreTrailer is not None and len(value) <= MAX_SUBJECT_CHARS:
            parsed = _LoreTrailer.parse(match.group("name"), value, msgid=message["message_id"])
            record.update(trailer_type=parsed.type, claimed_email=parsed.addr[1] if parsed.addr else None,
                          parser="liblore " + str(_liblore_version))
        result.append(record)
    return result


def parse_message(raw: bytes, list_ids: list[str] | None = None) -> dict[str, Any]:
    """Parse at most 32 MiB without remote fetch, active content or application.

    ``list_ids`` is physical archive membership supplied by the backend; List-Id
    headers are stored separately and are never treated as corpus membership.
    ``textparts`` retains all decoded text parts, including HTML as inert text.
    ``body`` combines plain/patch text parts only. Raw bytes must be retrieved
    separately when exact octets, MIME boundaries or invalid encodings matter.
    """
    if not isinstance(raw, bytes):
        raise TypeError("raw must be bytes")
    if len(raw) > MAX_RAW_BYTES:
        raise ValueError("Raw message exceeds 32 MiB limit")
    warnings: list[str] = []
    created_parts = 0
    def message_factory(*args: Any, **kwargs: Any) -> EmailMessage:
        nonlocal created_parts
        created_parts += 1
        if created_parts > MAX_PARTS:
            raise ValueError("MIME message exceeds 4096 parts")
        return EmailMessage(*args, **kwargs)
    try:
        mail = BytesParser(policy=policy.default.clone(message_factory=message_factory)).parsebytes(raw)
    except (RecursionError, ValueError) as exc:
        raise ValueError("MIME message cannot be safely parsed") from exc
    headers: dict[str, list[str]] = defaultdict(list)
    raw_headers: list[dict[str, str]] = []
    for key, value in mail.raw_items():
        raw_headers.append({"name": key, "value": value})
    for key, value in mail.items():
        headers[key.lower()].append(str(value))
    message_ids = message_header_ids(headers.get("message-id", []))
    message_id = message_ids[0] if len(message_ids) == 1 else None
    if not message_ids:
        warnings.append("missing_or_invalid_message_id")
    elif len(message_ids) > 1:
        warnings.append("conflicting_message_id_headers")
    parts: list[dict[str, Any]] = []
    body_parts: list[str] = []
    stack = [mail]
    part_count = decoded_bytes = 0
    while stack:
        part = stack.pop()
        part_count += 1
        if part_count > MAX_PARTS:
            raise ValueError("MIME message exceeds 4096 parts")
        warnings.extend(type(defect).__name__ for defect in part.defects)
        # Do not mistake attached .eml content for the enclosing sender's patch.
        if part.get_content_type() == "message/rfc822":
            warnings.append("attached_message_excluded_from_body")
            continue
        if part.is_multipart():
            stack.extend(reversed(part.get_payload()))
            continue
        content_type = part.get_content_type()
        filename = part.get_filename() or ""
        patch_attachment = content_type in {"application/x-patch", "application/x-diff"} or (
            content_type == "application/octet-stream" and filename.lower().endswith((".patch", ".diff")))
        if part.get_content_maintype() != "text" and not patch_attachment:
            continue
        payload = part.get_payload(decode=True)
        warnings.extend(type(defect).__name__ for defect in part.defects)
        if payload is None:
            payload = str(part.get_payload()).encode("utf-8", "replace")
        decoded_bytes += len(payload)
        if decoded_bytes > MAX_RAW_BYTES:
            raise ValueError("Decoded text exceeds 32 MiB limit")
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, "replace")
        except LookupError:
            warnings.append("unknown_charset:" + charset[:80])
            text = payload.decode("utf-8", "replace")
        parts.append({"content_type": content_type, "charset": charset,
                      "filename": part.get_filename(), "disposition": part.get_content_disposition(),
                      "text": text})
        if content_type in {"text/plain", "text/x-patch", "text/x-diff", "text/patch"} or patch_attachment:
            body_parts.append(text)
    body = "\n".join(body_parts)
    subject = headers.get("subject", [""])[0]
    sender = headers.get("from", [""])[0]
    facts = parse_subject(subject)
    patch_text = _patch_text(body)
    received = [value.rsplit(";", 1)[-1].strip() for value in headers.get("received", []) if ";" in value]
    result = {
        "message_id": message_id, "message_ids": message_ids,
        "variant": hashlib.sha256(raw).hexdigest(), "raw_size": len(raw),
        "subject": subject, "subject_info": facts, "from": sender, "from_email": parseaddr(sender)[1],
        "date": _date(headers.get("date", [None])[0]), "date_header": headers.get("date", [None])[0],
        "received_date": next((date for value in received if (date := _date(value))), None),
        "headers": dict(headers), "raw_headers": raw_headers,
        "list_ids": sorted(set(list_ids or [])), "list_id_headers": headers.get("list-id", []),
        "references": _ids(headers.get("references", [])),
        "in_reply_to": _ids(headers.get("in-reply-to", [])),
        "body": body, "textparts": parts, "patch": patch_text is not None,
        "patch_id": stable_patch_id(patch_text) if patch_text else None,
        "paths": extract_diff_paths(patch_text) if patch_text else [],
        "parse_warnings": sorted(set(warnings + facts["warnings"])),
        "trailers_truncated": False,
        "applied_status": "unknown", "application_evidence": [],
    }
    result["trailers"] = _trailers(body, result)
    if result["patch"] and not result["patch_id"]:
        result["parse_warnings"].append("patch_id_unavailable_or_ambiguous")
    return result


def _quote_query(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").replace("\r", " ") + '"'


def series_candidates_query(message: dict[str, Any]) -> str:
    """A lei subject/author candidate query, never a logical-series identity."""
    facts = message.get("subject_info") or parse_subject(message.get("subject", ""))
    title = facts["title"]
    author = message.get("from_email", "")
    if not title or not author:
        raise ValueError("Subject and sender are needed for a bounded series candidate query")
    return "s:" + _quote_query(title) + " AND f:" + _quote_query(author)


def _path_match(path: str, pattern: str) -> bool:
    if not pattern or len(pattern) > 4096 or len(path) > 4096:
        return False
    if pattern.endswith("/"):
        return path.startswith(pattern)
    # F: patterns use path segments; '*' cannot silently cross directories.
    pattern_parts, path_parts = pattern.split("/"), path.split("/")
    positions = {0}
    for segment in pattern_parts:
        if not positions:
            return False
        if segment == "**":
            positions = set(range(min(positions), len(path_parts) + 1))
        else:
            positions = {i + 1 for i in positions if i < len(path_parts) and fnmatch.fnmatchcase(path_parts[i], segment)}
    return len(path_parts) in positions


def match_paths(message: dict[str, Any], paths: list[str]) -> bool:
    """Match extracted diff paths to exact paths, directory prefixes or globs."""
    return any(_path_match(path, pattern) for path in message.get("paths", []) for pattern in paths)


@lru_cache(maxsize=4)
def _maintainer_sections(maintainers_text: str):
    if len(maintainers_text.encode("utf-8")) > MAX_RAW_BYTES:
        raise ValueError("MAINTAINERS text exceeds 32 MiB limit")
    sections = []
    name = ""
    rules: dict[str, list[str]] = defaultdict(list)
    def finish() -> None:
        if name and rules:
            sections.append((name, dict(rules)))
    for line in maintainers_text.splitlines() + [""]:
        match = re.match(r"^([A-Z]):\s*(.*)$", line)
        if match:
            rules[match.group(1)].append(match.group(2).strip())
        elif line.strip():
            if rules:
                finish()
                rules = defaultdict(list)
            name = line.strip()
        elif rules:
            finish()
            rules = defaultdict(list)
            name = ""
    return sections


def classify_subsystems(message: dict[str, Any], maintainers_text: str) -> list[dict[str, Any]]:
    """Match MAINTAINERS F/X rules; N/K regexes are explicitly not evaluated.

    Callers must attach the MAINTAINERS source tree/ref/commit. This function
    cannot infer that provenance from text, and makes no authoritative ownership
    claim. Empty output is not proof that no subsystem owns a changed path.
    """
    sections = _maintainer_sections(maintainers_text)
    result = []
    for name, rules in sections:
        included = [path for path in message.get("paths", []) if any(_path_match(path, p) for p in rules.get("F", []))]
        excluded = [path for path in included if any(_path_match(path, p) for p in rules.get("X", []))]
        matched = sorted(set(included) - set(excluded))
        if matched:
            result.append({"name": name, "matched_paths": matched, "excluded_paths": sorted(excluded),
                           "include_rules": rules.get("F", []), "exclude_rules": rules.get("X", []),
                           "unevaluated_regex_rules": {k: rules[k] for k in ("N", "K") if k in rules},
                           "evidence": "maintainers_F_X_path_rules", "source_provenance": "caller_required"})
    return result


def posting_evidence(message: dict[str, Any], *, message_id: str | None = None,
                     patch_id: str | None = None, subject: str | None = None,
                     author: str | None = None) -> dict[str, Any]:
    """Separate exact/equivalence evidence from weak search coincidences."""
    signals: list[dict[str, Any]] = []
    if message_id is not None and normalize_message_id(message_id) == message.get("message_id"):
        signals.append({"kind": "exact_message_id", "strength": "exact_identifier"})
    if patch_id is not None and patch_id == message.get("patch_id"):
        signals.append({"kind": "git_stable_patch_id", "strength": "patch_equivalence"})
    if subject is not None and parse_subject(subject)["normalized_title"] == parse_subject(message.get("subject", ""))["normalized_title"]:
        signals.append({"kind": "normalized_subject", "strength": "candidate_only"})
    if author is not None and author.casefold() == message.get("from_email", "").casefold():
        signals.append({"kind": "author", "strength": "candidate_only"})
    strong = any(signal["strength"] != "candidate_only" for signal in signals)
    return {"message_id": message.get("message_id"), "variant": message.get("variant"),
            "signals": signals, "was_posted": "local_archive_evidence" if strong else "candidate_only" if signals else "unknown",
            "applied_status": "unknown", "application_evidence": []}


def summarize_series(messages: list[dict[str, Any]], anchor_id: str,
                     all_versions: bool = True) -> dict[str, Any]:
    """Conservative posting groups with explicit cross-version candidate evidence.

    Explicit References/In-Reply-To edges establish thread membership, not
    proof of logical series identity. Detached subject/author and patch-id
    matches remain in candidate_versions. Incomplete input is never silently
    completed with old patches, and no trailer is carried to a newer revision.
    """
    anchor_id = normalize_message_id(anchor_id)
    if len(messages) > MAX_SERIES_MESSAGES:
        raise ValueError("Series input exceeds 10000 messages")
    by_id: dict[str, dict[str, Any]] = {}
    variants: dict[str, list[str]] = defaultdict(list)
    for message in messages:
        mid = message.get("message_id")
        if mid:
            by_id.setdefault(mid, message)
            variant = message.get("variant")
            if variant and variant not in variants[mid]:
                variants[mid].append(variant)
    if anchor_id not in by_id:
        raise ValueError("Unknown anchor Message-ID; no series can be inferred")
    parent: dict[str, str] = {}
    def root(mid: str) -> str:
        parent.setdefault(mid, mid)
        trail = []
        while parent[mid] != mid:
            trail.append(mid)
            mid = parent[mid]
        for previous in trail:
            parent[previous] = mid
        return mid
    def union(left: str, right: str) -> None:
        a, b = root(left), root(right)
        if a != b:
            parent[max(a, b)] = min(a, b)
    for mid, message in by_id.items():
        root(mid)
        for reference in message.get("references", []) + message.get("in_reply_to", []):
            union(mid, reference)
    anchor_component = root(anchor_id)
    facts = {mid: message.get("subject_info") or parse_subject(message.get("subject", "")) for mid, message in by_id.items()}
    submissions = {mid: message for mid, message in by_id.items()
                   if facts[mid]["is_patch"] and not facts[mid]["is_reply"]}
    groups: dict[tuple[str, int, str], list[dict[str, Any]]] = defaultdict(list)
    for mid, message in submissions.items():
        info = facts[mid]
        component = root(mid)
        # Keep different authors separate even when a long discussion interleaves postings.
        groups[(component, info["version"], message.get("from_email", "").casefold())].append(message)
    connected_ids = {mid for mid in by_id if root(mid) == anchor_component}
    seed_ids = set(submissions) & connected_ids
    if not all_versions:
        wanted = facts[anchor_id]["version"]
        seed_ids = {mid for mid in seed_ids if facts[mid]["version"] == wanted}
    links: list[dict[str, Any]] = []
    links_truncated = False
    candidate_keys: set[tuple[str, int, str]] = set()
    if all_versions:
        title_seeds: dict[tuple[str, str], set[str]] = defaultdict(set)
        patch_seeds: dict[str, set[str]] = defaultdict(set)
        for sid in seed_ids:
            seed = submissions[sid]
            title, author = facts[sid]["normalized_title"], seed.get("from_email", "").casefold()
            if title and author:
                title_seeds[(title, author)].add(sid)
            if seed.get("patch_id"):
                patch_seeds[seed["patch_id"]].add(sid)
        for key, members in groups.items():
            if key[0] == anchor_component:
                continue
            for candidate in members:
                cid = candidate["message_id"]
                title_matches = title_seeds.get((facts[cid]["normalized_title"], candidate.get("from_email", "").casefold()), set())
                patch_matches = patch_seeds.get(candidate.get("patch_id"), set())
                if title_matches or patch_matches:
                    candidate_keys.add(key)
                for sid in sorted(title_matches | patch_matches):
                    seed = submissions[sid]
                    same_patch = bool(candidate.get("patch_id") and candidate.get("patch_id") == seed.get("patch_id"))
                    if len(links) < MAX_SERIES_MESSAGES:
                        links.append({"from_message_id": sid, "to_message_id": cid,
                                      "kind": "stable_patch_id" if same_patch else "subject_author",
                                      "heuristic": True, "series_identity": "candidate_only",
                                      "patch_equivalence": same_patch})
                    else:
                        links_truncated = True
                        break
        for mid in sorted(seed_ids):
            for reference in submissions[mid].get("in_reply_to", []) + submissions[mid].get("references", []):
                if reference in seed_ids and facts[mid]["version"] != facts[reference]["version"]:
                    if len(links) < MAX_SERIES_MESSAGES:
                        links.append({"from_message_id": reference, "to_message_id": mid,
                                      "kind": "explicit_thread_reference", "heuristic": False,
                                      "series_identity": "thread_association_only", "patch_equivalence": False})
                    else:
                        links_truncated = True
    def summarize(key: tuple[str, int, str], members: list[dict[str, Any]], candidate: bool) -> dict[str, Any]:
        members = sorted(members, key=lambda m: (facts[m["message_id"]]["ordinal"] if facts[m["message_id"]]["ordinal"] is not None else 1,
                                                m.get("date") or "", m["message_id"]))
        totals = {facts[m["message_id"]]["total"] for m in members} - {None}
        expected = next(iter(totals)) if len(totals) == 1 else (1 if not totals and len(members) == 1 and members[0].get("patch") else None)
        counters: dict[int, list[str]] = defaultdict(list)
        for m in members:
            index = facts[m["message_id"]]["ordinal"]
            if index is None and expected == 1 and m.get("patch"):
                index = 1
            if index is not None and index > 0 and m.get("patch"):
                counters[index].append(m["message_id"])
        missing = [i for i in range(1, expected + 1) if i not in counters] if expected else []
        cover = [m["message_id"] for m in members if facts[m["message_id"]]["is_cover"]]
        declared_partial = any(re.search(r"\bpartial\s+re-?roll\b", m.get("body", ""), re.I) for m in members)
        collisions = {str(index): ids for index, ids in counters.items() if len(ids) > 1}
        partial = "declared" if declared_partial else "possible" if key[1] > 1 and missing else "not_established"
        return {"version": key[1], "author": key[2], "thread_component": key[0],
                "membership_evidence": "heuristic_candidate" if candidate else "explicit_thread_links",
                "series_identity": "candidate_only" if candidate else "thread_association_only",
                "cover_message_ids": cover, "missing_cover": not bool(cover) if expected and expected > 1 else None,
                "expected_patch_count": expected, "observed_patch_count": len(counters),
                "missing_patch_payload_ids": [m["message_id"] for m in members if not facts[m["message_id"]]["is_cover"] and not m.get("patch")],
                "missing_indices": missing, "counter_conflicts": collisions,
                "count_status": "conflicting" if len(totals) > 1 or collisions or len(cover) > 1 else "complete_observed" if expected and not missing else "incomplete_or_unknown",
                "partial_reroll": partial, "synthesized": False,
                "messages": [{"message_id": m["message_id"], "variants": variants[m["message_id"]],
                              "subject": m.get("subject"), "ordinal": facts[m["message_id"]]["ordinal"],
                              "kind": "cover" if facts[m["message_id"]]["is_cover"] else "patch" if m.get("patch") else "patch_candidate",
                              "patch_id": m.get("patch_id"), "actually_posted": True, "synthesized": False,
                              "applied_status": "unknown"} for m in members],
                "applied_status": "unknown"}
    included = []
    candidates = []
    for key, members in sorted(groups.items()):
        if key[0] == anchor_component and (all_versions or key[1] == facts[anchor_id]["version"]):
            included.append(summarize(key, members, False))
        elif key in candidate_keys:
            candidates.append(summarize(key, members, True))
    return {"anchor_message_id": anchor_id, "versions": included, "candidate_versions": candidates,
            "version_links": links, "thread_message_ids": sorted(connected_ids),
            "version_links_truncated": links_truncated,
            "variant_conflicts": {mid: values for mid, values in variants.items() if len(values) > 1},
            "input_message_count": len(messages), "completeness": "provided_messages_only",
            "synthesized": False, "applied_status": "unknown", "application_evidence": [],
            "warnings": ["Subject/author and patch-id matches do not prove logical series identity or application.",
                         "Missing postings may reflect incomplete retrieval or a partial reroll; older patches are not synthesized."]}

"""Conservative, local commit/mail links. No fetch, application, or merge claims.

Stable patch IDs here are computed anew from both sides using the same versioned
normalizer. Never compare them to public-inbox patchid or semantics.patch_id.
"""
from __future__ import annotations

import hashlib
import math
import re
import time
import urllib.parse
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parsedate_to_datetime

from .semantics import message_header_ids, normalize_message_id
from .code_runtime import CodeRunError, bounded_run
from .code_config import DEFAULT_CODE_LINKS, DEFAULT_CODE_LIMITS

ALGORITHM = "git-patch-id-stable/lore-text-diff-v1"
PATCH_ALGORITHM = ALGORITHM
TRAILER_ALGORITHM = "commit-trailer/lore-final-block-v1"
MAX_INPUT = 16 * 1024 * 1024
DEFAULT_MAXIMUM_BYTES = DEFAULT_CODE_LINKS["max_patch_bytes"]
MAX_TRAILERS = 256
_TRAILER = re.compile(r"^([A-Za-z0-9][A-Za-z0-9-]*):[ \t]*(.*)$")
_HUNK = re.compile(r"^@@ -([0-9]{1,10})(?:,([0-9]{1,10}))? \+([0-9]{1,10})(?:,([0-9]{1,10}))? @@(?:.*)$")


def _lore_mid(url):
    if len(url) > 4096 or any(ord(c) < 33 or ord(c) == 127 for c in url):
        return None
    try:
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or parsed.netloc not in {"lore.kernel.org", "patch.msgid.link"}
                or parsed.query or parsed.fragment or re.search(r"%(?![0-9a-fA-F]{2})", parsed.path)):
            return None
        if not parsed.path.startswith("/") or parsed.path.startswith("//") or parsed.path.endswith("//"):
            return None
        parts = parsed.path[1:].removesuffix("/").split("/")
        # patch.msgid.link/<mid> is b4's fixed Message-ID form; nothing to follow.
        if parsed.netloc == "patch.msgid.link":
            parts = ["r", *parts]
        if len(parts) != 2 or not re.fullmatch(r"[A-Za-z0-9_.+-]+", parts[0]):
            return None
        mid = urllib.parse.unquote(parts[1], encoding="utf-8", errors="strict")
        normalized = normalize_message_id(mid)
        return normalized if normalized == mid else None
    except (ValueError, UnicodeError):
        return None


def commit_trailer_evidence(message, known_mids=None):
    """Only unquoted trailers occupying the entire final paragraph count.

    URL extraction does not resolve aliases, follow links, decode twice, or
    convert a Fixes commit reference into mail or patch-equivalence evidence.
    """
    if not isinstance(message, str) or len(message.encode("utf-8")) > MAX_INPUT:
        raise ValueError("commit message exceeds input limit")
    lines = message.rstrip("\r\n").split("\n")
    start = next((i + 1 for i in range(len(lines) - 1, -1, -1) if not lines[i].strip()), 0)
    if not start:
        return []  # A subject that looks like a trailer is not a trailer block.
    parsed = []
    for number, line in enumerate(lines[start:], start + 1):
        match = _TRAILER.fullmatch(line)
        if match:
            if len(parsed) >= MAX_TRAILERS:
                return []
            parsed.append([match.group(1), match.group(2), line, number])
        elif line.startswith((" ", "\t")) and parsed:
            parsed[-1][1] += " " + line.strip()
            parsed[-1][2] += "\n" + line
        elif re.fullmatch(r"\[[^\[\]\n]*\]", line.strip()):
            continue  # Maintainer note such as "[ bp: Massage commit message. ]".
        else:
            return []  # Mixed prose or quoted trailers: no trusted final block.
    result = []
    for name, value, source, number in parsed:
        lowered = name.lower()
        if lowered in {"link", "closes"}:
            url = value.strip()
            if url.startswith("<") and url.endswith(">"):
                url = url[1:-1]
            mid = _lore_mid(url)
            row = {"kind": lowered, "trailer": name, "source": source,
                   "line": number, "value": value, "url": url,
                   "status": ("unverified_local" if mid is not None and known_mids is None else
                              "linked" if mid is not None and mid in known_mids else "candidate")}
            if mid is not None:
                row["message_id"] = mid
                row["known_message"] = None if known_mids is None else mid in known_mids
            else:
                row["warning"] = "not_an_unambiguous_lore_message_url"
            result.append(row)
        elif lowered == "fixes":
            match = re.fullmatch(r"([0-9a-fA-F]{7,40})(?:[ \t]+\([^\r\n]*\))?", value.strip())
            row = {"kind": "fixes_commit", "trailer": name, "source": source,
                   "line": number, "value": value, "status": "candidate"}
            if match:
                row["commit_prefix"] = match.group(1).lower()
            else:
                row["warning"] = "ambiguous_fixes_reference"
            result.append(row)
    return result


def _normalize_patch(data, maximum_bytes):
    """Extract one well-formed text diff; reject rather than guess ambiguity."""
    if not isinstance(data, bytes) or len(data) > maximum_bytes:
        return None, "patch_input_limit"
    try:
        text = data.decode("utf-8", "strict").replace("\r\n", "\n")
    except UnicodeError:
        return None, "non_utf8_patch"
    if "\x00" in text or "\r" in text:
        return None, "binary_or_ambiguous_text"
    lines = text.split("\n")
    starts = [i for i, line in enumerate(lines) if line.startswith("diff --git ")]
    if not starts:
        return None, "no_git_text_diff"
    if len(re.findall(r"(?m)^From [0-9a-f]{40} Mon Sep 17", text)) > 1:
        return None, "multiple_patch_messages"
    if any(line.startswith(("diff ", "--- ", "+++ ", "GIT binary patch", "Binary files ")) for line in lines[:starts[0]]):
        return None, "ambiguous_diff_prefix"
    lines = lines[starts[0]:]
    result = []
    remaining = None
    hunks = 0
    changes = 0
    file_hunks = 0
    for position, line in enumerate(lines):
        if remaining is not None and (remaining[0] or remaining[1]):
            if line == "\\ No newline at end of file":
                result.append(line)
                continue
            if not line or line[0] not in " +-":
                return None, "malformed_hunk"
            changes += line[0] in "+-"
            old, new = remaining
            old -= line[0] in " -"
            new -= line[0] in " +"
            if old < 0 or new < 0:
                return None, "malformed_hunk"
            remaining = (old, new)
            result.append(line)
            continue
        if line.startswith("diff --git "):
            if result and file_hunks == 0:
                return None, "non_text_file_change"
            file_hunks = 0
            remaining = None
            result.append(line)
        elif line.startswith(("GIT binary patch", "Binary files ", "diff --cc ", "diff --combined ")):
            return None, "binary_or_combined_diff"
        elif line.startswith("@@"):
            match = _HUNK.fullmatch(line)
            if not match:
                return None, "malformed_hunk"
            remaining = (int(match.group(2) or 1), int(match.group(4) or 1))
            hunks += 1
            file_hunks += 1
            result.append(line)
        elif line == "\\ No newline at end of file":
            result.append(line)
        elif line.startswith(("index ", "old mode ", "new mode ", "new file mode ", "deleted file mode ",
                              "similarity index ", "dissimilarity index ", "rename from ", "rename to ",
                              "copy from ", "copy to ", "--- ", "+++ ")) and file_hunks == 0:
            result.append(line)
        elif line == "-- " and file_hunks:
            if any(tail.startswith("diff --git ") for tail in lines[position+1:]):
                return None, "multiple_patch_messages"
            break  # Conventional format-patch signature is not patch content.
        elif not line and file_hunks:
            continue
        else:
            return None, "ambiguous_diff_tail"
    if remaining is not None and any(remaining):
        return None, "truncated_hunk"
    if not hunks or not file_hunks or not changes:
        return None, "non_text_file_change"
    return ("\n".join(result) + "\n").encode(), None


def _git(args, data, deadline, lock_fd=None, memory_bytes=None):
    """Fixed official Git path, bounded pipes, local config-free invocation."""
    executable = "/usr/bin/git"
    env = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "LANG": "C", "LC_ALL": "C",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
           "GIT_NO_REPLACE_OBJECTS": "1", "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}
    try:
        result = bounded_run([executable, "--no-pager", "--no-replace-objects", *args],
            env=env, cwd="/", deadline=deadline, max_bytes=4096, stderr_bytes=4096,
            memory_bytes=DEFAULT_CODE_LIMITS["query_memory_bytes"] if memory_bytes is None else memory_bytes,
            input_bytes=data, max_input_bytes=MAX_INPUT,
            pass_fds=() if lock_fd is None else (lock_fd,))
    except OSError:
        return None, "git_unavailable"
    except CodeRunError:
        return None, "patch_input_limit"
    if result.partial_reason:
        return None, {"timeout": "patch_id_timeout", "memory_limit": "patch_id_memory_limit"}.get(
            result.partial_reason, "patch_id_output_limit")
    return (None, "git_patch_id_failed") if result.returncode else (result.output, None)


def _patch_evidence(data, *, timeout=5, maximum_bytes=DEFAULT_MAXIMUM_BYTES, lock_fd=None, memory_bytes=None):
    result = {"patch_id": None, "algorithm": ALGORITHM, "git_version": None,
              "status": "candidate", "warnings": []}
    if not isinstance(timeout, (int, float)) or not 0 < timeout <= 30:
        raise ValueError("timeout must be within (0, 30]")
    if type(maximum_bytes) is not int or not 1 <= maximum_bytes <= MAX_INPUT:
        raise ValueError("maximum_bytes must be within 1..16 MiB")
    started = time.monotonic()
    normalized, warning = _normalize_patch(data, maximum_bytes)
    if warning:
        result["warnings"].append(warning)
        return result
    deadline = started + timeout
    version, warning = _git(["--version"], b"", deadline, lock_fd, memory_bytes)
    if warning:
        result["warnings"].append(warning)
        return result
    version = version.decode("ascii", "strict").strip()
    if not re.fullmatch(r"git version [0-9][A-Za-z0-9.()+ -]{0,100}", version):
        result["warnings"].append("invalid_git_version")
        return result
    result["git_version"] = version
    raw, warning = _git(["patch-id", "--stable"], normalized, deadline, lock_fd, memory_bytes)
    if warning:
        result["warnings"].append(warning)
        return result
    lines = raw.decode("ascii", "replace").splitlines()
    if len(lines) != 1 or not re.fullmatch(r"[0-9a-f]{40} [0-9a-f]{40}", lines[0]):
        result["warnings"].append("ambiguous_patch_id_output")
        return result
    result.update(patch_id=lines[0].split()[0], status="patch_equivalent_candidate",
                  normalized_sha256=hashlib.sha256(normalized).hexdigest())
    return result


def commit_patch_evidence(diff, *, parent_count=1, timeout=5, maximum_bytes=DEFAULT_MAXIMUM_BYTES, lock_fd=None, memory_bytes=None):
    if type(parent_count) is not int or parent_count > 1 or parent_count < 0:
        return {"patch_id": None, "algorithm": ALGORITHM, "git_version": None,
                "status": "candidate", "warnings": ["merge_or_unknown_parentage"]}
    return _patch_evidence(diff, timeout=timeout, maximum_bytes=maximum_bytes, lock_fd=lock_fd, memory_bytes=memory_bytes)


def mail_patch_evidence(raw, *, timeout=5, maximum_bytes=DEFAULT_MAXIMUM_BYTES, lock_fd=None, patch_since=None, memory_bytes=None):
    """Hash one mail patch; optional UTC epoch horizon gates expensive hashing.

    Date is sender-provided evidence, not proof of receipt time. Missing,
    ambiguous, malformed, or timezone-free dates cannot qualify for hashing.
    The default keeps compatibility for callers without a horizon policy.
    """
    started = time.monotonic()
    if not isinstance(timeout, (int, float)) or not 0 < timeout <= 30:
        raise ValueError("timeout must be within (0, 30]")
    if type(maximum_bytes) is not int or not 1 <= maximum_bytes <= MAX_INPUT:
        raise ValueError("maximum_bytes must be within 1..16 MiB")
    if patch_since is not None and (type(patch_since) not in (int, float) or not math.isfinite(patch_since)):
        raise ValueError("patch_since must be a finite UTC epoch timestamp")
    result = {"patch_id": None, "algorithm": ALGORITHM, "git_version": None,
              "status": "candidate", "warnings": [], "message_id": None,
              "message_ids": [], "variant": None}
    if not isinstance(raw, bytes) or len(raw) > maximum_bytes:
        result["warnings"].append("mail_input_limit")
        return result
    result["variant"] = hashlib.sha256(raw).hexdigest()
    count = 0
    def factory(*args, **kwargs):
        nonlocal count
        count += 1
        if count > 256:
            raise ValueError("too many MIME parts")
        return EmailMessage(*args, **kwargs)
    try:
        mail = BytesParser(policy=policy.default.clone(message_factory=factory)).parsebytes(raw)
        mids = message_header_ids([str(v) for v in mail.get_all("Message-ID", [])])
        result["message_ids"] = mids
        if len(mids) != 1:
            result["warnings"].append("ambiguous_message_identity")
            return result
        result["message_id"] = mids[0]
        if patch_since is not None:
            dates = mail.get_all("Date", [])
            try:
                if len(dates) != 1 or dates[0].defects or len(str(dates[0])) > 256:
                    raise ValueError("missing or ambiguous date")
                date = parsedate_to_datetime(str(dates[0]))
                if date.tzinfo is None or date.utcoffset() is None:
                    raise ValueError("date has no trustworthy timezone")
                timestamp = date.timestamp()
                if not math.isfinite(timestamp):
                    raise ValueError("invalid date timestamp")
            except (ValueError, TypeError, OverflowError, IndexError):
                result["warnings"].append("unknown_horizon")
                result["horizon_status"] = "unknown_horizon"
                return result
            result["mail_date_epoch"] = timestamp
            if timestamp < patch_since:
                result["warnings"].append("outside_horizon")
                result["horizon_status"] = "outside_horizon"
                return result
            result["horizon_status"] = "within_horizon"
        stack, candidates, decoded = [mail], [], 0
        while stack:
            part = stack.pop()
            if part.defects:
                raise ValueError("MIME defects")
            if part.get_content_type() == "message/rfc822":
                continue
            if part.is_multipart():
                stack.extend(reversed(part.get_payload()))
                continue
            content_type = part.get_content_type()
            patch_attachment = content_type == "application/octet-stream" and (part.get_filename() or "").lower().endswith((".patch", ".diff"))
            if not patch_attachment and content_type not in {"text/plain", "text/x-patch", "text/x-diff", "text/patch", "application/x-patch", "application/x-diff"}:
                continue
            payload = part.get_payload(decode=True)
            if payload is None or part.defects:
                raise ValueError("undecodable MIME payload")
            decoded += len(payload)
            if decoded > maximum_bytes:
                raise ValueError("decoded MIME limit")
            charset = (part.get_content_charset() or "utf-8").lower()
            if charset not in {"utf-8", "utf8", "us-ascii", "ascii"}:
                raise ValueError("unsupported patch charset")
            payload.decode(charset, "strict")
            if re.search(br"(?m)^diff --git ", payload):
                candidates.append(payload)
        if len(candidates) != 1:
            result["warnings"].append("ambiguous_or_missing_patch_part")
            return result
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            result["warnings"].append("patch_id_timeout")
            return result
        result.update(_patch_evidence(candidates[0], timeout=remaining, maximum_bytes=maximum_bytes, lock_fd=lock_fd, memory_bytes=memory_bytes))
    except (ValueError, UnicodeError, RecursionError):
        result["warnings"].append("malformed_or_unsupported_mime")
    return result


def patch_equivalence(commit, mail):
    """An equivalence edge needs matching freshly computed algorithm AND Git."""
    return bool(commit.get("patch_id") and commit.get("patch_id") == mail.get("patch_id")
                and commit.get("algorithm") == mail.get("algorithm") == ALGORITHM
                and commit.get("git_version") and commit.get("git_version") == mail.get("git_version"))

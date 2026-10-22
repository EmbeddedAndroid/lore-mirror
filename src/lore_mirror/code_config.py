"""Opt-in code catalog validation; no Git, network or filesystem mutations.

Only the coordinator resolves upstream refs. Configuration declares intent, not
proof that a branch exists or that any code is ready to search.
"""
from __future__ import annotations

from datetime import date
import hashlib
import json
import math
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .errors import MirrorError
from .code_runtime import code_ref_valid

TRUSTED_CODE_HOSTS = frozenset({
    "git.kernel.org", "git.u-boot-project.org", "source.denx.de",
})
CODE_STATE_MAX_BYTES = 4 * 1024 * 1024


def code_catalog_fingerprint(tree):
    """Bind published objects to the upstream and selected ref policy."""
    fields = {key: tree[key] for key in ("url", "refs", "tag_patterns", "default_ref")}
    fields["branch_patterns"] = tree.get("branch_patterns", [])
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()


KERNEL_MANIFEST_URL = "https://git.kernel.org/manifest.js.gz"
DEFAULT_CODE_LIMITS = {
    "fetch_timeout_seconds": 900,
    "fetch_memory_bytes": 512 * 1024 * 1024,
    "repository_bytes": 8 * 1024 ** 3,
    "max_fetch_bytes": 2 * 1024 ** 3,
    "command_output_bytes": 1024 * 1024,
    "manifest_bytes": 4 * 1024 * 1024,
    "manifest_expanded_bytes": 32 * 1024 * 1024,
    "query_timeout": 15,
    "query_output_bytes": 4 * 1024 * 1024,
    "query_memory_bytes": 256 * 1024 * 1024,
    "max_file_bytes": 1024 * 1024,
    "max_line_bytes": 4096,
    "scan_limit": 1000,
    "max_sessions": 16,
    "session_bytes": 8 * 1024 * 1024,
    "cursor_ttl": 900,
}
_LIMIT_MAXIMA = {
    "fetch_timeout_seconds": 3600,
    "fetch_memory_bytes": 1024 ** 3,
    "repository_bytes": 1024 ** 4,
    "max_fetch_bytes": 64 * 1024 ** 3,
    "command_output_bytes": 16 * 1024 * 1024,
    "manifest_bytes": 16 * 1024 * 1024,
    "manifest_expanded_bytes": 64 * 1024 * 1024,
    "query_timeout": 120,
    "query_output_bytes": 16 * 1024 * 1024,
    "query_memory_bytes": 2 * 1024 ** 3,
    "max_file_bytes": 16 * 1024 * 1024,
    "max_line_bytes": 8192,
    "scan_limit": 10000,
    "max_sessions": 64,
    "session_bytes": 32 * 1024 * 1024,
    "cursor_ttl": 3600,
}
DEFAULT_CODE_INDEX = {
    "enabled": False,
    "symbols": False,
    "pilot_tree": None,
    "pilot_ref": None,
    "memory_bytes": 512 * 1024 * 1024,
    "timeout_seconds": 1800,
    "max_index_bytes": 2 * 1024 ** 3,
    "max_source_bytes": 1024 ** 3,
}
_INDEX_MAXIMA = {
    "memory_bytes": 768 * 1024 * 1024,
    "timeout_seconds": 7200,
    "max_index_bytes": 8 * 1024 ** 3,
    "max_source_bytes": 4 * 1024 ** 3,
}
DEFAULT_CODE_LINKS = {
    "enabled": False,
    "patch_since": None,
    "batch_commits": 50,
    "batch_mails": 25,
    "timeout_seconds": 30,
    "max_db_bytes": 1024 ** 3,
    "max_patch_bytes": 8 * 1024 * 1024,
}
_LINKS_MAXIMA = {
    "batch_commits": 500,
    "batch_mails": 250,
    "timeout_seconds": 120,
    "max_db_bytes": 2 * 1024 ** 3,
    "max_patch_bytes": 16 * 1024 * 1024,
}
_CODE_KEYS = {"enabled", "root_dir", "trees", "limits", "max_refs", "min_free_fraction", "index", "links"}
_TREE_KEYS = {"id", "name", "url", "default_ref", "refs", "tag_patterns", "branch_patterns", "index_refs",
              "fetch_mode", "manifest_url", "manifest_key", "interval_seconds"}
_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")


def _invalid(message):
    raise MirrorError("invalid_config", "code: " + message)


def _mapping(value, label, keys):
    if not isinstance(value, dict):
        _invalid(f"{label} must be an object")
    unknown = set(value) - keys
    if unknown:
        _invalid(f"unknown {label} field(s): " + ", ".join(sorted(map(str, unknown))))
    return dict(value)


def _integer(value, label, minimum=1, maximum=None):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        _invalid(f"{label} must be an integer >= {minimum}")
    if maximum is not None and value > maximum:
        _invalid(f"{label} must be <= {maximum}")
    return value


def validate_code_ref(value):
    """Return an exact, fully qualified head/tag ref, never a revision expression."""
    if not code_ref_valid(value):
        _invalid("refs must be exact refs/heads/... or refs/tags/... names")
    return value


def _ref_list(value, label, maximum, *, nonempty=False):
    if not isinstance(value, list) or len(value) > maximum or (nonempty and not value):
        _invalid(f"{label} must be {'a nonempty' if nonempty else 'a'} list of at most {maximum} refs")
    result = [validate_code_ref(ref) for ref in value]
    if len(set(result)) != len(result):
        _invalid(f"duplicate {label}")
    return result


def validate_code_url(value, *, manifest=False):
    """Validate canonical HTTPS endpoints; no credentials, redirects or URL tricks."""
    if (not isinstance(value, str) or len(value) > 2048
            or any(ord(c) < 33 or ord(c) > 126 for c in value)
            or any(c in value for c in ("\\", "%", "?", "#"))):
        _invalid("upstream URL must be plain trusted HTTPS without credentials, query or fragment")
    try:
        parsed = urlsplit(value)
        bad = (parsed.scheme != "https" or parsed.hostname not in TRUSTED_CODE_HOSTS
               or parsed.username is not None or parsed.password is not None
               or parsed.port not in (None, 443) or parsed.query or parsed.fragment)
    except ValueError:
        _invalid("invalid upstream URL")
    if bad:
        _invalid("upstream URL must use a trusted HTTPS host without credentials or a custom port")
    path = parsed.path.rstrip("/")
    if (not path.startswith("/") or "//" in parsed.path
            or any(part in (".", "..") for part in path.split("/"))
            or not re.fullmatch(r"/[A-Za-z0-9_./+-]+", path)):
        _invalid("unsafe upstream URL path")
    if manifest:
        if parsed.hostname != "git.kernel.org" or path != "/manifest.js.gz":
            _invalid("grokmirror manifest must be https://git.kernel.org/manifest.js.gz")
    else:
        prefix = "/pub/scm/linux/kernel/git/" if parsed.hostname == "git.kernel.org" else "/u-boot/"
        if not path.startswith(prefix) or not path.endswith(".git") or path == prefix + ".git":
            _invalid("upstream must be a .git repository in the trusted project namespace")
    return urlunsplit(("https", parsed.hostname, path, "", ""))


def _tag_patterns(value):
    if not isinstance(value, list) or len(value) > 32:
        _invalid("tag_patterns must be a list of at most 32 selectors")
    result = []
    for selector in value:
        selector = _mapping(selector, "tag selector", {"pattern", "keep"})
        pattern = selector.get("pattern")
        # Restrict globs to a single final '*' and literal prefix: no regex,
        # pathspec magic, character classes, broad refs/*, or arbitrary refspecs.
        if (not isinstance(pattern, str) or not pattern.startswith("refs/tags/")
                or not pattern.endswith("*") or pattern.count("*") != 1
                or pattern == "refs/tags/*"):
            _invalid("tag pattern must have a literal refs/tags/ prefix and one final '*'")
        validate_code_ref(pattern[:-1] + "0")
        if "keep" not in selector:
            _invalid("tag selector requires explicit keep (integer or null for all)")
        keep = selector["keep"]
        if keep is not None:
            _integer(keep, "tag keep", maximum=1000)
        result.append({"pattern": pattern, "keep": keep})
    if len({item["pattern"] for item in result}) != len(result):
        _invalid("duplicate tag patterns")
    return result


def _branch_patterns(value):
    """Mirror-only numeric release selectors; selection runs in the coordinator."""
    if not isinstance(value, list) or len(value) > 32:
        _invalid("branch_patterns must be a list of at most 32 selectors")
    result = []
    for selector in value:
        selector = _mapping(selector, "branch selector", {"pattern", "keep"})
        pattern = selector.get("pattern")
        if (not isinstance(pattern, str) or not pattern.startswith("refs/heads/")
                or not pattern.endswith("*") or pattern.count("*") != 1
                or pattern == "refs/heads/*"):
            _invalid("branch pattern must have a literal refs/heads/ prefix and one final '*'")
        validate_code_ref(pattern[:-1] + "0")
        _integer(selector.get("keep"), "branch keep", minimum=1, maximum=1)
        result.append({"pattern": pattern, "keep": 1})
    if len({item["pattern"] for item in result}) != len(result):
        _invalid("duplicate branch patterns")
    return result


def _normalize_index(value, *, trees, code_enabled):
    """Declare one opt-in pilot; a config memory budget is not a cgroup cap."""
    index = DEFAULT_CODE_INDEX | _mapping(value, "index", set(DEFAULT_CODE_INDEX))
    for key in ("enabled", "symbols"):
        if not isinstance(index[key], bool):
            _invalid("index." + key + " must be boolean")
    for key, maximum in _INDEX_MAXIMA.items():
        _integer(index[key], "index." + key, maximum=maximum)
    tree_id, ref = index["pilot_tree"], index["pilot_ref"]
    if (tree_id is None) != (ref is None):
        _invalid("index.pilot_tree and index.pilot_ref must be selected together")
    if tree_id is not None:
        if not isinstance(tree_id, str) or not _ID.fullmatch(tree_id):
            _invalid("index.pilot_tree must be a configured tree id")
        tree = next((tree for tree in trees if tree["id"] == tree_id), None)
        if tree is None:
            _invalid("index.pilot_tree must be a configured tree id")
        validate_code_ref(ref)
        if ref not in tree["index_refs"]:
            _invalid("index.pilot_ref must be an exact configured index_ref of pilot_tree")
    if index["enabled"] and (not code_enabled or tree_id is None):
        _invalid("index.enabled requires code.enabled and an explicit pilot_tree/pilot_ref pair")
    return index


def _normalize_links(value, *, code_enabled):
    """Bound local evidence work; never invent an operator's coverage horizon."""
    links = DEFAULT_CODE_LINKS | _mapping(value, "links", set(DEFAULT_CODE_LINKS))
    if not isinstance(links["enabled"], bool):
        _invalid("links.enabled must be boolean")
    for key, maximum in _LINKS_MAXIMA.items():
        _integer(links[key], "links." + key, maximum=maximum)
    since = links["patch_since"]
    if since is not None:
        if not isinstance(since, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", since):
            _invalid("links.patch_since must be an explicit YYYY-MM-DD date or null")
        try:
            date.fromisoformat(since)
        except ValueError:
            _invalid("links.patch_since must be a valid calendar date")
    if links["enabled"] and (not code_enabled or since is None):
        _invalid("links.enabled requires code.enabled and an explicit patch_since date")
    return links


def normalize_code_config(value=None, *, data_dir=None):
    """Normalize a code section for both API load_config and sync prepare_config.

    Callers should pass the enclosing data_dir. Existing mail-only configs remain
    disabled with an empty catalog. Invalid disabled sections also fail closed.
    Indexing is separately opt-in and restricted to one explicit pilot ref.
    This catalog declares intent, never current mirror or index readiness.
    """
    cfg = _mapping({} if value is None else value, "configuration", _CODE_KEYS)
    enabled = cfg.get("enabled", False)
    if not isinstance(enabled, bool):
        _invalid("enabled must be boolean")
    root = cfg.get("root_dir", str(Path(data_dir or ".") / "code"))
    if not isinstance(root, (str, Path)) or not str(root) or "\x00" in str(root):
        _invalid("root_dir must be a nonempty filesystem path")
    try:
        root = str(Path(root).expanduser().resolve())
    except (OSError, RuntimeError, ValueError) as exc:
        raise MirrorError("invalid_config", "code: root_dir cannot be resolved") from exc
    max_refs = _integer(cfg.get("max_refs", 10000), "max_refs", maximum=10000)
    reserve = cfg.get("min_free_fraction", .30)
    if (isinstance(reserve, bool) or not isinstance(reserve, (int, float))
            or not .30 <= reserve < 1 or not math.isfinite(reserve)):
        _invalid("min_free_fraction must be finite and at least 0.30 and less than 1")
    limits = DEFAULT_CODE_LIMITS | _mapping(cfg.get("limits", {}), "limit", set(DEFAULT_CODE_LIMITS))
    for key, number in limits.items():
        if key == "query_timeout":
            if (isinstance(number, bool) or not isinstance(number, (int, float))
                    or not 0 < number <= _LIMIT_MAXIMA[key] or not math.isfinite(number)):
                _invalid("query_timeout must be finite and in (0, 120] seconds")
        else:
            _integer(number, key, maximum=_LIMIT_MAXIMA[key])
    if limits["manifest_expanded_bytes"] < limits["manifest_bytes"]:
        _invalid("manifest_expanded_bytes must be >= manifest_bytes")
    raw_trees = cfg.get("trees", [])
    if not isinstance(raw_trees, list) or len(raw_trees) > 32:
        _invalid("trees must be a list of at most 32 trees")
    if enabled and not raw_trees:
        _invalid("enabling code requires an explicit nonempty tree catalog")
    trees = []
    ids, urls = set(), set()
    for raw in raw_trees:
        tree = _mapping(raw, "tree", _TREE_KEYS)
        tree_id = tree.get("id")
        if not isinstance(tree_id, str) or not _ID.fullmatch(tree_id):
            _invalid("tree id must be 1 to 64 lowercase letters, digits, '-' or '_', starting alphanumeric")
        if tree_id in ids:
            _invalid("duplicate tree id: " + tree_id)
        ids.add(tree_id)
        name = tree.get("name", tree_id)
        if not isinstance(name, str) or not 1 <= len(name) <= 160 or not name.isprintable():
            _invalid("tree name must be 1 to 160 printable characters")
        url = validate_code_url(tree.get("url"))
        # DENX and the current U-Boot host identify the same project namespace.
        # Detect duplicate mirrors but never silently change the requested host.
        identity = url.replace("https://source.denx.de/", "https://git.u-boot-project.org/")
        if identity in urls:
            _invalid("duplicate repository URL; combine its branches in one tree")
        urls.add(identity)
        refs = _ref_list(tree.get("refs"), "refs", max_refs, nonempty=True)
        default_ref = validate_code_ref(tree.get("default_ref"))
        if default_ref not in refs:
            _invalid("default_ref must be an explicitly selected exact ref")
        tag_patterns = _tag_patterns(tree.get("tag_patterns", []))
        branch_patterns = _branch_patterns(tree.get("branch_patterns", []))
        index_refs = _ref_list(tree.get("index_refs", []), "index_refs", max_refs)
        # Dynamic globs are deliberately not index selectors: current hot heads
        # are independently declared, and recent retained tags are not indexed.
        if not set(index_refs).issubset(refs):
            _invalid("index_refs must be explicitly selected exact refs")
        kernel = urlsplit(url).hostname == "git.kernel.org"
        expected_mode = "grokmirror_manifest" if kernel else "conditional_refs"
        mode = tree.get("fetch_mode", expected_mode)
        if mode != expected_mode:
            _invalid(f"{tree_id} requires fetch_mode={expected_mode}")
        interval = _integer(tree.get("interval_seconds", 3600), "interval_seconds",
                            minimum=300 if kernel else 3600, maximum=7 * 86400)
        normalized = {"id": tree_id, "name": name, "url": url,
                      "default_ref": default_ref, "refs": refs, "tag_patterns": tag_patterns,
                      "branch_patterns": branch_patterns, "index_refs": index_refs, "fetch_mode": mode, "interval_seconds": interval}
        if kernel:
            normalized["manifest_url"] = validate_code_url(tree.get("manifest_url", KERNEL_MANIFEST_URL), manifest=True)
            key = tree.get("manifest_key", urlsplit(url).path)
            if key != urlsplit(url).path:
                _invalid("manifest_key must exactly match the upstream repository path")
            normalized["manifest_key"] = key
        elif "manifest_url" in tree or "manifest_key" in tree:
            _invalid("conditional_refs trees must not configure a grokmirror manifest")
        trees.append(normalized)
    index = _normalize_index(cfg.get("index", {}), trees=trees, code_enabled=enabled)
    links = _normalize_links(cfg.get("links", {}), code_enabled=enabled)
    return {"enabled": enabled, "root_dir": root, "trees": trees, "limits": limits,
            "max_refs": max_refs, "min_free_fraction": float(reserve), "index": index, "links": links}

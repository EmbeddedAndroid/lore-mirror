#!/usr/bin/env python3
"""Tiny offline actual-engine fixture. Not a production memory/I/O qualification.
Usage: python integration_test.py /path/to/zoekt-git-index /path/to/lore-zoekt
No network, archive extraction, services, or production data.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

INDEXER, BRIDGE = map(str, map(Path, sys.argv[1:3]))
CTAGS = sys.argv[3] if len(sys.argv)>3 else None
COUNT = 0

def command(args, **kw):
    return subprocess.run(args, check=True, capture_output=True, timeout=30, **kw)

with tempfile.TemporaryDirectory(prefix="lore-zoekt-fixture-") as temp:
    root = Path(temp)
    repo, bare, index = root / "work", root / "fixture.git", root / "index"
    repo.mkdir()
    command(["git", "init", "-q", str(repo)])
    command(["git", "-C", str(repo), "config", "user.email", "fixture@example.invalid"])
    command(["git", "-C", str(repo), "config", "user.name", "Fixture"])
    (repo / ".gitattributes").write_text("ignored.c export-ignore\nsubst.c export-subst\n")
    (repo / "ignored.c").write_text("export_ignore_original\n")
    (repo / "subst.c").write_text("$Format:%H$\n")
    (repo / "a.b").mkdir()
    (repo / "axb").mkdir()
    (repo / "a.b" / "name.c").write_text("prefix_needle\n")
    (repo / "axb" / "file.c").write_text("prefix_needle\n")
    (repo / "symbol.c").write_text("int distinctive_function(int argument) { return argument; }\n")
    (repo / "many.c").write_text("many_needle\n" * 30)
    (repo / "long.c").write_text("long_needle " + "x" * 10000 + "\n")
    (repo / "link.c").symlink_to("link_target_needle")
    command(["git", "-C", str(repo), "add", "."])
    command(["git", "-C", str(repo), "commit", "-qm", "fixture"])
    oid = command(["git", "-C", str(repo), "rev-parse", "HEAD"]).stdout.decode().strip()
    command(["git", "clone", "--bare", "--no-local", "-q", str(repo), str(bare)])
    index.mkdir()
    command([INDEXER, "-index", str(index), "-branches", oid, "-parallelism", "1", "-submodules=false", "-incremental=false", "-delta=false", "-disable_ctags=true", "-file_limit", "1048576", str(bare)])

    def query(pattern="", **override):
        request = dict(index_dir=str(index), commit=oid, pattern=pattern, mode="literal", paths=[], case_sensitive=True, max_matches=100, max_bytes=65536, timeout_ms=5000)
        request.update(override)
        p = subprocess.run([BRIDGE], input=json.dumps(request).encode(), capture_output=True, timeout=10)
        data = json.loads(p.stdout)
        assert len(p.stdout) <= request["max_bytes"], (len(p.stdout), request)
        return p.returncode, data

    def check(ok, label):
        global COUNT
        assert ok, label
        COUNT += 1
        print("PASS", label)

    p = command([BRIDGE, "--version"])
    check(json.loads(p.stdout)["engine_commit"] == "e54cd91d1a64864fda7e681c9d31171eaab09ca7", "pinned identity")
    rc, r = query(op="verify")
    check(rc == 0 and r["commit_oid"] == oid, "bare immutable commit verified")
    rc, r = query("export_ignore_original")
    check(rc == 0 and r["matches"][0]["path"] == "ignored.c", "export-ignore does not remove source")
    rc, r = query("$Format:%H$")
    check(rc == 0 and r["matches"][0]["text"] == "$Format:%H$", "export-subst does not rewrite source")
    rc, r = query("prefix_needle", paths=["a.b"])
    check(rc == 0 and [m["path"] for m in r["matches"]] == ["a.b/name.c"], "literal regex-magic path boundaries")
    rc, r = query("many_needle", max_matches=2)
    check(rc == 0 and len(r["matches"]) <= 2 and r["truncated"], "match bound disclosed")
    rc, r = query("many_needle", max_bytes=1024)
    check(rc == 0 and r["truncated"] and "output_limit" in r["partial_reasons"], "encoded JSON byte bound disclosed")
    rc, r = query("long_needle", max_line_bytes=64)
    check(rc == 0 and len(r["matches"][0]["text"].encode()) <= 64 and r["matches"][0]["text_truncated"], "line bound disclosed")
    rc, r = query("nonexistent", commit="0" * 40)
    check(rc == 1 and r["error"] == "index_commit_mismatch", "wrong commit fails even no-hit query")
    rc, r = query("needle", mode="symbol")
    check(rc == 1 and r["error"] == "symbols_not_indexed", "missing symbols fail closed")
    rc, r = query("link_target_needle")
    check(rc == 0 and r["matches"][0]["path"] == "link.c", "upstream symlink text behavior characterized; public adapter must filter modes")
    if CTAGS:
        symbol_index = root / "symbol-index"
        symbol_index.mkdir()
        env = dict(os.environ, CTAGS_COMMAND=CTAGS)
        command([INDEXER, "-index", str(symbol_index), "-branches", oid, "-parallelism", "1", "-submodules=false", "-incremental=false", "-delta=false", "-require_ctags=true", str(bare)], env=env)
        rc, r = query("distinctive_function", mode="symbol", index_dir=str(symbol_index))
        check(rc == 0 and r["has_symbols"] and any(m["path"] == "symbol.c" for m in r["matches"]), "actual ctags lexical symbol lookup")
        rc, r = query("distinctive_function", index_dir=str(symbol_index), op="verify")
        check(rc == 0 and r["has_symbols"], "ctags metadata verified")
    # Upstream currently loses this pathname. Pin this characterization so an
    # upgrade cannot silently alter why the coordinator rejects such corpora.
    (repo / "name\nfile.c").write_text("newline_only_needle\n")
    command(["git", "-C", str(repo), "add", "."])
    command(["git", "-C", str(repo), "commit", "-qm", "newline fixture"])
    weird_oid = command(["git", "-C", str(repo), "rev-parse", "HEAD"]).stdout.decode().strip()
    command(["git", "--git-dir", str(bare), "fetch", "-q", str(repo), "HEAD"])
    weird_index = root / "weird-index"
    weird_index.mkdir()
    command([INDEXER, "-index", str(weird_index), "-branches", weird_oid, "-parallelism", "1", "-submodules=false", "-disable_ctags=true", str(bare)])
    rc, r = query("newline_only_needle", index_dir=str(weird_index), commit=weird_oid)
    check(rc == 1 and r["error"] == "invalid_result_path", "upstream newline filename fails closed on returned empty path")
    (index / "corrupt.zoekt").write_bytes(b"not-an-index")
    rc, r = query(op="verify")
    check(rc == 1 and r["error"] == "index_metadata_invalid", "extra corrupt shard fails verification")
    print(json.dumps({"passed": COUNT, "fixture_commit": oid, "index_files": sorted(p.name for p in index.iterdir()), "symbols_tested": bool(CTAGS), "production_resource_limits_tested": False}))

from __future__ import annotations
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import selectors
import shlex
import signal
import sqlite3
import subprocess
import time
import re
from .errors import MirrorError


def bounded_run(argv, *, input_bytes=b"", env=None, timeout=30, max_bytes=16*1024*1024):
    """No shell. Bound the pipes before allocation; kill the whole child group."""
    if len(input_bytes) > 65536:
        raise MirrorError("input_limit", "Subprocess input exceeds 64 KiB")
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env, start_new_session=True)
    except OSError as exc:
        raise MirrorError("backend_unavailable", f"Cannot start backend: {exc}", 503) from exc
    sel = selectors.DefaultSelector()
    out, err = bytearray(), bytearray()
    started = time.monotonic()
    try:
        input_offset=0
        if input_bytes:
            os.set_blocking(proc.stdin.fileno(),False)
            sel.register(proc.stdin,selectors.EVENT_WRITE,"in")
        else:
            proc.stdin.close()
        for stream, name in ((proc.stdout, "out"), (proc.stderr, "err")):
            os.set_blocking(stream.fileno(), False)
            sel.register(stream, selectors.EVENT_READ, name)
        while sel.get_map():
            if time.monotonic() - started > timeout:
                raise MirrorError("timeout", "Local query exceeded its time budget", 504)
            for key, _ in sel.select(0.05):
                if key.data=="in":
                    try:input_offset+=os.write(key.fileobj.fileno(),input_bytes[input_offset:input_offset+4096])
                    except BrokenPipeError:input_offset=len(input_bytes)
                    if input_offset>=len(input_bytes):
                        sel.unregister(key.fileobj);key.fileobj.close()
                    continue
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    sel.unregister(key.fileobj)
                    continue
                target = out if key.data == "out" else err
                target.extend(chunk)
                if len(out) > max_bytes or len(err) > 65536:
                    raise MirrorError("backend_output_limit", "Local query output exceeded its byte budget; narrow the query", 413)
        try:code = proc.wait(timeout=max(0.1, timeout-(time.monotonic()-started)))
        except subprocess.TimeoutExpired as exc:raise MirrorError("timeout","Local query exceeded its time budget",504) from exc
        if code:
            detail = err.decode("utf-8", "replace")[:1000]
            raise MirrorError("backend_error", f"Local backend exited {code}: {detail}", 503)
        return bytes(out)
    except BaseException:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        raise
    finally:
        sel.close()
        for stream in (proc.stdin,proc.stdout, proc.stderr):
            if not stream.closed:stream.close()


def query_quote(text):
    return '"' + str(text).replace('\\', '\\\\').replace('"', '\\"') + '"'


def _base_env(cfg):
    env = {key:os.environ[key] for key in ("PATH","LANG","LC_ALL","LC_CTYPE","TZ","TMPDIR","PERL5LIB","LD_LIBRARY_PATH","PERL_INLINE_DIRECTORY") if key in os.environ}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_TERMINAL_PROMPT": "0", "GIT_ALLOW_PROTOCOL": "file", "GIT_NO_REPLACE_OBJECTS":"1", "GIT_OPTIONAL_LOCKS":"0",
                "HOME": cfg["runtime_dir"], "XDG_CONFIG_HOME": cfg["runtime_dir"] + "/config",
                "XDG_CACHE_HOME": cfg["runtime_dir"] + "/cache",
                "XDG_DATA_HOME": cfg["runtime_dir"] + "/data"})
    for key, value in cfg.get("query_env", {}).items():
        if key not in {"PATH", "PERL5LIB", "LD_LIBRARY_PATH", "PERL_INLINE_DIRECTORY", "PERL5OPT"}:
            raise MirrorError("invalid_config", f"Unsupported query environment key {key}")
        env[key] = str(value)
    if cfg.get("public_inbox_config"):
        env["PI_CONFIG"] = cfg["public_inbox_config"]
    return env


# A writer commit can replace the Xapian revision or hold the SQLite write
# lock while a reader opens them; both clear within one commit.
TRANSIENT = re.compile(r"DatabaseModified|DatabaseOpeningError|database is locked|SQLITE_BUSY")
TRANSIENT_ATTEMPTS = 4


class PublicInboxBackend:
    def __init__(self, cfg):
        self.cfg, self.limits = cfg, cfg["limits"]
        self.extindex = Path(cfg.get("extindex", Path(cfg["data_dir"]) / "extindex")).resolve()
        self.env = _base_env(cfg)
        self.native_syntax = True

    def _run(self, argv, inp=b"", max_bytes=None):
        for attempt in range(TRANSIENT_ATTEMPTS):
            try:
                return bounded_run(argv, input_bytes=inp, env=self.env,
                                   timeout=self.limits["query_timeout"],
                                   max_bytes=max_bytes or self.limits["query_output_bytes"])
            except MirrorError as exc:
                if exc.code != "backend_error" or not TRANSIENT.search(exc.message):
                    raise
                if attempt == TRANSIENT_ATTEMPTS - 1:
                    raise MirrorError("index_busy", "Index commit in progress; retry shortly", 503) from exc
                time.sleep(0.2 * (attempt + 1))

    def search(self, query, limit, thread_id=None, list_ids=None):
        if list_ids is not None and not list_ids:return [],False
        if not self.extindex.exists():
            raise MirrorError("index_unavailable", "Local extindex has not been built", 503)
        if self.cfg["backend"] == "lei":
            argv = [self.cfg.get("lei_bin", "lei"), "q", "--no-save", "--no-remote",
                    "--only="+str(self.extindex), "--format=jsonl", "--limit="+str(limit),
                    "--sort=received", "--stdin"]
            if thread_id:
                argv += ["--thread-id="+thread_id]
            data = self._run(argv, query.encode())
        else:
            helper = self.cfg.get("query_helper")
            if not helper:
                raise MirrorError("invalid_config", "query_helper is required for direct public-inbox backend")
            request = {"op": "search", "query": query, "limit": limit,
                       "thread_id": thread_id, "extindex": str(self.extindex),
                       "config": self.cfg.get("public_inbox_config")}
            if list_ids is not None:
                request["eidx_keys"]=[item.get("newsgroup") or item["inbox_path"] for item in self.cfg["lists"] if item["id"] in list_ids]
            data = self._run([self.cfg.get("perl_bin", "perl"), helper], json.dumps(request).encode())
        records = []
        meta = {}
        for line in data.splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except (ValueError, TypeError) as exc:
                raise MirrorError("backend_protocol", "Backend emitted invalid JSON", 503) from exc
            if obj is None:
                continue
            if isinstance(obj, dict) and ("_meta" in obj):
                meta.update(obj["_meta"])
                continue
            if isinstance(obj, dict) and "error" in obj:
                raise MirrorError("backend_error", str(obj["error"]), 503)
            if isinstance(obj, list):
                entries = obj
            elif isinstance(obj, dict) and "results" in obj:
                entries = obj["results"]
                meta.update({k:v for k,v in obj.items() if k != "results"})
            else:
                entries = [obj]
            for record in entries:
                if record is not None:
                    records.append(self._normalize(record))
                    if len(records) >= limit:
                        break
            if len(records) >= limit:
                break
        return records, bool(meta.get("partial", len(records) >= limit))

    def search_scoped(self, query, limit, list_ids, thread_id=None):
        return self.search(query,limit,thread_id=thread_id,list_ids=list_ids)

    def _normalize(self, r):
        def addresses(x):
            if isinstance(x, list):
                return ", ".join((f"{p[0]} <{p[1]}>" if p[0] else p[1])
                                 if isinstance(p, list) and len(p)>1 else str(p) for p in x)
            return str(x or "")
        mid = r.get("message_id", r.get("m", ""))
        if isinstance(mid, list):
            mid = mid[0] if mid else ""
        blob = r.get("blob", r.get("oid", ""))
        return {"message_id": mid, "blob": blob, "variant": blob, "docid": r.get("docid"),
                "subject": r.get("subject", r.get("s", "")),
                "from": addresses(r.get("from", r.get("f"))),
                "from_email": r.get("from_email", ""),
                "date": r.get("date", r.get("dt", "")),
                "received_date": r.get("received_date", r.get("rt", "")),
                "list_ids": r.get("list_ids") or self.memberships(blob, mid),
                "references": re.findall(r"<([^<>]+)>",r["references"]) if isinstance(r.get("references"),str) else r.get("references",[]), "in_reply_to": r.get("in_reply_to", []),
                "provenance": {"backend": self.cfg["backend"], "git_blob": blob}}

    def memberships(self, blob, mid):
        if re.fullmatch(r"[0-9a-f]{40,64}", str(blob)):
            for over in self.extindex.glob("ei*/over.sqlite3"):
                try:
                    with sqlite3.connect(over.as_uri()+"?mode=ro", uri=True, timeout=1) as con:
                        rows = con.execute("SELECT DISTINCT i.eidx_key FROM xref3 x JOIN inboxes i ON i.ibx_id=x.ibx_id WHERE x.oidbin=?", (bytes.fromhex(blob),)).fetchall()
                    keys = {row[0] for row in rows}
                    return [x["id"] for x in self.cfg["lists"]
                            if x.get("newsgroup", "") in keys or x.get("inbox_path", "") in keys]
                except (sqlite3.Error, OSError):
                    continue
        # Lower-fidelity MID fallback is marked by get_message variant handling.
        found=[]
        for item in self.cfg["lists"]:
            db=Path(item.get("inbox_path", "/nonexistent"))/"msgmap.sqlite3"
            if not db.is_file(): continue
            try:
                with sqlite3.connect(db.as_uri()+"?mode=ro", uri=True, timeout=1) as con:
                    if con.execute("SELECT 1 FROM msgmap WHERE mid=?", (mid,)).fetchone():
                        found.append(item["id"])
            except sqlite3.Error:
                pass
        return found

    def raw(self, record):
        blob=str(record.get("blob", ""))
        if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", blob):
            raise MirrorError("invalid_blob", "Backend did not provide a valid Git object ID", 503)
        return self._run([self.cfg.get("git_bin", "git"), "--git-dir="+str(self.extindex/"ALL.git"),
                          "cat-file", "blob", blob], max_bytes=self.limits["raw_bytes"])

    def _lookup_alias(self, mid):
        # A second raw Message-ID can live in per-inbox msgmap while absent
        # from extindex's canonical mid term. Resolve only exact local links.
        candidates={};saw_mid=False;deadline=time.monotonic()+self.limits["query_timeout"]
        overs=list(self.extindex.glob("ei*/over.sqlite3"))
        for item in self.cfg["lists"]:
            if time.monotonic()>deadline:raise MirrorError("alias_lookup_limit","Exact-ID lookup exceeded its local metadata time budget",503)
            db=Path(item.get("inbox_path","/nonexistent"))/"msgmap.sqlite3"
            if not db.is_file():continue
            try:
                with sqlite3.connect(db.as_uri()+"?mode=ro",uri=True,timeout=1) as con:
                    mapped={r[0] for r in con.execute("SELECT num FROM msgmap WHERE mid=?",(mid,))}
                for local_over in Path(item["inbox_path"]).glob("xap*/over.sqlite3"):
                    with sqlite3.connect(local_over.as_uri()+"?mode=ro",uri=True,timeout=1) as con:
                        mapped.update(r[0] for r in con.execute("SELECT n.num FROM id2num n JOIN msgid m ON m.id=n.id WHERE m.mid=? AND n.num>0 LIMIT 101",(mid,)))
                if not mapped:continue
                saw_mid=True;group=item.get("newsgroup") or item["inbox_path"]
                for over in overs:
                    with sqlite3.connect(over.as_uri()+"?mode=ro",uri=True,timeout=1) as con:
                        for article in sorted(mapped)[:101]:
                            refs=con.execute("SELECT x.docid,hex(x.oidbin) FROM xref3 x JOIN inboxes i ON i.ibx_id=x.ibx_id WHERE i.eidx_key=? AND x.xnum=? LIMIT 101",(group,article)).fetchall()
                            for docid,oid in refs:candidates.setdefault((int(docid),oid.lower()),set()).add(item["id"])
            except sqlite3.Error as exc:raise MirrorError("alias_index_unavailable","Cannot verify exact-ID metadata",503) from exc
            if len(candidates)>100:break
        if not candidates:
            if saw_mid:raise MirrorError("alias_index_incomplete","ID exists in a local inbox but lacks a shared-index source link",503)
            return []
        docids=sorted({key[0] for key in candidates})[:100]
        query=" OR ".join(f"uid:{number}..{number}" for number in docids)
        metadata,partial=self.search(query,100);bases={}
        for record in metadata:
            if isinstance(record.get("docid"),int):bases[record["docid"]]=record;continue
            for over in overs:
                with sqlite3.connect(over.as_uri()+"?mode=ro",uri=True,timeout=1) as con:
                    matches=con.execute("SELECT DISTINCT docid FROM xref3 WHERE oidbin=? LIMIT 101",(bytes.fromhex(record["blob"]),)).fetchall()
                for row in matches:
                    if row[0] in docids:bases[row[0]]=record|{"docid":row[0]}
        result=[]
        for (docid,blob),lists in sorted(candidates.items()):
            if docid not in bases:continue
            base=bases[docid]
            result.append(base|{"message_id":mid,"indexed_primary_message_id":base["message_id"],
                "blob":blob,"variant":blob,"list_ids":sorted(lists),"_requested_alias":base["message_id"]!=mid,
                "_variants_partial":partial or len(candidates)>100})
        if not result:raise MirrorError("alias_index_incomplete","Exact-ID source link has no searchable metadata",503)
        return result[:100]

    def get(self, mid):
        from email import policy
        from email.parser import BytesHeaderParser
        from .semantics import message_header_ids
        rows, partial = self.search("mid:"+query_quote(mid), min(100, self.limits["scan_limit"]))
        rows=[r for r in rows if r["message_id"]==mid]
        direct=self._direct_variants(rows,partial) if rows else []
        # A primary-ID hit does not rule out another raw message whose secondary
        # ID has the same value. Union both exact linkage paths before filtering.
        aliases=self._lookup_alias(mid)
        merged={}
        for record in direct+aliases:
            key=record["blob"]
            if key not in merged:merged[key]=dict(record)
            else:
                target=merged[key]
                target["list_ids"]=sorted(set(target["list_ids"])|set(record["list_ids"]))
                target["_variants_partial"]=bool(target.get("_variants_partial") or record.get("_variants_partial"))
        incomplete=partial or len(merged)>100
        result=[];used=0;deadline=time.monotonic()+self.limits["query_timeout"]
        for record in sorted(merged.values(),key=lambda r:(bool(r.get("_requested_alias")),r["blob"]))[:100]:
            if time.monotonic()>deadline:incomplete=True;break
            raw=self.raw(record);used+=len(raw)
            if used>self.limits["raw_bytes"]:incomplete=True;break
            # id2num can also contain X-Alt-Message-ID and truncated index terms.
            # Only actual, exact raw Message-ID headers establish this identity.
            prefix=raw[:1048578];boundary=re.search(br"\r?\n\r?\n",prefix)
            if boundary:header=prefix[:boundary.end()]
            elif len(raw)<=1048576:header=raw
            else:incomplete=True;continue
            try:
                mail=BytesHeaderParser(policy=policy.default).parsebytes(header)
                ids=message_header_ids([str(value) for value in mail.get_all("Message-ID",[])])
            except (ValueError,RecursionError):incomplete=True;continue
            if mid not in ids:incomplete=True;continue
            result.append(record)
        if not result and merged:
            raise MirrorError("identity_unverified","Native metadata matched, but exact raw Message-ID identity could not be established within read limits",503)
        return [record|{"_variants_partial":incomplete or bool(record.get("_variants_partial"))} for record in result]

    def _direct_variants(self, rows, partial):
        # Extindex deduplicates transport-header variants into a single search
        # document. xref3 retains every raw blob; expose those as variants too.
        for over in self.extindex.glob("ei*/over.sqlite3"):
            try:
                grouped={};bases={}
                with sqlite3.connect(over.as_uri()+"?mode=ro",uri=True,timeout=1) as con:
                    for row in rows:
                        ids=[row["docid"]] if isinstance(row.get("docid"),int) else [r[0] for r in con.execute("SELECT DISTINCT docid FROM xref3 WHERE oidbin=? LIMIT 101",(bytes.fromhex(row["blob"]),))]
                        for docid in ids:
                            bases[docid]=row
                            for oid,key in con.execute("SELECT hex(x.oidbin),i.eidx_key FROM xref3 x JOIN inboxes i ON i.ibx_id=x.ibx_id WHERE x.docid=? LIMIT 1001",(docid,)):
                                token=(docid,oid.lower());grouped.setdefault(token,set()).add(key)
                expanded=[]
                for (docid,blob),keys in grouped.items():
                    lists=[x["id"] for x in self.cfg["lists"] if x.get("newsgroup") in keys or x.get("inbox_path") in keys]
                    expanded.append(bases[docid]|{"blob":blob,"variant":blob,"list_ids":lists})
                expanded.sort(key=lambda r:r["blob"])
                incomplete=partial or len(expanded)>100
                return [r|{"_variants_partial":incomplete} for r in expanded[:100]] or [r|{"_variants_partial":True} for r in rows]
            except (sqlite3.Error,OSError,ValueError):continue
        return [r|{"_variants_partial":True} for r in rows]

    def status(self):
        return {"backend": self.cfg["backend"], "native_query_syntax": True,
                "index_exists": self.extindex.exists()}


class FixtureBackend:
    """Small deterministic offline fixture store. Not a substitute full-text engine."""
    native_syntax=False
    def __init__(self,cfg):
        self.cfg=cfg
        self.db=Path(cfg["data_dir"])/"fixtures.sqlite3"

    def import_paths(self,path):
        from .semantics import parse_message
        self.db.parent.mkdir(parents=True,exist_ok=True)
        p=Path(path); files=sorted(p.glob("*.eml")) if p.is_dir() else [p]
        manifest={}
        if p.is_dir() and (p/"manifest.json").exists():
            doc=json.loads((p/"manifest.json").read_text())
            manifest=doc.get("files",{}) or {x["file"]:{"list_ids":[x["list_id"]]} for x in doc.get("messages",[])}
        with sqlite3.connect(self.db) as con:
            con.execute("CREATE TABLE IF NOT EXISTS messages(variant TEXT PRIMARY KEY,message_id TEXT,meta TEXT,raw BLOB)")
            for f in files:
                raw=f.read_bytes()
                item=manifest.get(f.name,{}) if isinstance(manifest,dict) else {}
                lists=item.get("list_ids",[]) or [x["id"] for x in self.cfg["lists"][:1]]
                meta=parse_message(raw,lists)
                if meta.get("message_id"):
                    con.execute("INSERT OR REPLACE INTO messages VALUES(?,?,?,?)",(meta["variant"],meta["message_id"],json.dumps(meta),raw))
        return len(files)

    def _rows(self):
        if not self.db.exists(): raise MirrorError("index_unavailable","Import offline fixtures first",503)
        with sqlite3.connect(self.db.as_uri()+"?mode=ro",uri=True) as con:
            return [json.loads(r[0]) for r in con.execute("SELECT meta FROM messages ORDER BY message_id,variant LIMIT 10001")]

    def get(self,mid): return [r for r in self._rows() if r["message_id"]==mid]
    def raw(self,record):
        with sqlite3.connect(self.db.as_uri()+"?mode=ro",uri=True) as con:
            r=con.execute("SELECT raw FROM messages WHERE variant=?",(record["variant"],)).fetchone()
        if not r: raise MirrorError("not_found","Message variant no longer available",404)
        return r[0]

    def search(self,query,limit,thread_id=None):
        rows=self._rows()
        if thread_id:
            if not any(r["message_id"]==thread_id for r in rows): return [],False
            linked={thread_id}; changed=True
            while changed:
                changed=False
                for r in rows:
                    refs=set(r.get("references",[]))|set(r.get("in_reply_to",[]))
                    if r["message_id"] in linked or refs&linked:
                        added=({r["message_id"]}|refs)-linked
                        if added: linked.update(added); changed=True
            rows=[r for r in rows if r["message_id"] in linked]
        try: terms=shlex.split(query)
        except ValueError as exc: raise MirrorError("invalid_query",str(exc)) from exc
        if any(t in ("OR","NOT","XOR","NEAR") or "*" in t or "(" in t for t in terms):
            raise MirrorError("fixture_query_unsupported","Fixture backend only supports AND plus simple f/s/b/mid/l/dfn/patchid/date terms")
        def matches(r):
            for term in terms:
                if term=="AND": continue
                key,sep,val=term.partition(":")
                if not sep: key,val="any",term
                lookup={"mid":r["message_id"],"f":r.get("from", ""),"s":r.get("subject",""),
                        "b":r.get("body",""),"l":" ".join(r.get("list_ids",[])),
                        "dfn":" ".join(r.get("paths",[])),"patchid":r.get("patch_id") or "",
                        "any":r.get("subject","")+" "+r.get("body","")}
                if key in ("d","rt"):
                    date=str(r.get("date" if key=="d" else "received_date", ""))[:10].replace("-","")
                    low,_,high=val.replace("-","").partition("..")
                    if not date or (low and date<low) or (high and date>high): return False
                elif key not in lookup: raise MirrorError("fixture_query_unsupported",f"Unsupported fixture field {key}")
                elif key in ("mid","patchid"):
                    if lookup[key]!=val: return False
                elif val.casefold() not in lookup[key].casefold(): return False
            return True
        rows=[r for r in rows if matches(r)]
        rows.sort(key=lambda r:(str(r.get("received_date") or r.get("date") or ""),r["message_id"],r["variant"]),reverse=True)
        return rows[:limit],len(rows)>limit
    def status(self): return {"backend":"fixture","native_query_syntax":False,"index_exists":self.db.exists(),"warning":"Offline test fixture corpus; query subset only"}

from __future__ import annotations
import base64
from contextlib import contextmanager
import datetime as dt
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from urllib.parse import quote
from .config import load_config
from .errors import MirrorError
from .backend import FixtureBackend, PublicInboxBackend, query_quote


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode()


_UNSET=object()

def utcnow():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def clip(text, byte_limit):
    raw=str(text or "").encode("utf-8")
    return raw[:byte_limit].decode("utf-8", "ignore"),len(raw)>byte_limit


def import_activity(row):
    """One-line summary, for example 'importing linux-arm-msm 176938/317572'."""
    def number(value):return str(value) if isinstance(value,int) and not isinstance(value,bool) else "?"
    text=f"importing {clip(row.get('inbox'),200)[0]} {number(row.get('done'))}/{number(row.get('total'))}"
    if row.get("phase")!="extindex":text+=f" ({clip(row.get('phase'),40)[0]})"
    rate=row.get("rate_per_hour")
    if isinstance(rate,(int,float)) and not isinstance(rate,bool):
        text+=f" at {round(rate)}/h"
        if isinstance(row.get("eta"),str):text+=f", ETA {clip(row['eta'],40)[0]}"
    return text+f" as of {clip(row.get('sampled_at'),40)[0]}"


class MirrorService:
    def __init__(self, config):
        self.cfg=load_config(config); self.limits=self.cfg["limits"]
        Path(self.cfg["runtime_dir"]).mkdir(parents=True,exist_ok=True)
        Path(self.cfg["issue_dir"]).mkdir(parents=True,exist_ok=True)
        self.backend=FixtureBackend(self.cfg) if self.cfg["backend"]=="fixture" else PublicInboxBackend(self.cfg)
        self._code_backend=None
        self._code_links=None
        self.code_query_slots=threading.BoundedSemaphore(1)
        self.secret=secrets.token_bytes(32)
        self.sessions={}; self.code_sessions={}; self.mutex=threading.RLock(); self.issue_calls=[]; self.query_slots=threading.BoundedSemaphore(4)
        self.issue_db=Path(self.cfg["issue_dir"])/"issues.sqlite3"
        with sqlite3.connect(self.issue_db) as db:
            db.execute("CREATE TABLE IF NOT EXISTS issues(id TEXT PRIMARY KEY, idem TEXT UNIQUE NOT NULL, created TEXT NOT NULL, category TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL, context TEXT NOT NULL)")
        self.tools={"mail.search":self.search,"mail.get_message":self.get_message,"mail.get_thread":self.get_thread,
                    "mail.get_series":self.get_series,"mail.in_flight":self.in_flight,"mail.was_posted":self.was_posted,
                    "mail.list_lists":self.list_lists,"mail.status":self.status,
                    "issue.report":self.issue_report,"issue.get":self.issue_get,"issue.list":self.issue_list}
        self.tools.update({"code.search":self.code_search,"code.get_file":self.code_get_file,
            "code.log":self.code_log,"code.show":self.code_show,"code.list_trees":self.code_list_trees,
            "code.list_refs":self.code_list_refs,"code.status":self.code_status,
            "code.related_mail":self.code_related_mail,"mail.related_commits":self.mail_related_commits})

    @property
    def code_backend(self):
        if self._code_backend is None:
            if not self.cfg["code"]["enabled"]:
                raise MirrorError("code_disabled","Code tools are disabled",503)
            from .code_backend import CodeBackend
            self._code_backend=CodeBackend(self.cfg)
        return self._code_backend

    @code_backend.setter
    def code_backend(self,value):
        self._code_backend=value

    @property
    def code_links(self):
        if self._code_links is None:
            from .code_link_backend import CodeLinkBackend
            self._code_links=CodeLinkBackend(self.cfg,self.code_backend)
        return self._code_links

    @staticmethod
    def _is_code_operation(operation):
        return operation.startswith("code.") or operation=="mail.related_commits"

    def _session_pool(self,operation):
        return self.code_sessions if self._is_code_operation(operation) else self.sessions

    def _session_limits(self,operation):
        return self.cfg["code"]["limits"] if self._is_code_operation(operation) else self.limits

    def generation(self):
        state=Path(self.cfg["data_dir"])/"sync-state.json"
        if state.exists():
            try:
                info=json.loads(state.read_text())
                return str(info.get("generation",info.get("last_indexed_at",state.stat().st_mtime_ns)))
            except (ValueError,OSError):
                return "invalid-state"
        if self.cfg["backend"]=="fixture":
            p=self.backend.db
            return str(p.stat().st_mtime_ns) if p.exists() else "empty"
        paths=list(self.backend.extindex.glob("ei*/over.sqlite3"))
        return str(max((p.stat().st_mtime_ns for p in paths),default=0))

    @contextmanager
    def read_guard(self):
        # Reads never wait for the sync worker. public-inbox commits each index
        # batch atomically (Xapian revisions, SQLite transactions), so a reader
        # sees the last committed batch while a list is being indexed; the
        # backend retries the brief window in which a commit replaces the
        # revision it opened. Only an unreadable checkpoint stops reads.
        state=Path(self.cfg["data_dir"])/"sync-state.json"
        if state.exists():
            try: json.loads(state.read_text())
            except (OSError,ValueError): raise MirrorError("index_state_invalid","Cannot validate the current index checkpoint",503)
        yield

    def invoke(self,name,arguments=_UNSET):
        if name not in self.tools: raise MirrorError("unknown_tool","Unknown tool",404)
        args={} if arguments is _UNSET else arguments
        if not isinstance(args,dict): raise MirrorError("invalid_arguments","Arguments must be a JSON object")
        try: request_bytes=len(encoded(args))
        except (UnicodeError,RecursionError,ValueError) as exc:raise MirrorError("invalid_encoding","Request has invalid Unicode or excessive nesting") from exc
        if request_bytes>65536: raise MirrorError("input_limit","Request exceeds 64 KiB",413)
        if "cursor" in args and args["cursor"] is not None and not isinstance(args["cursor"],str):
            raise MirrorError("invalid_cursor","cursor must be a string or null")
        if "limit" in args:
            lo,hi=(4,8192) if name=="mail.get_message" else (1,100 if name.startswith("issue.") else self.limits["max_page_size"])
            if isinstance(args["limit"],bool) or not isinstance(args["limit"],int) or not lo<=args["limit"]<=hi:
                raise MirrorError("invalid_limit",f"limit must be {lo}..{hi}")
        try:
            if name in ("code.related_mail","mail.related_commits"):
                if not self.code_query_slots.acquire(blocking=False):
                    raise MirrorError("busy", "Local code query concurrency limit reached", 429)
                try:
                    with self.read_guard():
                        before=self._operation_generation(name)
                        # Cursor pages read only cached bounded results and generations.
                        result=self.tools[name](**args)
                        if self._operation_generation(name)!=before:
                            raise MirrorError("stale_links_generation","Mail/code evidence changed during the query; retry",409)
                finally: self.code_query_slots.release()
            elif name.startswith("mail.") and name!="mail.status":
                if not self.query_slots.acquire(blocking=False):
                    raise MirrorError("busy", "Local query concurrency limit reached", 429)
                try:
                    with self.read_guard(): result=self.tools[name](**args)
                finally: self.query_slots.release()
            elif name.startswith("code.") and name!="code.status":
                if not self.code_query_slots.acquire(blocking=False):
                    raise MirrorError("busy", "Local code query concurrency limit reached", 429)
                try:
                    if args.get("cursor"):
                        result=self.tools[name](**args)
                    else:
                        with self.code_backend.read_guard(): result=self.tools[name](**args)
                finally: self.code_query_slots.release()
            else: result=self.tools[name](**args)
        except TypeError as exc:
            raise MirrorError("invalid_arguments",str(exc)[:300]) from exc
        except ValueError as exc:
            raise MirrorError("invalid_arguments",str(exc)[:300]) from exc
        # MCP emits both structured data and text. Reserve space for both plus envelope.
        budget=max(512,(self.limits["response_bytes"]-1024)//3)
        if len(encoded(result))>budget:
            raise MirrorError("response_limit","Result exceeds the response budget; request a smaller page",413)
        return result

    def _mid(self,mid):
        from .semantics import normalize_message_id
        if not isinstance(mid,str) or len(mid.encode())>2048: raise MirrorError("invalid_message_id","Invalid Message-ID")
        return normalize_message_id(mid)

    def _query(self,query):
        if not isinstance(query,str) or len(query.encode())>4096: raise MirrorError("invalid_query","Query must be text of at most 4096 bytes")
        if "\x00" in query or len(re.findall(r"\b(?:AND|OR|NOT|XOR|NEAR)\b",query))>64 or query.count("*")>8:
            raise MirrorError("query_complexity","Query complexity exceeds the budget")
        return query

    def _scope(self,lists=None,class_filter=None):
        if class_filter is not None and (not isinstance(class_filter,str) or not class_filter.strip() or len(class_filter)>128):
            raise MirrorError("invalid_class","class_filter must be a nonempty class name")
        known={x["id"]:x for x in self.cfg["lists"]}; alias={a:x["id"] for x in self.cfg["lists"] for a in x.get("aliases",[])}
        if lists is not None and (not isinstance(lists,list) or len(lists)>500 or any(not isinstance(x,str) or len(x)>128 for x in lists)):
            raise MirrorError("invalid_lists","lists must be a bounded list of inbox IDs")
        selected=set(known) if not lists else {alias.get(x,x) for x in lists}
        if selected-set(known):raise MirrorError("unknown_list","One or more inbox IDs are not in the local catalog; use mail.list_lists")
        if class_filter:
            classes={x.get("class","unclassified") for x in known.values()}
            if class_filter not in classes: raise MirrorError("unknown_class","Unknown list class")
            selected={x for x in selected if known[x].get("class","unclassified")==class_filter}
        return selected

    def _search_scoped(self,query,limit,scope,thread_id=None):
        if not scope:return [],False
        method=getattr(self.backend,"search_scoped",None)
        if method:return method(query,limit,scope,thread_id=thread_id)
        return self.backend.search(query,limit,thread_id=thread_id) if thread_id is not None else self.backend.search(query,limit)

    def _header(self,r):
        mid=r.get("message_id") or ""
        result={k:r.get(k) for k in ("message_id","variant","subject","from","from_email","date","received_date","list_ids")}
        result["subject"],st=clip(result["subject"],1024)
        result["from"],ft=clip(result["from"],1024)
        result["metadata_truncated"]=st or ft
        result["lore_url"]="https://lore.kernel.org/r/"+quote(mid,safe="@")
        return result

    def _coverage(self,selected=None):
        return {"lists":sorted(selected if selected is not None else [x["id"] for x in self.cfg["lists"]]),
                "mode":"full_history_selected_inboxes", "as_of":utcnow(),"index_generation":self.generation(),
                "native_query_syntax":self.backend.native_syntax,
                "completeness":"relative_to_locally_indexed_mail; see mail.status for sync gaps"}

    def _operation_generation(self, operation):
        if operation in ("code.related_mail","mail.related_commits"):
            return "mail:"+self.generation()+":code:"+self.code_backend.generation()+":links:"+self.code_links.generation()
        return self.code_backend.generation() if operation.startswith("code.") else self.generation()

    def _token(self,session,offset):
        payload=base64.urlsafe_b64encode(encoded({"s":session,"o":offset})).decode().rstrip("=")
        signature=hmac.new(self.secret,payload.encode(),hashlib.sha256).hexdigest()
        return payload+"."+signature

    def _cursor(self,token,operation,params):
        if not isinstance(token,str) or len(token)>2048: raise MirrorError("invalid_cursor","Invalid cursor")
        try:
            payload,sig=token.rsplit(".",1)
            if not hmac.compare_digest(sig,hmac.new(self.secret,payload.encode(),hashlib.sha256).hexdigest()): raise ValueError()
            info=json.loads(base64.urlsafe_b64decode(payload+"="*(-len(payload)%4)))
            session=self._session_pool(operation)[info["s"]]
            if session["expires"]<time.monotonic(): raise MirrorError("expired_cursor","Cursor expired; restart the query",409)
            if session["generation"]!=self._operation_generation(operation): raise MirrorError("stale_cursor","Index changed; restart the query",409)
            if session["op"]!=operation or session["params"]!=hashlib.sha256(encoded(params)).hexdigest(): raise MirrorError("cursor_mismatch","Cursor belongs to another query")
            offset=info["o"]
            if not isinstance(offset,int) or offset<0 or offset>len(session["items"]): raise ValueError()
            return info["s"],session,offset
        except MirrorError: raise
        except (ValueError,KeyError,TypeError,UnicodeError) as exc: raise MirrorError("invalid_cursor","Invalid or expired cursor") from exc

    def _page(self,op,params,items=None,*,limit=20,cursor=None,partial=False,extra=None):
        if isinstance(limit,bool) or not isinstance(limit,int) or not 1<=limit<=self.limits["max_page_size"]:
            raise MirrorError("invalid_limit",f"limit must be 1..{self.limits['max_page_size']}")
        with self.mutex:
            now=time.monotonic()
            sessions=self._session_pool(op)
            limits=self._session_limits(op)
            for key in list(sessions):
                if sessions[key]["expires"]<now: del sessions[key]
            if cursor:
                sid,session,offset=self._cursor(cursor,op,params)
            else:
                items=items or []
                sid=secrets.token_urlsafe(18);offset=0
                session={"op":op,"params":hashlib.sha256(encoded(params)).hexdigest(),"items":items,"partial":partial,
                         "extra":extra or {},"expires":now+limits["cursor_ttl"],"generation":self._operation_generation(op)}
                # Code metadata includes retained queries/paths even for no-hit
                # results. Charge the whole serialized session plus a bounded
                # allowance for its byte-count field, not just result rows.
                size=len(encoded(session))+32 if self._is_code_operation(op) else len(encoded(items))
                if size>limits["session_bytes"]: raise MirrorError("scan_limit","Candidate metadata exceeds query-session budget; narrow query",413)
                while sessions and (len(sessions)>=limits["max_sessions"] or sum(x["bytes"] for x in sessions.values())+size>limits["session_bytes"]):
                    del sessions[next(iter(sessions))]
                session["bytes"]=size
                sessions[sid]=session
            base={"items":[],"next_cursor":None,"has_more":True,"partial":session["partial"],"truncated":True,
                  "matched_count":len(session["items"]),"count_kind":"lower_bound" if session["partial"] else "exact_within_local_scope",
                  "index_generation":session["generation"],**session["extra"]}
            page=[];budget=max(256,max(512,(self.limits["response_bytes"]-1024)//3)-len(encoded(base))-384)
            for item in session["items"][offset:offset+limit]:
                if len(encoded(page+[item]))>budget:
                    if not page: raise MirrorError("entry_limit","Single entry exceeds budget; use narrower detail tool",413)
                    break
                page.append(item)
            end=offset+len(page); more=end<len(session["items"])
            return {"items":page,"next_cursor":self._token(sid,end) if more else None,"has_more":more,
                    "partial":session["partial"],"truncated":more or session["partial"],
                    "matched_count":len(session["items"]),"count_kind":"lower_bound" if session["partial"] else "exact_within_local_scope",
                    "index_generation":session["generation"],**session["extra"]}

    def _code_page(self, name, params, limit, cursor):
        if cursor:
            return self._page(name, params, limit=limit, cursor=cursor)
        kwargs={"limit":limit} if name=="code.log" else {}
        result=getattr(self.code_backend,name.split(".",1)[1])(**params,**kwargs)
        return self._page(name,params,result["items"],limit=limit,
            partial=result.get("partial",False),extra=result.get("metadata",{}))

    def code_search(self, query, tree, ref=None, paths=None, mode="literal", case_sensitive=True, limit=20, cursor=None):
        params=dict(query=query,tree=tree,ref=ref,paths=paths,mode=mode,case_sensitive=case_sensitive)
        return self._code_page("code.search",params,limit,cursor)

    def code_get_file(self, tree, ref, path, start_line=1, end_line=None, limit=20, cursor=None):
        return self._code_page("code.get_file",dict(tree=tree,ref=ref,path=path,start_line=start_line,end_line=end_line),limit,cursor)

    def code_log(self, tree, ref=None, path=None, since=None, until=None, limit=20, cursor=None):
        return self._code_page("code.log",dict(tree=tree,ref=ref,path=path,since=since,until=until),limit,cursor)

    def code_show(self, tree, commit, paths=None, part="metadata", limit=20, cursor=None):
        return self._code_page("code.show",dict(tree=tree,commit=commit,paths=paths,part=part),limit,cursor)

    def code_list_trees(self, limit=20, cursor=None):
        return self._code_page("code.list_trees",{},limit,cursor)

    def code_list_refs(self, tree, limit=20, cursor=None):
        return self._code_page("code.list_refs",dict(tree=tree),limit,cursor)

    def _links_page(self, name, params, limit, cursor):
        if cursor:
            return self._page(name, params, limit=limit, cursor=cursor)
        method=self.code_links.related_mail if name=="code.related_mail" else self.code_links.related_commits
        result=method(**params)
        return self._page(name,params,result["items"],limit=limit,
            partial=result.get("partial",False),extra=result.get("metadata",{}))

    def code_related_mail(self, tree, commit, ref=None, limit=20, cursor=None):
        return self._links_page("code.related_mail",dict(tree=tree,commit=commit,ref=ref),limit,cursor)

    def mail_related_commits(self, message_id, tree, ref=None, variant=None, limit=20, cursor=None):
        message_id=self._mid(message_id)
        return self._links_page("mail.related_commits",dict(message_id=message_id,tree=tree,ref=ref,variant=variant),limit,cursor)

    def code_status(self):
        if self._code_backend is None and not self.cfg["code"]["enabled"]:
            return {"enabled":False,"ready":False,"available":False,"status":"disabled",
                    "code_generation":"0","configured_trees":0,"trees":[],
                    "links":{"enabled":False,"status":"disabled"}}
        result=self.code_backend.status()
        result["links"]=(self.code_links.status() if self.cfg["code"]["links"]["enabled"]
                         else {"enabled":False,"status":"disabled"})
        return result

    def search(self,query="",lists=None,class_filter=None,limit=20,cursor=None):
        query=self._query(query);scope=self._scope(lists,class_filter)
        params={"query":query,"lists":sorted(scope)}
        rows=None;partial=False;extra=None
        if not cursor:
            candidates,partial=self._search_scoped(query,self.limits["scan_limit"],scope)
            rows=[self._header(r) for r in candidates if set(r.get("list_ids",[]))&scope]
            rows.sort(key=lambda r:(str(r.get("received_date") or r.get("date") or ""),r["message_id"],r["variant"]),reverse=True)
            extra={"coverage":self._coverage(scope),"scanned_count":len(candidates),"query":query,
                   "partial_reason":"candidate_scan_cap" if partial else None}
        return self._page("search",params,rows,limit=limit,cursor=cursor,partial=partial,extra=extra)

    def _message(self,mid,variant=None):
        from .semantics import parse_message
        rows=self.backend.get(mid)
        if not rows: raise MirrorError("not_found","Message-ID not found in local indexed corpus",404)
        if variant:
            matches=[r for r in rows if variant in (r.get("blob"),r.get("variant"))]
            if not matches:
                # Content SHA256 identities are exposed by parsed reads; resolve among exact MID variants.
                matches=[];resolved_bytes=0;deadline=time.monotonic()+self.limits["query_timeout"]
                for row in rows:
                    if time.monotonic()>deadline:raise MirrorError("variant_resolution_limit","Use a listed Git-blob variant to avoid an expensive content-hash scan",413)
                    source=self.backend.raw(row);resolved_bytes+=len(source)
                    if resolved_bytes>self.limits["raw_bytes"]:raise MirrorError("variant_resolution_limit","Variant content-hash scan exceeded its byte budget; use a listed Git-blob variant",413)
                    if hashlib.sha256(source).hexdigest()==variant:matches.append(row);break
            if not matches: raise MirrorError("variant_not_found","Message variant not found",404)
            record=matches[0]
        else: record=rows[0]
        raw=self.backend.raw(record)
        parsed=parse_message(raw,record.get("list_ids",[]))
        raw_id=parsed.get("message_id")
        if raw_id!=mid:
            if mid not in parsed.get("message_ids",[]):raise MirrorError("identity_mismatch","Raw source does not contain the requested Message-ID",503)
            parsed["message_id"]=mid
            parsed["identity_resolution"]="requested_id_present_among_multiple_raw_headers"
        else:parsed["identity_resolution"]="exact_raw_header"
        parsed["indexed_primary_message_id"]=record.get("indexed_primary_message_id",record["message_id"])
        parsed["raw_single_message_id"]=raw_id
        parsed["source_blob"]=record.get("blob")
        return parsed,raw,rows

    def get_message(self,message_id,variant=None,part="body",limit=8192,cursor=None,include_raw=False):
        mid=self._mid(message_id)
        if not isinstance(include_raw,bool):raise MirrorError("invalid_arguments","include_raw must be Boolean")
        if variant is not None and (not isinstance(variant,str) or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})",variant)):
            raise MirrorError("invalid_variant","variant must be a Git object ID or SHA-256 content hash")
        if part not in ("body","headers","raw"): raise MirrorError("invalid_part","part must be body, headers or raw")
        if include_raw: part="raw"
        if isinstance(limit,bool) or not isinstance(limit,int) or not 4<=limit<=8192: raise MirrorError("invalid_limit","Message chunk limit must be 4..8192 bytes")
        message,raw,variants=self._message(mid,variant)
        identity={"mid":mid,"variant":message["variant"],"part":part,"limit":limit}
        offset=0
        if cursor:
            if not isinstance(cursor,str) or len(cursor)>4096:raise MirrorError("invalid_cursor","Invalid message cursor")
            try:
                payload,sig=cursor.rsplit(".",1)
                if not hmac.compare_digest(sig,hmac.new(self.secret,payload.encode(),hashlib.sha256).hexdigest()):raise ValueError()
                info=json.loads(base64.urlsafe_b64decode(payload+"="*(-len(payload)%4)))
                if info["identity"]!=identity:raise MirrorError("cursor_mismatch","Cursor belongs to another message view")
                if info["generation"]!=self.generation():raise MirrorError("stale_cursor","Index changed; restart message read",409)
                if info["expires"]<time.time():raise MirrorError("expired_cursor","Message cursor expired",409)
                offset=info["offset"]
                if not isinstance(offset,int) or offset<0:raise ValueError()
            except MirrorError:raise
            except (ValueError,KeyError,TypeError,UnicodeError) as exc:raise MirrorError("invalid_cursor","Invalid message cursor") from exc
        if part=="raw":
            data=raw;chunk=data[offset:offset+limit];item={"raw_base64":base64.b64encode(chunk).decode(),"byte_offset":offset}
            end=offset+len(chunk)
        else:
            text=json.dumps(message.get("headers",{}),ensure_ascii=False) if part=="headers" else message.get("body","")
            data=text.encode("utf-8");chunk=data[offset:offset+limit].decode("utf-8","ignore")
            item={"text":chunk,"byte_offset":offset};end=offset+len(chunk.encode("utf-8"))
        if offset>len(data):raise MirrorError("invalid_cursor","Message offset exceeds content")
        more=end<len(data);next_cursor=None
        if more:
            payload=base64.urlsafe_b64encode(encoded({"identity":identity,"generation":self.generation(),"offset":end,"expires":time.time()+self.limits["cursor_ttl"]})).decode().rstrip("=")
            next_cursor=payload+"."+hmac.new(self.secret,payload.encode(),hashlib.sha256).hexdigest()
        return {"items":[item],"next_cursor":next_cursor,"has_more":more,"partial":False,"truncated":more,
                "message":self._header(message),"part":part,"variant_count":len(variants),"variants":[r.get("variant") for r in variants],
                "variants_partial":any(r.get("_variants_partial") for r in variants),
                "raw_sha256":hashlib.sha256(raw).hexdigest(),"raw_bytes":len(raw),"part_bytes":len(data),
                "coverage":self._coverage(set(message.get("list_ids",[]))),"index_generation":self.generation(),
                "parse_warnings":message.get("parse_warnings",[])[:10],
                "identity_resolution":message.get("identity_resolution"),"raw_message_ids":message.get("message_ids",[])[:20],
                "raw_message_ids_truncated":len(message.get("message_ids",[]))>20,
                "indexed_primary_message_id":message.get("indexed_primary_message_id")}

    def get_thread(self,message_id,lists=None,limit=50,cursor=None):
        mid=self._mid(message_id);scope=self._scope(lists,None);params={"mid":mid,"lists":sorted(scope)}
        rows=None;partial=False;extra=None
        if not cursor:
            anchors=self.backend.get(mid)
            if not anchors:raise MirrorError("not_found","Unknown thread Message-ID",404)
            scoped=[r for r in anchors if set(r.get("list_ids",[]))&scope]
            resolved_ids=sorted({r.get("indexed_primary_message_id",mid) for r in (scoped or anchors)})
            partial=any(r.get("_variants_partial") for r in anchors) or len(resolved_ids)>1 or any(value!=mid for value in resolved_ids)
            candidates=[];deadline=time.monotonic()+self.limits["query_timeout"]
            for resolved in resolved_ids:
                remaining=self.limits["scan_limit"]-len(candidates)
                if remaining<=0 or time.monotonic()>deadline:partial=True;break
                batch,more=self._search_scoped("",remaining,scope,thread_id=resolved)
                candidates.extend(batch);partial|=more
            rows=[];seen=set()
            for r in candidates:
                key=(r["message_id"],r["variant"])
                if key in seen or not set(r.get("list_ids",[]))&scope:continue
                seen.add(key)
                rows.append(self._header(r)|{"references":r.get("references",[])[:50],"in_reply_to":r.get("in_reply_to",[])[:10]})
            rows.sort(key=lambda r:(str(r.get("date") or r.get("received_date") or ""),r["message_id"],r["variant"]))
            extra={"coverage":self._coverage(scope),"anchor":mid,"resolved_thread_anchor":resolved_ids[0] if len(resolved_ids)==1 else None,"resolved_thread_anchors":resolved_ids,"alias_thread_note":("Exact raw IDs resolve to one or more local canonical threads; additional alias-reference threads are not proven complete" if any(value!=mid for value in resolved_ids) or len(resolved_ids)>1 else None),"body_tool":"mail.get_message","scanned_count":len(candidates)}
        return self._page("get_thread",params,rows,limit=limit,cursor=cursor,partial=partial,extra=extra)

    def get_series(self,message_id,all_versions=True,limit=20,cursor=None):
        from .semantics import summarize_series,series_candidates_query
        mid=self._mid(message_id); params={"mid":mid,"all_versions":all_versions}; rows=None;partial=False;extra=None
        if not isinstance(all_versions,bool): raise MirrorError("invalid_arguments","all_versions must be Boolean")
        if not cursor:
            anchor,_,anchor_variants=self._message(mid)
            resolved=anchor.get("indexed_primary_message_id",mid)
            anchor_ambiguous=len({r.get("indexed_primary_message_id",mid) for r in anchor_variants})>1
            anchor["message_id"]=resolved
            records,partial=self.backend.search("",min(self.limits["scan_limit"],500),thread_id=resolved)
            partial |= resolved!=mid or anchor_ambiguous or any(r.get("_variants_partial") for r in anchor_variants)
            candidate_query_unavailable=False
            if all_versions:
                try: candidate_query=self._query(series_candidates_query(anchor))
                except (ValueError,MirrorError): candidate_query_unavailable=True;partial=True
                else:
                    others,more=self.backend.search(candidate_query,min(self.limits["scan_limit"],500))
                    records+=others;partial|=more
            messages={anchor["variant"]:anchor};raw_total=0;deadline=time.monotonic()+self.limits["query_timeout"]
            seen_records=set()
            for r in records:
                key=(r.get("message_id"),r.get("blob") or r.get("variant"))
                if key in seen_records:continue
                seen_records.add(key)
                if time.monotonic()>deadline:partial=True;break
                if r.get("variant") in messages: continue
                raw=self.backend.raw(r);raw_total+=len(raw)
                if raw_total>32*1024*1024: partial=True;break
                from .semantics import parse_message
                m=parse_message(raw,r.get("list_ids",[]));messages.setdefault(m["variant"],m)
                if len(messages)>=500:partial=True;break
            summary=summarize_series(list(messages.values()),resolved,all_versions=all_versions)
            # Flatten versions, members, trailers and links so even a huge version is pageable.
            rows=[]
            for category in ("versions","candidate_versions"):
                for number,group in enumerate(summary.get(category,[])):
                    group_id=category+":"+str(number)
                    scalar={k:v for k,v in group.items() if not isinstance(v,(list,dict))}
                    rows.append({"kind":"version","group_id":group_id,"classification":category,**scalar})
                    for field,value in group.items():
                        if field=="messages":
                            for member in value:
                                row={"kind":"member","group_id":group_id,**member}
                                row["subject"],row["subject_truncated"]=clip(row.get("subject"),1024)
                                if len(row.get("variants",[]))>20:
                                    row["variants"]=row["variants"][:20];row["variants_truncated"]=True
                                rows.append(row)
                        elif isinstance(value,list):
                            for i in range(0,len(value),5):
                                rows.append({"kind":"version_detail","group_id":group_id,"field":field,"values":value[i:i+5]})
                        elif isinstance(value,dict):
                            for key,item in value.items():
                                chunks=[item[i:i+5] for i in range(0,len(item),5)] if isinstance(item,list) else [item]
                                for chunk in chunks:rows.append({"kind":"version_detail","group_id":group_id,"field":field,"key":key,"value":chunk})
            for message in messages.values():
                for trailer in message.get("trailers",[]):
                    # Provenance is retained, including quoted/copied/retracted status; never an approval score.
                    safe={k:v for k,v in trailer.items() if not isinstance(v,(dict,list))}
                    for k,v in list(safe.items()):
                        if isinstance(v,str):
                            safe[k],clipped=clip(v,2048)
                            if clipped:safe["value_truncated"]=True
                    rows.append({"kind":"trailer","message_id":message.get("message_id"),**safe})
            for link in summary.get("version_links",[]):rows.append({"kind":"version_link",**link})
            if not rows: rows=[{"kind":"summary","anchor":mid,"reason":"No patch postings found in retrieved thread"}]
            if len(rows)>10000:rows=rows[:10000];partial=True
            for conflict_mid,conflict_variants in summary.get("variant_conflicts",{}).items():
                for i in range(0,len(conflict_variants),50):
                    rows.append({"kind":"variant_conflict","message_id":conflict_mid,"variants":conflict_variants[i:i+50]})
            partial |= bool(summary.get("version_links_truncated")) or any(m.get("trailers_truncated") for m in messages.values())
            extra={"version_discovery":{"method":"explicit thread plus subject-author candidate search","complete":False,
                       "candidate_query_unavailable":candidate_query_unavailable,
                       "limitations":"Detached or retitled rerolls may not be discoverable; all_versions means all found candidates"},"anchor":mid,"resolved_series_anchor":resolved,"series_anchor_ambiguous":anchor_ambiguous,"selected_anchor_variant":anchor["variant"],"coverage":self._coverage(),"input_message_count":len(messages),
                   "applied_status":"unknown","partial_reason":("ambiguous_or_aliased_anchor" if resolved!=mid or anchor_ambiguous else "series_candidate_or_byte_cap") if partial else None,
                   "series_identity":"thread association or labelled heuristic candidates; not proof of identity"}
        return self._page("get_series",params,rows,limit=limit,cursor=cursor,partial=partial,extra=extra)

    def in_flight(self,paths=None,subsystem=None,since=None,until=None,lists=None,limit=20,cursor=None):
        from .semantics import match_paths,classify_subsystems,parse_message
        if subsystem is not None:
            if not isinstance(subsystem,str) or not subsystem.strip() or len(subsystem)>512:
                raise MirrorError("invalid_subsystem","subsystem must be a bounded nonempty name")
            subsystem=subsystem.strip()
        paths=[] if paths is None else paths
        if not isinstance(paths,list) or len(paths)>20 or any(not isinstance(x,str) or len(x)>1024 or x.startswith("/") or ".." in x.split("/") for x in paths): raise MirrorError("invalid_paths","Use up to 20 repository-relative paths")
        if not paths and not subsystem: raise MirrorError("invalid_arguments","paths or subsystem is required")
        scope=self._scope(lists,None)
        if since is None: since=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(days=180)).date().isoformat()
        for value in (since,until):
            if value is not None:
                if not isinstance(value,str):raise MirrorError("invalid_date","Use ISO YYYY-MM-DD date strings")
                try: dt.date.fromisoformat(value)
                except ValueError as exc: raise MirrorError("invalid_date","Use ISO YYYY-MM-DD dates") from exc
        if until and since>until:raise MirrorError("invalid_date","since must not be after until")
        params={"paths":paths,"subsystem":subsystem,"since":since,"until":until,"lists":sorted(scope)}
        rows=None;partial=False;extra=None
        if not cursor:
            query="d:"+since.replace("-","")+".."+(until or "").replace("-","")
            if paths and not any(any(c in path for c in "*?[") for path in paths) and (self.backend.native_syntax or len(paths)==1):
                terms=["dfn:"+query_quote(path.rstrip("/")) for path in paths]
                query+=" AND "+(terms[0] if len(terms)==1 else "("+" OR ".join(terms)+")")
            candidates,partial=self._search_scoped(query,min(2000,self.limits["scan_limit"]),scope)
            catalogs={};catalog_provenance={};missing_classes=set()
            list_classes={x["id"]:x.get("class","unclassified") for x in self.cfg["lists"]}
            if subsystem:
                configured=self.cfg.get("maintainers_catalogs",{})
                if not configured and self.cfg.get("maintainers_file"):
                    configured={"*":{"path":self.cfg["maintainers_file"],"commit":self.cfg.get("maintainers_commit"),"project":self.cfg.get("maintainers_project")}}
                wanted_classes={list_classes[x] for x in scope}
                for key,entry in configured.items():
                    if key!="*" and key not in wanted_classes:continue
                    try:
                        path=Path(entry["path"])
                        if path.stat().st_size>4*1024*1024:raise MirrorError("input_limit","MAINTAINERS exceeds 4 MiB")
                        raw_rules=path.read_bytes();rules=raw_rules.decode("utf-8")
                    except (OSError,UnicodeError) as exc:raise MirrorError("maintainers_unavailable","Configured MAINTAINERS catalog is unreadable",503) from exc
                    digest=hashlib.sha256(raw_rules).hexdigest()
                    if entry.get("sha256") and entry["sha256"]!=digest:raise MirrorError("catalog_integrity","MAINTAINERS no longer matches its pinned hash",503)
                    catalogs[key]=rules
                    catalog_provenance[key]={"sha256":digest,"commit":entry.get("commit"),"project":entry.get("project",key),"source_url":entry.get("source_url"),"coverage":"root catalog F/X rules only; N/K and nested board catalogs are not exhaustive"}
                if not catalogs:raise MirrorError("maintainers_unavailable","No matching pinned MAINTAINERS catalog is configured",503)
            rows=[];used=0;scanned=0;deadline=time.monotonic()+self.limits["query_timeout"]
            for r in candidates:
                if time.monotonic()>deadline:partial=True;break
                if not set(r.get("list_ids",[]))&scope:continue
                raw=self.backend.raw(r);used+=len(raw);scanned+=1
                if used>32*1024*1024:partial=True;break
                m=parse_message(raw,r.get("list_ids",[]))
                subs=[]
                if subsystem:
                    classes={list_classes[x] for x in set(r.get("list_ids",[]))&scope}
                    active=["*"] if "*" in catalogs else sorted(classes&set(catalogs))
                    if "*" not in catalogs:missing_classes.update(classes-set(catalogs))
                    for key in active:
                        for match in classify_subsystems(m,catalogs[key]):
                            if match.get("name","").casefold()!=subsystem.casefold():continue
                            match=dict(match)
                            for field in ("include_rules","exclude_rules","matched_paths","excluded_paths"):
                                values=match.get(field,[]);match[field]=values[:20];match[field+"_truncated"]=len(values)>20
                            match["source_provenance"]=catalog_provenance[key]
                            subs.append(match)
                if (not paths or match_paths(m,paths)) and (not subsystem or subs) and m.get("patch"):
                    rows.append(self._header(m)|{"paths":m.get("paths",[])[:10],"paths_truncated":len(m.get("paths",[]))>10,"subsystems":subs[:10],"subsystems_truncated":len(subs)>10,"applied_status":"unknown","in_flight_basis":"recent_patch_with_application_unknown"})
            partial |= bool(missing_classes)
            extra={"coverage":self._coverage(scope),"scanned_count":scanned,"status_semantics":"Candidates only; no evidence of non-application is inferred","since":since,"until":until,
                   "maintainers_provenance":catalog_provenance or None,"classification_complete":False if subsystem else None,"missing_catalog_classes":sorted(missing_classes)}
        return self._page("in_flight",params,rows,limit=limit,cursor=cursor,partial=partial,extra=extra)

    def was_posted(self,message_id=None,patch_id=None,subject=None,diff=None,lists=None,limit=20,cursor=None):
        from .semantics import stable_patch_id
        if not any((message_id,patch_id,subject,diff)): raise MirrorError("invalid_arguments","Provide a Message-ID, patch ID, subject or inline diff")
        if diff is not None and (not isinstance(diff,str) or len(diff.encode())>49152): raise MirrorError("input_limit","Inline diff limit is 48 KiB")
        if message_id: message_id=self._mid(message_id)
        if diff:
            computed=stable_patch_id(diff)
            if not computed:raise MirrorError("patch_id_unavailable","Input did not yield exactly one stable patch ID; no negative posting claim can be made")
            if patch_id and patch_id!=computed:raise MirrorError("conflicting_patch_id","Provided patch ID differs from inline diff")
            patch_id=computed
        if patch_id and not re.fullmatch(r"[0-9a-f]{40,64}",patch_id):raise MirrorError("invalid_patch_id","Expected hexadecimal stable patch ID")
        if subject is not None:subject=self._query(subject)
        scope=self._scope(lists,None)
        params={"mid":message_id,"patch_id":patch_id,"subject":subject,"diff_hash":hashlib.sha256((diff or "").encode()).hexdigest(),"lists":sorted(scope)}
        rows=None;partial=False;extra=None
        if not cursor:
            rows=[];seen=set()
            for label,query in (("exact_message_id","mid:"+query_quote(message_id) if message_id else None),
                                ("stable_patch_id_equivalence","patchid:"+patch_id if patch_id else None),
                                ("subject_candidate","s:"+query_quote(subject) if subject else None)):
                if not query:continue
                if label=="exact_message_id":records=self.backend.get(message_id);more=any(r.get("_variants_partial") for r in records)
                else:records,more=self._search_scoped(query,self.limits["scan_limit"],scope)
                partial|=more
                for r in records:
                    if not set(r.get("list_ids",[]))&scope:continue
                    key=(r["message_id"],r["variant"],label)
                    if key in seen:continue
                    seen.add(key);rows.append(self._header(r)|{"evidence_type":"exact_archive_message_id_alias" if r.get("_requested_alias") else label,"indexed_primary_message_id":r.get("indexed_primary_message_id"),"applied_status":"unknown"})
            extra={"coverage":self._coverage(scope),"posted_result":"matches_found" if rows else "no_match_in_local_scope",
                   "applied_status":"unknown","application_note":"v1 does not verify named-tree commits or refs"}
        return self._page("was_posted",params,rows,limit=limit,cursor=cursor,partial=partial,extra=extra)

    def list_lists(self,limit=100,cursor=None,class_filter=None):
        self._scope(None,class_filter)
        state={};p=Path(self.cfg["data_dir"])/"sync-state.json"
        if p.exists():
            try: state=json.loads(p.read_text()).get("lists",{})
            except (ValueError,OSError):pass
        rows=[]
        for x in self.cfg["lists"]:
            if class_filter and x.get("class")!=class_filter:continue
            st=state.get(x["id"],{})
            row={k:x.get(k) for k in ("id","class","aliases","newsgroup")}
            row["sync"]={k:st.get(k) for k in ("status","checked_at","fetched_at","indexed_at","retry_count","next_retry_at") if k in st}
            row["sync"]["last_error"]=clip(st.get("last_error"),500)[0] or None
            row["sync"]["message_count"]=st.get("measurements",{}).get("messages")
            row["sync"]["epoch_count"]=len(st.get("upstream_epochs",{}))
            rows.append(row)
        return self._page("list_lists",{"class":class_filter},rows,limit=limit,cursor=cursor)

    def status(self):
        state={};p=Path(self.cfg["data_dir"])/"sync-state.json"
        if p.exists():
            try: state=json.loads(p.read_text())
            except ValueError:state={"error":"invalid sync state"}
        # Operational state can be large. No local filesystem paths or arbitrary error payloads escape.
        safe={k:state.get(k) for k in ("generation","updating","unsafe_index","status","cycle_started_at","cycle_finished_at","next_run_at","pending_lists","last_success","last_indexed_at","last_error","last_error_at","last_error_step","stage","last_priority_refresh_at","last_priority_refresh_status","next_priority_refresh_at") if k in state}
        if "last_error" in safe:safe["last_error"]=clip(safe["last_error"],1000)[0]
        importing=state.get("importing")
        if isinstance(importing,dict) and isinstance(importing.get("inbox"),str):
            safe["importing"]={k:importing.get(k) for k in ("inbox","phase","done","total","slices","rate_per_hour","rate_basis","eta","started_at","sampled_at")}
            safe["activity"]=import_activity(safe["importing"])
        history=state.get("error_history")
        if isinstance(history,list):
            safe["error_history"]=[{"error":clip(str(h.get("error")),300)[0],**{k:h.get(k) for k in ("at","step","cleared_at","cleared_by")}}
                for h in history[-5:] if isinstance(h,dict)]
        interval=float(self.cfg.get("interval_seconds",3600))
        freshness={"last_success_at":state.get("last_success"),"age_seconds":None,"stale":None,"stale_after_seconds":2*interval}
        try:
            checkpoint=dt.datetime.fromisoformat(state["last_success"].replace("Z","+00:00"))
            if checkpoint.tzinfo is None:raise ValueError("timezone missing")
            age=(dt.datetime.now(dt.timezone.utc)-checkpoint).total_seconds()
            if age < -60:raise ValueError("future checkpoint")
            freshness.update(age_seconds=max(0,round(age,1)),stale=age>2*interval)
        except (KeyError,ValueError,TypeError,AttributeError):freshness["reason"]="No valid successful-sync checkpoint"
        # A first full pass can take weeks; priority lists refresh within it.
        priority={"last_refresh_at":state.get("last_priority_refresh_at"),"status":state.get("last_priority_refresh_status"),
                  "age_seconds":None,"stale":None,"stale_after_seconds":2*interval,"lists":list(self.cfg.get("priority_lists") or [])}
        try:
            checkpoint=dt.datetime.fromisoformat(state["last_priority_refresh_at"].replace("Z","+00:00"))
            if checkpoint.tzinfo is None:raise ValueError("timezone missing")
            age=(dt.datetime.now(dt.timezone.utc)-checkpoint).total_seconds()
            if age < -60:raise ValueError("future checkpoint")
            priority.update(age_seconds=max(0,round(age,1)),stale=age>2*interval or priority["status"]!="ready")
        except (KeyError,ValueError,TypeError,AttributeError):priority["reason"]="No valid priority refresh checkpoint"
        if priority["lists"]:freshness["priority"]=priority
        daemon_path=Path(self.cfg["data_dir"])/"daemon-state.json";daemon={}
        if daemon_path.exists():
            try:
                if daemon_path.stat().st_size>65536:raise ValueError("oversized daemon state")
                recorded=json.loads(daemon_path.read_text())
                daemon={key:recorded[key] for key in ("status","started_at","cycles_completed","first_successful_cycle_at","last_successful_cycle_at","last_cycle_status","last_cycle_finished_at","next_scheduled_at","stopped_at","code_status","code_error") if key in recorded}
                daemon["observation"]="Recorded scheduler state, not a live process probe; workspace termination may stop updates"
            except (OSError,ValueError,TypeError):daemon={"error":"invalid daemon state"}
        return {"version":"0.1.0","archive_read_only":True,"query_network":False,"issue_writes":"local_only",
                "application_verification":"not_implemented_unknown_only","generation":self.generation(),
                "backend":self.backend.status(),"sync":safe,"freshness":freshness,"daemon":daemon,"list_count":len(self.cfg["lists"]),
                "limits":self.limits,"as_of":utcnow(),"authentication":"deferred; trusted local/private proxy only"}

    def issue_report(self,title,description,category="bug",message_ids=None,idempotency_key=None):
        if category not in {"bug","missing_data","wrong_thread","wrong_series","search_mismatch","staleness","security"}:raise MirrorError("invalid_category","Unknown issue category")
        if not isinstance(title,str) or not 1<=len(title)<=200 or not isinstance(description,str) or not 1<=len(description.encode())<=12000: raise MirrorError("invalid_issue","Title 1..200 characters; description 1..12000 bytes")
        if not idempotency_key or not isinstance(idempotency_key,str) or not 8<=len(idempotency_key)<=128:raise MirrorError("invalid_idempotency_key","Provide an idempotency key of 8..128 characters")
        message_ids=message_ids or []
        if not isinstance(message_ids,list) or len(message_ids)>20:raise MirrorError("invalid_message_ids","At most 20 Message-IDs")
        mids=[self._mid(x) for x in message_ids]
        idem=hashlib.sha256(idempotency_key.encode()).hexdigest()
        context={"message_ids":mids,"index_generation":self.generation(),"status":"open","local_only":True}
        prospective={"id":"0"*32,"created":utcnow(),"category":category,"title":title,"description":description,
                     **context,"idempotent_replay":True}
        if len(encoded(prospective))>max(512,(self.limits["response_bytes"]-1024)//3):
            raise MirrorError("issue_response_limit","Issue content exceeds the configured readable response budget; reduce excerpts or Message-IDs",413)
        with self.mutex,sqlite3.connect(self.issue_db,timeout=5) as db:
            row=db.execute("SELECT id FROM issues WHERE idem=?",(idem,)).fetchone()
            if row:
                existing=self.issue_get(row[0])
                if (existing["title"],existing["description"],existing["category"],existing["message_ids"])!=(title,description,category,mids):
                    raise MirrorError("idempotency_conflict","Idempotency key was already used with a different issue payload",409)
                return existing|{"idempotent_replay":True}
            now=time.monotonic();self.issue_calls=[t for t in self.issue_calls if t>now-60]
            if len(self.issue_calls)>=10:raise MirrorError("rate_limited","Local issue creation limit is 10 per minute",429)
            self.issue_calls.append(now);iid=secrets.token_hex(16);created=utcnow()
            db.execute("INSERT INTO issues VALUES(?,?,?,?,?,?,?)",(iid,idem,created,category,title,description,json.dumps(context)))
        return self.issue_get(iid)

    def issue_get(self,issue_id):
        if not isinstance(issue_id,str) or not re.fullmatch(r"[0-9a-f]{32}",issue_id):raise MirrorError("invalid_issue_id","Invalid local issue ID")
        with sqlite3.connect(self.issue_db) as db:
            row=db.execute("SELECT id,created,category,title,description,context FROM issues WHERE id=?",(issue_id,)).fetchone()
        if not row:raise MirrorError("not_found","Local issue not found",404)
        return {"id":row[0],"created":row[1],"category":row[2],"title":row[3],"description":row[4],**json.loads(row[5])}

    def issue_resolve(self,issue_id,resolution):
        """Operator-only (host CLI): mark a local issue resolved with its evidence."""
        if not isinstance(resolution,str) or not 1<=len(resolution.encode())<=4000:raise MirrorError("invalid_resolution","Resolution 1..4000 bytes")
        with self.mutex,sqlite3.connect(self.issue_db,timeout=5) as db:
            current=self.issue_get(issue_id)
            if current.get("status")!="open":raise MirrorError("issue_not_open","Local issue is already resolved",409)
            context={k:current[k] for k in ("message_ids","index_generation","local_only")}
            context.update(status="resolved",resolved_at=utcnow(),resolution=resolution)
            if len(encoded(current|context))>max(512,(self.limits["response_bytes"]-1024)//3):
                raise MirrorError("issue_response_limit","Resolution exceeds the readable issue budget; shorten it",413)
            changed=db.execute("UPDATE issues SET context=? WHERE id=? AND json_extract(context,'$.status')='open'",
                               (json.dumps(context),issue_id)).rowcount
            if not changed:raise MirrorError("issue_not_open","Local issue is already resolved",409)
        return self.issue_get(issue_id)

    def issue_list(self,limit=20,cursor=None):
        if isinstance(limit,bool) or not isinstance(limit,int) or not 1<=limit<=100:raise MirrorError("invalid_limit","limit must be 1..100")
        if cursor and not re.fullmatch(r"[0-9a-f]{32}",cursor):raise MirrorError("invalid_cursor","Invalid issue cursor")
        # IDs sort lexically and existing reports are immutable. New issues need a fresh list to appear.
        with sqlite3.connect(self.issue_db) as db:
            rows=db.execute("SELECT id,created,category,title,json_extract(context,'$.status') FROM issues WHERE id>? ORDER BY id LIMIT ?",(cursor or "",limit+1)).fetchall()
        page=rows[:limit]
        return {"items":[{"id":r[0],"created":r[1],"category":r[2],"title":r[3],"status":r[4] or "open"} for r in page],
                "has_more":len(rows)>limit,"next_cursor":page[-1][0] if len(rows)>limit else None,"local_only":True}

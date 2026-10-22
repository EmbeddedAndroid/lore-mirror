"""Read-only, evidence-qualified mail/code links at exact published snapshots."""
from __future__ import annotations

from contextlib import contextmanager
from .code_common import OID
from .errors import MirrorError


# One batch graph plus at most this many distinct unresolved candidates.
MAX_ANCESTRY_FALLBACKS = 64


class CodeLinkBackend:
    def __init__(self, cfg, code_backend, *, store=None):
        self.cfg = cfg
        self.code = code_backend
        self.enabled = bool(cfg.get("code", {}).get("links", {}).get("enabled", False))
        self._store = store

    @property
    def store(self):
        if not self.enabled:
            raise MirrorError("code_links_disabled", "Mail/code evidence links are disabled", 503)
        if self._store is None:
            from .code_links import LinksStore
            self._store = LinksStore(self.code.root)
        return self._store

    def generation(self):
        return self.store.generation()

    def status(self):
        if not self.enabled:
            return {"enabled":False,"status":"disabled"}
        try:
            return {"enabled":True,**self.store.status(),"application_status":"unknown"}
        except MirrorError as exc:
            return {"enabled":True,"status":"unavailable","error":exc.code,"application_status":"unknown"}

    @staticmethod
    def _variant(variant):
        if variant is not None and (not isinstance(variant, str) or not OID.fullmatch(variant)):
            raise MirrorError("invalid_variant", "Use an exact source-blob or raw SHA256 mail variant identity")

    @staticmethod
    def _items(result):
        if not isinstance(result,dict) or not isinstance(result.get("items"),list) or any(not isinstance(x,dict) for x in result["items"]):
            raise MirrorError("code_links_invalid", "Invalid evidence database result", 503)
        return result["items"]

    @contextmanager
    def read_guard(self):
        # The code guard only probes index.lock and releases it immediately.
        # SQLite readers use committed snapshots; a concurrent evidence update
        # invalidates the result rather than holding the writer's I/O lock.
        with self.code.read_guard():
            generation = self.generation()
            yield generation
            if self.generation() != generation:
                raise MirrorError("stale_links_generation", "Mail/code evidence changed during the query; retry", 409)

    def _reachability(self, tree, tip, commits):
        """Batch the graph, then check unresolved OIDs without a depth cutoff.

        Candidate roots make Git validate their existence as well. An incomplete
        target graph can prove a positive path, but never a negative answer.
        Capped exact fallbacks share the enclosing read guard's deadline/output
        budget; a missing object or exhausted budget is unknown, never false.
        """
        commits = list(dict.fromkeys(commits))
        if not commits:
            return {}, []
        try:
            limit = self.code.limits["scan_limit"]
            result = self.code._run(tree, ["rev-list", "--parents", f"--max-count={limit + 1}",
                                          *dict.fromkeys([tip, *commits]), "--"], allow_partial=True)
            lines = result.output.split(b"\n")
            if lines[-1]:
                if not result.partial_reason:
                    raise MirrorError("code_links_invalid", "Incomplete local ancestry record", 503)
                lines.pop()  # A bounded pipe may end in the middle of an OID.
            else:
                lines.pop()
            if len(lines) > limit + 1:
                raise MirrorError("code_links_invalid", "Ancestry walk exceeded its scan bound", 503)
            graph = {}
            for line in lines:
                fields = line.decode("ascii").split(" ")
                if (not fields or any(not OID.fullmatch(oid) for oid in fields)
                        or fields[0] in graph):
                    raise MirrorError("code_links_invalid", "Malformed local ancestry record", 503)
                graph[fields[0]] = fields[1:]
            reachable, pending, complete = set(), [tip], True
            while pending:
                oid = pending.pop()
                if oid in reachable:
                    continue
                reachable.add(oid)
                if oid not in graph:
                    complete = False
                else:
                    pending.extend(graph[oid])
            checked = {oid: "reachable" if oid in reachable else "not_reachable" if complete else "unknown"
                       for oid in commits}
            graph_reason = result.partial_reason or "ancestry_scan_limit"
        except (MirrorError, UnicodeError) as exc:
            if getattr(exc, "code", None) == "code_cleanup_timeout":
                raise
            checked = dict.fromkeys(commits, "unknown")
            graph_reason = getattr(exc, "code", "code_links_invalid")

        unresolved = [oid for oid in commits if checked[oid] == "unknown"]
        if not unresolved:
            return checked, []
        reasons = [graph_reason]
        # A process/output/deadline bound is not another graph-depth boundary.
        # Do not launch more work after exhausting the shared resource budget.
        if graph_reason.startswith("query_"):
            return checked, reasons
        for index, oid in enumerate(unresolved):
            if index >= MAX_ANCESTRY_FALLBACKS:
                reasons.append("ancestry_fallback_limit")
                break
            try:
                answer = self.code._run(tree, ["merge-base", "--is-ancestor", oid, tip],
                                        allow_partial=True, allow_one=True,
                                        accepted_returncodes=(0, 1))
            except MirrorError as exc:
                if exc.code == "code_cleanup_timeout":
                    raise
                reasons.append(exc.code)
                if exc.code.startswith("query_"):
                    break
                continue
            if answer.partial_reason:
                reasons.append(answer.partial_reason)
                break
            # Only Git's complete 0/1 answers establish ancestry. Unexpected
            # output/status (including errors) cannot establish a negative.
            if answer.returncode not in (0, 1) or answer.output:
                reasons.append("code_links_invalid")
                continue
            checked[oid] = "reachable" if answer.returncode == 0 else "not_reachable"
        return checked, list(dict.fromkeys(reasons)) if "unknown" in checked.values() else []

    def _annotate(self, result, tree, ref, generation):
        tip, snapshot = self.code._resolve(tree, ref)
        if not isinstance(result, dict) or not isinstance(result.get("items"), list):
            raise MirrorError("code_links_invalid", "Invalid evidence database result", 503)
        items = result["items"]
        if len(items) > self.code.limits["scan_limit"]:
            raise MirrorError("code_links_invalid", "Evidence result exceeds scan budget", 503)
        coverage=self.store.coverage(tree,snapshot["requested_ref"],tip)
        partial = bool(result.get("partial", False))
        reasons = []
        if not coverage.get("complete") or coverage.get("catalog_fingerprint") != snapshot.get("catalog_fingerprint"):
            partial=True
            reasons.append("snapshot_backfill_incomplete_or_stale")
        # Inventory provenance is retained; a cursor generation is not proof
        # that an old raw variant still belongs to today's mail corpus.
        partial=True
        reasons.append("current_mail_membership_not_verified")
        for original in items:
            if (not isinstance(original, dict) or original.get("tree") != tree
                    or not isinstance(original.get("commit"), str) or not OID.fullmatch(original["commit"])
                    or original.get("evidence_level") not in ("linked", "patch_equivalent", "candidate")):
                raise MirrorError("code_links_invalid", "Invalid evidence identity", 503)
            if original.get("catalog_fingerprint") != snapshot.get("catalog_fingerprint"):
                raise MirrorError("links_catalog_changed", "Evidence belongs to an older tree configuration; refresh the links database", 409)
        checked, ancestry_reasons = self._reachability(tree, tip, (item["commit"] for item in items))
        if ancestry_reasons:
            partial = True
            reasons.extend(reason for reason in ancestry_reasons if reason not in reasons)
        for original in items:
            original.update({"reachability": checked[original["commit"]], "target_ref": snapshot["requested_ref"],
                             "target_commit": tip, "effective_change": "unknown",
                             "application_status": "unknown", "current_mail_membership": "not_verified"})
        metadata = dict(result.get("metadata", {}))
        metadata.update({"tree": tree, "target_ref": snapshot["requested_ref"], "target_commit": tip,
                         "code_generation": snapshot["code_generation"], "links_generation": generation,
                         "reachability_at_snapshot": True, "partial_reasons": reasons,
                         "snapshot_coverage": coverage, "mail_snapshot_status": "historical_inventory_current_membership_not_verified",
                         "interpretation": "Citations are not application proof. Stable patch IDs are equivalence evidence; reachability does not prove the change remains effective or a series is fully applied.",
                         "negative_result": "No match in the scanned scope; not proof of non-application."})
        return {"items": items, "metadata": metadata, "partial": partial}

    def related_mail(self, tree, commit, ref=None):
        if not isinstance(commit, str) or not OID.fullmatch(commit):
            raise MirrorError("invalid_commit", "Use a full lowercase commit OID")
        with self.read_guard() as generation:
            self.code._resolve(tree, ref)
            result=self.store.related_mail(tree, commit, limit=min(1000,self.code.limits["scan_limit"]))
            if any(item.get("commit") != commit for item in self._items(result)):
                raise MirrorError("code_links_invalid", "Evidence commit does not match the request", 503)
            return self._annotate(result, tree, ref, generation)

    def related_commits(self, message_id, tree, ref=None, variant=None):
        self._variant(variant)
        with self.read_guard() as generation:
            self.code._resolve(tree, ref)
            result=self.store.related_commits(message_id, tree, variant=variant,limit=min(1000,self.code.limits["scan_limit"]))
            if any(item.get("message_id") != message_id or (variant is not None and variant not in
                   (item.get("source_blob"),item.get("raw_sha256"))) for item in self._items(result)):
                raise MirrorError("code_links_invalid", "Evidence mail identity does not match the request", 503)
            return self._annotate(result, tree, ref, generation)

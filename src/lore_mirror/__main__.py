from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
from .config import load_config
from .errors import MirrorError


def main(argv=None):
    parser=argparse.ArgumentParser(prog="lore-mirror")
    sub=parser.add_subparsers(dest="command",required=True)
    for name in ("serve","stdio","sync","code-sync","code-index","fixture-import","call"):
        p=sub.add_parser(name);p.add_argument("--config",required=True)
        if name=="serve":p.add_argument("--listen",default=None);p.add_argument("--port",type=int,default=None)
        if name=="code-index":p.add_argument("--preflight",action="store_true")
        if name=="sync":
            mode=p.add_mutually_exclusive_group();mode.add_argument("--once",action="store_true");mode.add_argument("--daemon",action="store_true")
        if name=="fixture-import":p.add_argument("path")
        if name=="call":p.add_argument("tool");p.add_argument("arguments",nargs="?",default="{}")
    args=parser.parse_args(argv)
    try:
        config=load_config(args.config)
        if args.command=="code-index":
            from .code_index import CodeIndexError, _enforced_memory_limit, index_code_once
            try:
                result=({"status":"ready", "preflight_only":True,
                         "enforced_memory":_enforced_memory_limit(config["code"]["index"]["memory_bytes"])}
                        if args.preflight else index_code_once(config))
            except CodeIndexError as exc:
                raise MirrorError("code_index_error", str(exc)[:160], 503) from exc
            print(json.dumps(result))
            return 0 if result.get("status") in ("ready", "disabled", "unchanged") else 2
        if args.command=="code-sync":
            from .code_sync import CodeSyncError,sync_code_once
            try:result=sync_code_once(config)
            except CodeSyncError as exc:
                print(json.dumps({"error":{"code":"code_sync_error","message":str(exc)}}),file=sys.stderr);return 1
            print(json.dumps(result))
            return 0 if result.get("status") in ("ready", "disabled") else 2
        if args.command=="sync":
            from .sync import run_sync
            result=run_sync(args.config,once=not args.daemon)
            if isinstance(result,dict):return 0 if result.get("status")=="ready" else 2
            return 0
        if args.command=="fixture-import":
            from .backend import FixtureBackend
            count=FixtureBackend(config).import_paths(args.path)
            (Path(config["data_dir"])/"index.lock").touch(exist_ok=True)
            print(json.dumps({"imported_files":count}));return 0
        if args.command=="call":
            from .service import MirrorService
            print(json.dumps(MirrorService(config).invoke(args.tool,json.loads(args.arguments)),ensure_ascii=False));return 0
        if args.command=="stdio":
            import anyio
            from .server import run_bounded_stdio
            from .service import MirrorService
            anyio.run(run_bounded_stdio,MirrorService(config));return 0
        import uvicorn
        from .server import create_app
        config["listen"]=args.listen or config.get("listen","127.0.0.1")
        config["port"]=args.port or config.get("port",8765)
        if config["listen"] not in ("127.0.0.1","localhost","::1"):
            print("WARNING: authentication is deferred. Non-loopback exposure requires a trusted private proxy/firewall; direct public access is unsafe.",file=sys.stderr)
        uvicorn.run(create_app(config),host=config["listen"],port=config["port"],log_level="warning")
        return 0
    except (MirrorError,ValueError) as exc:
        payload=exc.as_dict() if isinstance(exc,MirrorError) else {"error":{"code":"invalid_arguments","message":str(exc)}}
        print(json.dumps(payload),file=sys.stderr);return 1

if __name__=="__main__":raise SystemExit(main())

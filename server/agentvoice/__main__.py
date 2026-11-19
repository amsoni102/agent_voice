"""Command-line interface: `agentvoice serve|token|import-leads|import-dnc|stats|prompt`."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import timedelta
from pathlib import Path

from .config import load_settings
from .db import Database, utcnow


def _db(args: argparse.Namespace) -> tuple[Database, object]:
    settings = load_settings(args.env)
    return Database(settings.db_path), settings


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .api import create_app

    settings = load_settings(args.env)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    app = create_app(settings)
    token = app.state.admin_token
    print(f"\n  AgentVoice dashboard: http://localhost:{args.port or settings.port}/")
    if not settings.admin_token:
        print(f"  API token (generated, also in `agentvoice token`): {token}")
    if not settings.openai_configured:
        print("  WARNING: OPENAI_API_KEY is not set; calls and practice mode are disabled.")
    if not settings.twilio_configured:
        print("  WARNING: Twilio is not fully configured; phone calls are disabled (practice mode still works).")
    print()
    uvicorn.run(
        app,
        host=args.host or settings.host,
        port=args.port or settings.port,
        log_level=args.log_level,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
    return 0


def cmd_token(args: argparse.Namespace) -> int:
    from .api import resolve_admin_token

    db, settings = _db(args)
    print(resolve_admin_token(settings, db))  # type: ignore[arg-type]
    return 0


def cmd_import_leads(args: argparse.Namespace) -> int:
    from .importer import import_leads_csv

    db, _ = _db(args)
    if args.campaign is not None and db.get_campaign(args.campaign) is None:
        print(f"campaign {args.campaign} does not exist", file=sys.stderr)
        return 1
    text = Path(args.file).read_text(encoding="utf-8", errors="replace")
    result = import_leads_csv(db, text, args.campaign, default_consent=args.consent_confirmed)
    print(json.dumps(result, indent=2))
    return 0


def cmd_import_dnc(args: argparse.Namespace) -> int:
    from .importer import parse_phone_list

    db, _ = _db(args)
    phones = parse_phone_list(Path(args.file).read_text(encoding="utf-8", errors="replace"))
    changes = db.add_dnc_many(phones, source=args.source)
    print(f"parsed {len(phones)} numbers, {changes} database changes")
    return 0


def cmd_campaign(args: argparse.Namespace) -> int:
    db, _ = _db(args)
    if args.action == "list":
        for c in db.list_campaigns():
            print(f"{c['id']:>4}  {c['status']:<9} leads={c['lead_count']:<6} remaining={c['remaining']:<6} meetings={c['meetings']:<4} {c['name']}")
    elif args.action == "create":
        c = db.create_campaign(args.name or "New campaign")
        print(f"created campaign {c['id']}")
    elif args.action in ("start", "pause"):
        db.update_campaign(args.id, status="running" if args.action == "start" else "paused")
        print(f"campaign {args.id}: {args.action}")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    db, _ = _db(args)
    print(json.dumps(db.stats(utcnow() - timedelta(days=args.days)), indent=2))
    return 0


def cmd_prompt(args: argparse.Namespace) -> int:
    from .agent_settings import get_agent_settings
    from .playbook import build_instructions, get_playbook

    db, _ = _db(args)
    lead = db.get_lead(args.lead) if args.lead else None
    print(build_instructions(get_playbook(db), get_agent_settings(db), lead))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentvoice", description="AI voice sales agent")
    parser.add_argument("--env", default=None, help="path to .env file (default: ./.env)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="run the server, dialer and dashboard")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--log-level", default="info", choices=["debug", "info", "warning", "error"])
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("token", help="print the API token used by the dashboard and Android app")
    p.set_defaults(func=cmd_token)

    p = sub.add_parser("import-leads", help="import leads from a CSV file")
    p.add_argument("file")
    p.add_argument("--campaign", type=int)
    p.add_argument("--consent-confirmed", action="store_true", help="mark rows without a consent column as consented")
    p.set_defaults(func=cmd_import_leads)

    p = sub.add_parser("import-dnc", help="import do-not-call numbers (one per line)")
    p.add_argument("file")
    p.add_argument("--source", default="national_registry")
    p.set_defaults(func=cmd_import_dnc)

    p = sub.add_parser("campaign", help="list/create/start/pause campaigns")
    p.add_argument("action", choices=["list", "create", "start", "pause"])
    p.add_argument("--id", type=int)
    p.add_argument("--name")
    p.set_defaults(func=cmd_campaign)

    p = sub.add_parser("stats", help="show call statistics")
    p.add_argument("--days", type=int, default=7)
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("prompt", help="print the agent's full instructions")
    p.add_argument("--lead", type=int)
    p.set_defaults(func=cmd_prompt)

    args = parser.parse_args(argv)
    if args.command == "campaign" and args.action in ("start", "pause") and args.id is None:
        parser.error("--id is required")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

 #!/usr/bin/env python3
"""
Source library manager for TRA420_Modeling_2025.

Single source of truth: research/sources.yaml
Each entry:
  id: unique slug (string)
  title: str
  authors: str
  year: int
  url: str
  topics: list[str]
  verification_status: confirmed | unconfirmed | mismatch | not_found
  notes: str
  date_added: YYYY-MM-DD

Usage:
  python scripts/sources_db.py add --id <id> --title <title> --authors <authors> \
      --year <year> --url <url> --topics topic1,topic2 \
      --status confirmed --notes "..." [--db research/sources.yaml]

  python scripts/sources_db.py list [--topic TOPIC] [--status STATUS] [--db research/sources.yaml]

  python scripts/sources_db.py export --topic TOPIC --output PATH [--db research/sources.yaml]

  python scripts/sources_db.py topics [--db research/sources.yaml]

No hardcoded default for --db's *content* location beyond the path itself being
an explicit CLI argument (defaults to research/sources.yaml for convenience,
since this is repo infrastructure, not a modeling script under --config discipline).
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import yaml

VALID_STATUSES = {"confirmed", "unconfirmed", "mismatch", "not_found"}


def load_db(db_path: Path) -> list[dict]:
    if not db_path.exists():
        return []
    with db_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data or []


def save_db(db_path: Path, entries: list[dict]) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with db_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(entries, f, sort_keys=False, allow_unicode=True, width=100)


def cmd_add(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    entries = load_db(db_path)

    if any(e["id"] == args.id for e in entries):
        print(f"ERROR: id '{args.id}' already exists in {db_path}. IDs must be unique.", file=sys.stderr)
        return 1

    if args.status not in VALID_STATUSES:
        print(f"ERROR: --status must be one of {sorted(VALID_STATUSES)}, got '{args.status}'", file=sys.stderr)
        return 1

    topics = [t.strip() for t in args.topics.split(",") if t.strip()]
    if not topics:
        print("ERROR: --topics must contain at least one non-empty topic.", file=sys.stderr)
        return 1

    entry = {
        "id": args.id,
        "title": args.title,
        "authors": args.authors,
        "year": args.year,
        "url": args.url,
        "topics": topics,
        "verification_status": args.status,
        "notes": args.notes or "",
        "date_added": args.date_added or dt.date.today().isoformat(),
    }
    entries.append(entry)
    save_db(db_path, entries)
    print(f"Added '{args.id}' ({len(entries)} total entries in {db_path})")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    entries = load_db(db_path)

    if args.topic:
        entries = [e for e in entries if args.topic in e.get("topics", [])]
    if args.status:
        entries = [e for e in entries if e.get("verification_status") == args.status]

    if not entries:
        print("No matching entries.")
        return 0

    for e in entries:
        topics_str = ", ".join(e.get("topics", []))
        print(f"[{e['id']}] {e['title']} ({e.get('year', '?')}) — {topics_str} — {e.get('verification_status', '?')}")
    print(f"\n{len(entries)} entr{'y' if len(entries) == 1 else 'ies'} shown.")
    return 0


def cmd_topics(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    entries = load_db(db_path)
    all_topics: dict[str, int] = {}
    for e in entries:
        for t in e.get("topics", []):
            all_topics[t] = all_topics.get(t, 0) + 1
    if not all_topics:
        print("No topics found (database empty or missing).")
        return 0
    for t, count in sorted(all_topics.items(), key=lambda x: (-x[1], x[0])):
        print(f"{t}: {count}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    entries = load_db(db_path)
    filtered = [e for e in entries if args.topic in e.get("topics", [])]

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    lines = [f"# Sources — {args.topic}", ""]
    if not filtered:
        lines.append("_No sources tagged with this topic yet._")
    else:
        for e in sorted(filtered, key=lambda x: (x.get("year") or 0), reverse=True):
            lines.append(f"## {e['title']} ({e.get('year', '?')})")
            lines.append(f"- **Authors/Publisher:** {e.get('authors', '?')}")
            lines.append(f"- **URL:** {e.get('url', '?')}")
            lines.append(f"- **Verification status:** {e.get('verification_status', '?')}")
            other_topics = [t for t in e.get("topics", []) if t != args.topic]
            if other_topics:
                lines.append(f"- **Also tagged:** {', '.join(other_topics)}")
            if e.get("notes"):
                lines.append(f"- **Notes:** {e['notes']}")
            lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Exported {len(filtered)} entries to {out_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="TRA420 source library manager")
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="Add a new source")
    p_add.add_argument("--id", required=True)
    p_add.add_argument("--title", required=True)
    p_add.add_argument("--authors", required=True)
    p_add.add_argument("--year", required=True, type=int)
    p_add.add_argument("--url", required=True)
    p_add.add_argument("--topics", required=True, help="Comma-separated, e.g. air_pollution,electricity_demand")
    p_add.add_argument("--status", required=True, choices=sorted(VALID_STATUSES))
    p_add.add_argument("--notes", default="")
    p_add.add_argument("--date-added", dest="date_added", default=None, help="YYYY-MM-DD, defaults to today")
    p_add.add_argument("--db", default="research/sources.yaml")
    p_add.set_defaults(func=cmd_add)

    p_list = sub.add_parser("list", help="List sources, optionally filtered")
    p_list.add_argument("--topic", default=None)
    p_list.add_argument("--status", default=None, choices=sorted(VALID_STATUSES))
    p_list.add_argument("--db", default="research/sources.yaml")
    p_list.set_defaults(func=cmd_list)

    p_topics = sub.add_parser("topics", help="List all topics in use with counts")
    p_topics.add_argument("--db", default="research/sources.yaml")
    p_topics.set_defaults(func=cmd_topics)

    p_export = sub.add_parser("export", help="Export a topic's sources to a markdown file")
    p_export.add_argument("--topic", required=True)
    p_export.add_argument("--output", required=True)
    p_export.add_argument("--db", default="research/sources.yaml")
    p_export.set_defaults(func=cmd_export)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
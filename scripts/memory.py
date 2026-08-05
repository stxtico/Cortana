"""Memory inspector (PROMPTS.md A7) - built alongside the memory layer itself,
not after. Lists what's stored, shows which session it came from, deletes a
wrong entry, and safely edits config/profile.md. This is the tool for catching
memory drift before weeks of accumulation make it hard to untangle (CLAUDE.md).

    uv run scripts/memory.py list [--session ID] [--limit N] [--role user|assistant]
    uv run scripts/memory.py sessions
    uv run scripts/memory.py show ID
    uv run scripts/memory.py delete ID
    uv run scripts/memory.py profile
    uv run scripts/memory.py edit-profile
"""

import argparse
import difflib
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services.memory import store  # noqa: E402
from services.memory.profile import PROFILE_PATH, load_profile  # noqa: E402

CONFIG_PATH = ROOT / "config" / "cortana.toml"


def _load_memory_config() -> dict:
    with CONFIG_PATH.open("rb") as f:
        config = tomllib.load(f)
    return config["memory"]


def _connect():
    cfg = _load_memory_config()
    db_path = ROOT / cfg["db_path"]
    if not db_path.exists():
        raise SystemExit(f"No memory store yet at {db_path} - nothing's been recorded.")
    return store.get_connection(db_path, cfg["embedding_dim"])


def _truncate(text: str, width: int = 70) -> str:
    text = text.replace("\n", " ")
    return text if len(text) <= width else text[: width - 1] + "…"


def cmd_list(args: argparse.Namespace) -> None:
    conn = _connect()
    passages = store.list_all(conn, session_id=args.session, limit=args.limit)
    if args.role:
        passages = [p for p in passages if p.role == args.role]
    if not passages:
        print("(nothing stored)")
        return
    print(f"{'id':>5}  {'session':<24}  {'when':<20}  {'role':<9}  {'src':<7}  text")
    for p in passages:
        print(f"{p.id:>5}  {p.session_id:<24}  {p.timestamp[:19]:<20}  {p.role:<9}  {p.source:<7}  {_truncate(p.text)}")
    print(f"\n{len(passages)} entries shown.")


def cmd_sessions(args: argparse.Namespace) -> None:
    conn = _connect()
    sessions = store.list_sessions(conn)
    if not sessions:
        print("(no sessions yet)")
        return
    print(f"{'session_id':<24}  {'started':<20}  count")
    for session_id, started, count in sessions:
        print(f"{session_id:<24}  {started[:19]:<20}  {count}")


def cmd_show(args: argparse.Namespace) -> None:
    conn = _connect()
    passages = store.list_all(conn, limit=100000)
    match = next((p for p in passages if p.id == args.id), None)
    if match is None:
        raise SystemExit(f"No entry with id {args.id}")
    print(f"id:       {match.id}")
    print(f"session:  {match.session_id}")
    print(f"when:     {match.timestamp}")
    print(f"role:     {match.role}")
    print(f"source:   {match.source}")
    print(f"text:\n{match.text}")


def cmd_delete(args: argparse.Namespace) -> None:
    conn = _connect()
    passages = store.list_all(conn, limit=100000)
    match = next((p for p in passages if p.id == args.id), None)
    if match is None:
        raise SystemExit(f"No entry with id {args.id}")
    print(f"About to delete entry {match.id} (session {match.session_id}, {match.role}):")
    print(f"  {_truncate(match.text, 120)}")
    if not args.yes:
        confirm = input("Delete this entry? [y/N] ").strip().lower()
        if confirm != "y":
            print("Cancelled.")
            return
    ok = store.delete_passage(conn, args.id)
    print("Deleted." if ok else "Nothing deleted (already gone?).")


def cmd_profile(args: argparse.Namespace) -> None:
    text = load_profile()
    print(text if text.strip() else "(config/profile.md is empty)")


def cmd_edit_profile(args: argparse.Namespace) -> None:
    if not PROFILE_PATH.exists():
        raise SystemExit(f"{PROFILE_PATH} doesn't exist.")
    before = PROFILE_PATH.read_text(encoding="utf-8")
    backup_path = PROFILE_PATH.with_suffix(".md.bak")
    shutil.copy2(PROFILE_PATH, backup_path)

    editor = os.environ.get("EDITOR") or ("notepad" if os.name == "nt" else "nano")
    subprocess.run([editor, str(PROFILE_PATH)], check=False)

    after = PROFILE_PATH.read_text(encoding="utf-8")
    if after == before:
        print("No changes made.")
        backup_path.unlink(missing_ok=True)
        return

    print(f"Updated {PROFILE_PATH} (backup at {backup_path}). Diff:\n")
    diff = difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile="before", tofile="after",
    )
    sys.stdout.writelines(diff)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="list stored entries")
    p_list.add_argument("--session", default=None, help="filter to one session id")
    p_list.add_argument("--limit", type=int, default=50)
    p_list.add_argument("--role", choices=["user", "assistant"], default=None)
    p_list.set_defaults(func=cmd_list)

    p_sessions = sub.add_parser("sessions", help="list sessions with counts")
    p_sessions.set_defaults(func=cmd_sessions)

    p_show = sub.add_parser("show", help="show one entry in full")
    p_show.add_argument("id", type=int)
    p_show.set_defaults(func=cmd_show)

    p_delete = sub.add_parser("delete", help="delete one entry")
    p_delete.add_argument("id", type=int)
    p_delete.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p_delete.set_defaults(func=cmd_delete)

    p_profile = sub.add_parser("profile", help="print config/profile.md")
    p_profile.set_defaults(func=cmd_profile)

    p_edit = sub.add_parser("edit-profile", help="edit config/profile.md (backs up first, shows a diff after)")
    p_edit.set_defaults(func=cmd_edit_profile)

    args = parser.parse_args()
    args.func(args)
    store.close()


if __name__ == "__main__":
    main()

"""Memory inspector (PROMPTS.md A7) - built alongside the memory layer itself,
not after. Lists what's stored, shows which session it came from, edits or
deletes a wrong entry, and safely edits config/profile.md. This is the tool
for catching memory drift before weeks of accumulation make it hard to
untangle (CLAUDE.md).

    uv run scripts/memory.py list [--session ID] [--limit N] [--role user|assistant] [--json]
    uv run scripts/memory.py sessions [--json]
    uv run scripts/memory.py show ID
    uv run scripts/memory.py edit ID --text "corrected text" [--no-reembed] [--json]
    uv run scripts/memory.py delete ID [--yes] [--json]
    uv run scripts/memory.py profile
    uv run scripts/memory.py edit-profile

--json on list/sessions/edit/delete is for ui/'s memory inspector tab
(PROMPTS.md A12) - the Electron app shells out to this same CLI rather than
talking to the sqlite store directly, so there's exactly one implementation
of "what's in memory" instead of a second one reimplemented in JS. The
default text output is unchanged and still what a human runs by hand.
"""

import argparse
import asyncio
import dataclasses
import difflib
import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services.memory import embeddings  # noqa: E402
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
    if args.json:
        print(json.dumps([dataclasses.asdict(p) for p in passages]))
        return
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
    if args.json:
        print(json.dumps([
            {"session_id": sid, "started": started, "count": count} for sid, started, count in sessions
        ]))
        return
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
        if args.json:
            print(json.dumps({"ok": False, "error": f"No entry with id {args.id}"}))
            return
        raise SystemExit(f"No entry with id {args.id}")
    if not args.yes:
        # Never call input() here in --json mode - a spawned child process
        # (ui/'s memory tab) has no real stdin to answer it, and this project
        # already hit exactly that failure mode once (A10's ask_user/shell
        # EOFError, CLAUDE.md). The UI is expected to confirm in its own
        # dialog and always pass --yes; --json without --yes is refused
        # outright rather than risking a hang.
        if args.json:
            print(json.dumps({"ok": False, "error": "refusing to delete without --yes"}))
            return
        print(f"About to delete entry {match.id} (session {match.session_id}, {match.role}):")
        print(f"  {_truncate(match.text, 120)}")
        confirm = input("Delete this entry? [y/N] ").strip().lower()
        if confirm != "y":
            print("Cancelled.")
            return
    ok = store.delete_passage(conn, args.id)
    if args.json:
        print(json.dumps({"ok": ok}))
        return
    print(f"Deleted (logged to {store.MEMORY_LOG_PATH.relative_to(ROOT)})." if ok else "Nothing deleted (already gone?).")


def cmd_edit(args: argparse.Namespace) -> None:
    conn = _connect()
    passages = store.list_all(conn, limit=100000)
    match = next((p for p in passages if p.id == args.id), None)
    if match is None:
        if args.json:
            print(json.dumps({"ok": False, "error": f"No entry with id {args.id}"}))
            return
        raise SystemExit(f"No entry with id {args.id}")

    async def _embed_and_close(text: str) -> list[float]:
        # Embed and close in the same asyncio.run() lifecycle - embeddings.py's
        # httpx.AsyncClient is loop-bound (rule 7), so closing it from a
        # different asyncio.run() call than the one that created it would be
        # closing it from the wrong loop.
        vector = await embeddings.embed(text)
        await embeddings.aclose()
        return vector

    embedding = None if args.no_reembed else asyncio.run(_embed_and_close(args.text))
    ok = store.update_passage(conn, args.id, args.text, embedding)
    if args.json:
        print(json.dumps({"ok": ok, "reembedded": embedding is not None}))
        return
    if not ok:
        print("Nothing updated (already gone?).")
        return
    print(f"Updated entry {args.id}.")
    if embedding is not None:
        print("Re-embedded for retrieval.")
    else:
        print("Not re-embedded (--no-reembed) - retrieval will still match the old text.")


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
    p_list.add_argument("--json", action="store_true", help="print as a JSON array instead of a table")
    p_list.set_defaults(func=cmd_list)

    p_sessions = sub.add_parser("sessions", help="list sessions with counts")
    p_sessions.add_argument("--json", action="store_true", help="print as a JSON array instead of a table")
    p_sessions.set_defaults(func=cmd_sessions)

    p_show = sub.add_parser("show", help="show one entry in full")
    p_show.add_argument("id", type=int)
    p_show.set_defaults(func=cmd_show)

    p_delete = sub.add_parser("delete", help="delete one entry")
    p_delete.add_argument("id", type=int)
    p_delete.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p_delete.add_argument("--json", action="store_true", help="print {ok: bool} instead of a message")
    p_delete.set_defaults(func=cmd_delete)

    p_edit_entry = sub.add_parser("edit", help="correct a stored entry's text (re-embeds by default)")
    p_edit_entry.add_argument("id", type=int)
    p_edit_entry.add_argument("--text", required=True, help="replacement text")
    p_edit_entry.add_argument(
        "--no-reembed", action="store_true",
        help="skip re-embedding (faster, but retrieval will still match the old text)",
    )
    p_edit_entry.add_argument("--json", action="store_true", help="print {ok: bool} instead of a message")
    p_edit_entry.set_defaults(func=cmd_edit)

    p_profile = sub.add_parser("profile", help="print config/profile.md")
    p_profile.set_defaults(func=cmd_profile)

    p_edit = sub.add_parser("edit-profile", help="edit config/profile.md (backs up first, shows a diff after)")
    p_edit.set_defaults(func=cmd_edit_profile)

    args = parser.parse_args()
    args.func(args)
    store.close()


if __name__ == "__main__":
    main()

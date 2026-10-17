"""Collect local evidence; index and check public summaries. Python standard library only."""

import argparse
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
BEIJING = timezone(timedelta(hours=8))
PATTERNS = {
    "credential": re.compile(r"(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)"),
    "credential assignment": re.compile(r"(?i)(?:api[_-]?key|access[_-]?token|password|authorization|cookie)\s*[:=]\s*[\"']?\S{8,}"),
    "local path": re.compile(r"(?i)(?:[A-Z]:[\\/]|\\\\[a-z0-9_-]+\\|/(?:Users|home)/)"),
    "email": re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}"),
    "phone": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    "session id": re.compile(r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b", re.I),
}


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Source timestamp has no timezone")
    return parsed


def collect(home, day, until=None):
    start = datetime.combine(day, time(), BEIJING)
    end = datetime.combine((until or day) + timedelta(days=1), time(), BEIJING)
    if end <= start:
        raise ValueError("End date precedes start date")
    sessions = home / "sessions"
    if not sessions.is_dir():
        raise RuntimeError("Codex sessions directory unavailable; not an empty workday")
    records, seen, scanned = [], set(), 0
    for folder in (sessions, home / "archived_sessions"):
        if not folder.exists():
            continue
        for path in sorted(folder.rglob("*.jsonl")):
            # An old conversation can resume today; never filter by filename date.
            if path.stat().st_mtime < start.timestamp():
                continue
            scanned += 1
            session_id, project = path.stem, ""
            with path.open(encoding="utf-8") as source:
                for line_number, line in enumerate(source, 1):
                    # Skip large tool payloads, reasoning, images and unrelated events.
                    if not any(marker in line[:500] for marker in ('"session_meta"', '"response_item"', '"event_msg"')):
                        continue
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        raise RuntimeError(f"Incomplete/unreadable JSONL at {path.name}:{line_number}") from None
                    payload = item.get("payload", {})
                    if item.get("type") == "session_meta":
                        session_id = payload.get("id", session_id)
                        project = payload.get("cwd", "")
                        source_kind = json.dumps(payload.get("source", ""))
                        if "subagent" in source_kind.lower():
                            break
                        continue
                    if not item.get("timestamp"):
                        continue
                    at = timestamp(item["timestamp"])
                    if not start <= at < end:
                        continue
                    text, kind = "", ""
                    if item.get("type") == "event_msg" and payload.get("type") == "task_complete":
                        text, kind = payload.get("last_agent_message") or "", "reported_result"
                    elif item.get("type") == "response_item" and payload.get("type") == "message":
                        role = payload.get("role")
                        if role == "assistant" and (payload.get("phase") == "final_answer" or payload.get("channel") == "final"):
                            kind = "reported_result"
                        elif role == "user":
                            kind = "request_only"
                        if kind:
                            text = "\n".join(c.get("text", "") for c in payload.get("content", []) if c.get("type") in ("input_text", "output_text", "text"))
                    if kind == "request_only" and text.lstrip().startswith(("# AGENTS.md", "<environment_context>", "<external_codex_apps_open_page>", "<turn_aborted>")):
                        continue
                    # Desktop's final message and completion event can differ only
                    # by an internal citation suffix; treat them as one result.
                    text = re.sub(r"<oai-mem-citation>[\s\S]*?</oai-mem-citation>", "", text).strip()
                    if not text.strip():
                        continue
                    digest = hashlib.sha256((at.astimezone(BEIJING).date().isoformat() + session_id + kind + text.strip()).encode()).hexdigest()
                    if digest in seen:
                        continue
                    seen.add(digest)
                    records.append({"time": at.astimezone(BEIJING).isoformat(), "session": session_id, "project": project, "source": "codex", "kind": kind, "text": text})
    records.sort(key=lambda record: record["time"])
    return {"date": day.isoformat(), "timezone": "Asia/Shanghai", "scanned_files": scanned, "records": records}


def collect_workbuddy(home, day, until=None):
    start = datetime.combine(day, time(), BEIJING)
    end = datetime.combine((until or day) + timedelta(days=1), time(), BEIJING)
    if end <= start:
        raise ValueError("End date precedes start date")
    projects = home / "projects"
    if not projects.is_dir():
        raise RuntimeError("WorkBuddy projects directory unavailable; not an empty workday")
    requests, notes, seen, scanned = [], {}, set(), 0
    # Main conversations live directly inside each project; exclude subagents.
    for path in sorted(projects.glob("*/*.jsonl")):
        if path.stat().st_mtime < start.timestamp():
            continue
        scanned += 1
        current_request = ""
        with path.open(encoding="utf-8") as source:
            for line_number, line in enumerate(source, 1):
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    raise RuntimeError(f"Incomplete/unreadable WorkBuddy JSONL at {path.name}:{line_number}") from None
                if item.get("type") != "message":
                    continue
                role = item.get("role")
                if role == "user":
                    current_request = item.get("id", "")
                at = datetime.fromtimestamp(item["timestamp"] / 1000, timezone.utc).astimezone(BEIJING)
                if not start <= at < end:
                    continue
                status = item.get("status")
                if role != "user" and not (role == "assistant" and status in ("completed", "incomplete")):
                    continue
                text = "\n".join(c.get("text", "") for c in item.get("content", []) if c.get("type") in ("input_text", "output_text")).strip()
                if not text or text.lstrip().startswith(("<analysis>", "<think>")):
                    continue
                record = {"time": at.isoformat(), "session": item.get("sessionId") or path.stem, "project": item.get("cwd", ""), "source": "workbuddy", "kind": "request_only" if role == "user" else "assistant_note", "text": text}
                if role == "user":
                    key = (record["time"][:10], record["session"], text)
                    if key not in seen:
                        seen.add(key)
                        requests.append(record)
                else:
                    # completed describes a message, not task acceptance. Keep
                    # the last note per request/day, and retain incomplete notes.
                    request = item.get("providerData", {}).get("conversationRequestId") or current_request
                    key = (record["time"][:10], record["session"], request, status)
                    record["message_status"] = status
                    if key not in notes or record["time"] >= notes[key]["time"]:
                        notes[key] = record
    records = sorted(requests + list(notes.values()), key=lambda r: r["time"])
    return {"date": day.isoformat(), "timezone": "Asia/Shanghai", "scanned_files": scanned, "records": records}


def index(root):
    readme = root / "README.md"
    text = readme.read_text(encoding="utf-8")
    links = []
    for path in sorted((root / "daily").glob("*/*/*.md"), reverse=True):
        date.fromisoformat(path.stem)
        links.append(f"- [{path.stem}]({path.relative_to(root).as_posix()})")
    start, end = "<!-- daily-index:start -->", "<!-- daily-index:end -->"
    if text.count(start) != 1 or text.count(end) != 1:
        raise RuntimeError("README daily index markers missing or duplicated")
    before, rest = text.split(start)
    _, after = rest.split(end)
    readme.write_text(before + start + "\n" + "\n".join(links) + "\n" + end + after, encoding="utf-8", newline="\n")


def findings(text):
    return [name for name, pattern in PATTERNS.items() if pattern.search(text)]


def check(root):
    paths = [root / "README.md", *(root / "daily").glob("*/*/*.md"), *(root / "docs").rglob("*.md")]
    errors = []
    for path in paths:
        for issue in findings(path.read_text(encoding="utf-8")):
            errors.append(f"{path.relative_to(root)}: {issue}")
    if errors:
        raise RuntimeError("Public content check failed (values omitted):\n" + "\n".join(errors))
    print(f"Public content check passed: {len(paths)} files. Semantic privacy review is still required.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    collector = sub.add_parser("collect")
    collector.add_argument("--date", type=date.fromisoformat, required=True)
    collector.add_argument("--until", type=date.fromisoformat, help="Inclusive end date for historical backfill")
    collector.add_argument("--codex-home", type=Path, action="append", help="Repeat to merge multiple local homes")
    collector.add_argument("--workbuddy-home", type=Path, action="append", help="WorkBuddy home with projects; repeat for older homes")
    sub.add_parser("index")
    sub.add_parser("check")
    args = parser.parse_args()
    if args.command == "collect":
        homes = args.codex_home or [Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))]
        results = [collect(home, args.date, args.until) for home in homes]
        results.extend(collect_workbuddy(home, args.date, args.until) for home in (args.workbuddy_home or []))
        records, seen = {}, set()
        for result in results:
            for record in result["records"]:
                day = record["time"][:10]
                key = (day, record["source"], record["session"], record["kind"], record["text"])
                if key not in seen:
                    seen.add(key)
                    records.setdefault(day, []).append(record)
        day = args.date
        while day <= (args.until or args.date):
            key = day.isoformat()
            result = {"date": key, "timezone": "Asia/Shanghai", "scanned_files": sum(r["scanned_files"] for r in results), "records": sorted(records.get(key, []), key=lambda r: r["time"])}
            destination = ROOT / ".local" / f"{key}.json"
            destination.parent.mkdir(exist_ok=True)
            destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"Collected {len(result['records'])} records into .local/{key}.json (private; do not publish).")
            day += timedelta(days=1)
    elif args.command == "index":
        index(ROOT)
        print("Daily index updated.")
    else:
        check(ROOT)


if __name__ == "__main__":
    main()

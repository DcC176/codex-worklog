from datetime import date
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("worklog", Path(__file__).resolve().parents[1] / "scripts" / "worklog.py")
worklog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worklog)


class WorklogTests(unittest.TestCase):
    def test_workbuddy_groups_notes_by_request_day_and_status(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            project = home / "projects" / "example"
            project.mkdir(parents=True)
            def message(at, role, text, status=None, request="turn"):
                return {"id": text, "type": "message", "sessionId": "session", "timestamp": int(worklog.timestamp(at).timestamp() * 1000), "role": role, "status": status, "providerData": {"conversationRequestId": request}, "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}]}
            entries = [
                message("2026-10-13T15:59:59Z", "user", "before"),
                message("2026-10-13T16:00:00Z", "user", "request"),
                message("2026-10-14T01:00:00Z", "assistant", "progress", "completed"),
                {"type": "reasoning", "timestamp": 1791939600000, "content": "private reasoning"},
                message("2026-10-14T02:00:00Z", "assistant", "delivery reported", "completed"),
                message("2026-10-14T03:00:00Z", "assistant", "interrupted", "incomplete"),
                message("2026-10-14T03:30:00Z", "assistant", "<analysis>private thoughts</analysis>", "completed"),
                message("2026-10-14T16:00:00Z", "assistant", "next day", "completed"),
            ]
            payload = "\n".join(json.dumps(x) for x in entries)
            (project / "session.jsonl").write_text(payload, encoding="utf-8")
            (project / "session" / "subagents").mkdir(parents=True)
            (project / "session" / "subagents" / "child.jsonl").write_text(payload, encoding="utf-8")
            result = worklog.collect_workbuddy(home, date(2026, 10, 14), date(2026, 10, 15))
            self.assertEqual([r["text"] for r in result["records"]], ["request", "delivery reported", "interrupted", "next day"])
            self.assertEqual(result["scanned_files"], 1)
            self.assertEqual(result["records"][1]["kind"], "assistant_note")
            self.assertTrue(all(r["source"] == "workbuddy" for r in result["records"]))

    def test_workbuddy_missing_or_corrupt_source_is_not_an_empty_day(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with self.assertRaises(RuntimeError):
                worklog.collect_workbuddy(home, date(2026, 10, 14))
            project = home / "projects" / "example"
            project.mkdir(parents=True)
            (project / "bad.jsonl").write_text('{"type":"message",', encoding="utf-8")
            with self.assertRaises(RuntimeError):
                worklog.collect_workbuddy(home, date(2026, 10, 14))

    def test_cli_merges_homes_and_writes_each_date_without_duplicate_results(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            homes = [root / "current", root / "old"]
            for home in homes:
                (home / "sessions").mkdir(parents=True)
                entries = [{"type": "session_meta", "payload": {"id": "same-session"}}]
                for at in ("2026-10-13T16:00:00Z", "2026-10-14T16:00:00Z"):
                    entries.append({"timestamp": at, "type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "same result"}})
                if home == homes[1]:
                    entries.append({"timestamp": "2026-10-14T10:00:00Z", "type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "only in old home"}})
                (home / "sessions" / "history.jsonl").write_text("\n".join(json.dumps(x) for x in entries), encoding="utf-8")
            argv = ["worklog", "collect", "--date", "2026-10-14", "--until", "2026-10-15", "--codex-home", str(homes[0]), "--codex-home", str(homes[1])]
            for name in ("workbuddy", "workbuddy-old"):
                buddy = root / name / "projects" / "example"
                buddy.mkdir(parents=True)
                note = {"type": "message", "sessionId": "same-session", "timestamp": int(worklog.timestamp("2026-10-14T10:30:00Z").timestamp() * 1000), "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": "same result"}]}
                (buddy / "session.jsonl").write_text(json.dumps(note), encoding="utf-8")
                argv.extend(["--workbuddy-home", str(root / name)])
            with patch.object(worklog, "ROOT", root), patch("sys.argv", argv), redirect_stdout(io.StringIO()):
                worklog.main()
            first = json.loads((root / ".local" / "2026-10-14.json").read_text(encoding="utf-8"))
            second = json.loads((root / ".local" / "2026-10-15.json").read_text(encoding="utf-8"))
            self.assertEqual([r["text"] for r in first["records"]], ["same result", "only in old home", "same result"])
            self.assertEqual([r["source"] for r in first["records"]], ["codex", "codex", "workbuddy"])
            self.assertEqual([r["text"] for r in second["records"]], ["same result"])

    def test_range_keeps_same_result_on_distinct_days(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "sessions").mkdir()
            entries = [{"type": "session_meta", "payload": {"id": "test"}}]
            for at in ("2026-10-13T16:00:00Z", "2026-10-14T16:00:00Z", "2026-10-15T16:00:00Z"):
                entries.append({"timestamp": at, "type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "same result"}})
            (home / "sessions" / "old.jsonl").write_text("\n".join(json.dumps(x) for x in entries), encoding="utf-8")
            result = worklog.collect(home, date(2026, 10, 14), date(2026, 10, 15))
            self.assertEqual([r["time"][:10] for r in result["records"]], ["2026-10-14", "2026-10-15"])

    def test_beijing_boundary_resumed_thread_and_duplicate_results(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            sessions = home / "sessions" / "2020" / "01" / "01"
            sessions.mkdir(parents=True)
            entries = [{"type": "session_meta", "payload": {"id": "test", "cwd": "project"}}]
            for at, text in (("2026-10-13T15:59:59Z", "before"), ("2026-10-13T16:00:00Z", "included"), ("2026-10-14T16:00:00Z", "after")):
                entries.append({"timestamp": at, "type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": text}})
            entries.append({"timestamp": "2026-10-14T10:00:00Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final_answer", "content": [{"type": "output_text", "text": "included\n<oai-mem-citation>internal references</oai-mem-citation>"}]}})
            for filename in ("old-thread.jsonl", "duplicate.jsonl"):
                (sessions / filename).write_text("\n".join(json.dumps(x) for x in entries), encoding="utf-8")
            result = worklog.collect(home, date(2026, 10, 14))
            self.assertEqual([r["text"] for r in result["records"]], ["included"])
            self.assertEqual(result["scanned_files"], 2)

    def test_subagents_and_tool_output_are_not_daily_results(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "sessions").mkdir()
            items = [
                {"type": "session_meta", "payload": {"source": {"subagent": "worker"}}},
                {"timestamp": "2026-10-14T10:00:00Z", "type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "duplicate work"}},
            ]
            (home / "sessions" / "child.jsonl").write_text("\n".join(json.dumps(x) for x in items), encoding="utf-8")
            self.assertEqual(worklog.collect(home, date(2026, 10, 14))["records"], [])

    def test_unavailable_or_corrupt_sources_fail_instead_of_empty_day(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with self.assertRaises(RuntimeError):
                worklog.collect(home, date(2026, 10, 14))
            (home / "sessions").mkdir()
            (home / "sessions" / "broken.jsonl").write_text('{"type":"response_item",', encoding="utf-8")
            with self.assertRaises(RuntimeError):
                worklog.collect(home, date(2026, 10, 14))

    def test_sensitive_patterns_are_blocked_without_echoing_values(self):
        for sample in ("ghp_" + "a" * 30, "C:/Users/private/file", "name@example.com", "13812345678", "00000000-0000-0000-0000-000000000000"):
            self.assertTrue(worklog.findings(sample))
        self.assertEqual(worklog.findings("完成日志整理，实际运行验收尚未进行。"), [])

    def test_index_rebuild_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "README.md").write_text("# Log\n<!-- daily-index:start -->\n<!-- daily-index:end -->\n", encoding="utf-8")
            day = root / "daily" / "2026" / "10"
            day.mkdir(parents=True)
            (day / "2026-10-14.md").write_text("summary", encoding="utf-8")
            worklog.index(root)
            once = (root / "README.md").read_bytes()
            worklog.index(root)
            self.assertEqual(once, (root / "README.md").read_bytes())
            self.assertIn(b"daily/2026/10/2026-10-14.md", once)


if __name__ == "__main__":
    unittest.main()

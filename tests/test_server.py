import tempfile
import time
import unittest
from io import StringIO
from pathlib import Path
from contextlib import redirect_stdout

import server


class FakeClient:
    def __init__(self):
        self.messages = []

    def send_message(self, request):
        self.messages.append(request["content"])
        return {"result": "success"}


class FakeChild:
    def __init__(self, chunks):
        self.chunks = list(chunks)

    def read_nonblocking(self, size, timeout):
        if not self.chunks:
            raise server.pexpect.EOF("done")
        return self.chunks.pop(0)


def stream_message(content, topic="test"):
    return {
        "type": "stream",
        "stream_id": 1,
        "topic": topic,
        "content": content,
        "sender_email": "user@example.com",
        "display_recipient": "test-stream",
    }


def wait_until(predicate, timeout=1.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class AttachSessionTests(unittest.TestCase):
    def test_attach_command_binds_current_conversation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_store = server.SESSION_STORE
            try:
                server.SESSION_STORE = server.ConversationSessionStore(Path(tmpdir) / "sessions.json")
                client = FakeClient()

                server.process_message(client, stream_message("/attach 019e-existing-session"))

                key = "stream:1:test"
                self.assertEqual(server.SESSION_STORE.get(key), "019e-existing-session")
                self.assertTrue(server.SESSION_STORE.is_attached(key))
                self.assertIn("已绑定到 Codex session", client.messages[-1])
            finally:
                server.SESSION_STORE = original_store

    def test_attached_invalid_session_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_store = server.SESSION_STORE
            original_run_codex = server.run_codex
            calls = []
            try:
                server.SESSION_STORE = server.ConversationSessionStore(Path(tmpdir) / "sessions.json")
                key = "stream:1:test"
                server.SESSION_STORE.set(key, "019e-existing-session", attached=True)

                def fake_run_codex(prompt, session_id=None, reasoning_effort=None, progress_callback=None):
                    calls.append((prompt, session_id))
                    return session_id, "session not found"

                server.run_codex = fake_run_codex
                client = FakeClient()

                server.process_message(client, stream_message("continue"))

                self.assertEqual(len(calls), 1)
                self.assertEqual(server.SESSION_STORE.get(key), "019e-existing-session")
                self.assertTrue(server.SESSION_STORE.is_attached(key))
                self.assertIn("attached Codex session 不可恢复", client.messages[-1])
            finally:
                server.SESSION_STORE = original_store
                server.run_codex = original_run_codex


class DispatchModeTests(unittest.TestCase):
    def test_parse_dispatch_directive_supports_english_and_chinese(self):
        self.assertEqual(server.parse_dispatch_directive("/guide review this"), ("guide", "review this"))
        self.assertEqual(server.parse_dispatch_directive("引导: review this"), ("guide", "review this"))
        self.assertEqual(server.parse_dispatch_directive("/queue review this"), ("queue", "review this"))
        self.assertEqual(server.parse_dispatch_directive("排队：review this"), ("queue", "review this"))
        self.assertEqual(server.parse_dispatch_directive("review this"), ("normal", "review this"))

    def test_guide_prompt_requests_subagent_review(self):
        prompt = server.build_codex_prompt("review this", [], dispatch_mode="guide")

        self.assertIn("Zulip dispatch mode: guide.", prompt)
        self.assertIn("subagents", prompt)
        self.assertIn("review this", prompt)

    def test_queue_command_acknowledges_then_runs_with_queue_prompt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_store = server.SESSION_STORE
            original_run_codex = server.run_codex
            calls = []
            try:
                server.SESSION_STORE = server.ConversationSessionStore(Path(tmpdir) / "sessions.json")

                def fake_run_codex(prompt, session_id=None, reasoning_effort=None, progress_callback=None):
                    calls.append((prompt, session_id, reasoning_effort))
                    return "019e-new-session", "queued result"

                server.run_codex = fake_run_codex
                client = FakeClient()

                server.process_message(client, stream_message("/queue review this"))

                self.assertEqual(len(calls), 1)
                self.assertIn("Zulip dispatch mode: queue.", calls[0][0])
                self.assertIn("subagents", calls[0][0])
                self.assertIn("review this", calls[0][0])
                self.assertIn("已加入当前 Zulip 对话队列", client.messages[0])
                self.assertEqual(client.messages[-1], "queued result")
            finally:
                server.SESSION_STORE = original_store
                server.run_codex = original_run_codex


class ReasoningEffortTests(unittest.TestCase):
    def test_effort_command_applies_to_next_turn(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_store = server.SESSION_STORE
            original_run_codex = server.run_codex
            calls = []
            try:
                server.SESSION_STORE = server.ConversationSessionStore(Path(tmpdir) / "sessions.json")

                def fake_run_codex(prompt, session_id=None, reasoning_effort=None, progress_callback=None):
                    calls.append((prompt, session_id, reasoning_effort))
                    return "019e-new-session", "done"

                server.run_codex = fake_run_codex
                client = FakeClient()

                server.process_message(client, stream_message("/effort xhigh"))
                server.process_message(client, stream_message("continue"))

                self.assertEqual(server.SESSION_STORE.get_effort("stream:1:test"), "xhigh")
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0][2], "xhigh")
                self.assertIn("reasoning effort 已设置为 `xhigh`", client.messages[0])
                self.assertEqual(client.messages[-1], "done")
            finally:
                server.SESSION_STORE = original_store
                server.run_codex = original_run_codex

    def test_effort_clear_removes_conversation_override(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_store = server.SESSION_STORE
            original_run_codex = server.run_codex
            calls = []
            try:
                server.SESSION_STORE = server.ConversationSessionStore(Path(tmpdir) / "sessions.json")

                def fake_run_codex(prompt, session_id=None, reasoning_effort=None, progress_callback=None):
                    calls.append((prompt, session_id, reasoning_effort))
                    return "019e-new-session", "done"

                server.run_codex = fake_run_codex
                client = FakeClient()

                server.process_message(client, stream_message("/effort xhigh"))
                server.process_message(client, stream_message("/effort clear"))
                server.process_message(client, stream_message("continue"))

                self.assertIsNone(server.SESSION_STORE.get_effort("stream:1:test"))
                self.assertEqual(len(calls), 1)
                self.assertIsNone(calls[0][2])
                self.assertIn("reasoning effort 覆盖已清除", client.messages[1])
            finally:
                server.SESSION_STORE = original_store
                server.run_codex = original_run_codex

    def test_reasoning_effort_arg_is_added_to_exec_and_resume(self):
        _codex_bin, exec_args, _timeout, _workdir = server.build_codex_exec_args(
            "prompt",
            "/tmp/out",
            reasoning_effort="high",
        )
        _codex_bin, resume_args, _timeout, _workdir = server.build_codex_resume_args(
            "019e-existing-session",
            "prompt",
            "/tmp/out",
            reasoning_effort="xhigh",
        )

        self.assertIn("model_reasoning_effort=\"high\"", exec_args)
        self.assertIn("model_reasoning_effort=\"xhigh\"", resume_args)

    def test_background_jobs_for_same_conversation_run_fifo(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_store = server.SESSION_STORE
            original_run_codex = server.run_codex
            original_queues = server.THREAD_WORK_QUEUES
            calls = []
            try:
                server.SESSION_STORE = server.ConversationSessionStore(Path(tmpdir) / "sessions.json")
                server.THREAD_WORK_QUEUES = {}

                def fake_run_codex(prompt, session_id=None, reasoning_effort=None, progress_callback=None):
                    calls.append(prompt)
                    return "019e-new-session", f"done {len(calls)}"

                server.run_codex = fake_run_codex
                client = FakeClient()

                server.start_background_job(client, stream_message("/queue first", topic="fifo"))
                server.start_background_job(client, stream_message("/queue second", topic="fifo"))

                self.assertTrue(wait_until(lambda: len(calls) == 2))
                self.assertIn("first", calls[0])
                self.assertIn("second", calls[1])
            finally:
                server.SESSION_STORE = original_store
                server.run_codex = original_run_codex
                server.THREAD_WORK_QUEUES = original_queues

    def test_plain_message_gets_ack_when_implicitly_queued(self):
        original_queues = server.THREAD_WORK_QUEUES
        try:
            server.THREAD_WORK_QUEUES = {
                "stream:1:fifo": {
                    "messages": server.deque(),
                    "active": True,
                }
            }
            client = FakeClient()

            server.start_background_job(client, stream_message("second", topic="fifo"))

            self.assertIn("已有任务在运行", client.messages[-1])
            self.assertEqual(len(server.THREAD_WORK_QUEUES["stream:1:fifo"]["messages"]), 1)
        finally:
            server.THREAD_WORK_QUEUES = original_queues


class ProgressTests(unittest.TestCase):
    def test_progress_command_controls_current_conversation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_store = server.SESSION_STORE
            try:
                server.SESSION_STORE = server.ConversationSessionStore(Path(tmpdir) / "sessions.json")
                client = FakeClient()

                server.process_message(client, stream_message("/progress on"))
                server.process_message(client, stream_message("/progress status"))
                server.process_message(client, stream_message("/progress clear"))

                self.assertIn("progress 已设置为 `text`", client.messages[0])
                self.assertIn("当前 progress: `text`", client.messages[1])
                self.assertIsNone(server.SESSION_STORE.get_progress("stream:1:test"))
            finally:
                server.SESSION_STORE = original_store

    def test_progress_callback_sends_agent_message_text(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_store = server.SESSION_STORE
            original_run_codex = server.run_codex
            try:
                server.SESSION_STORE = server.ConversationSessionStore(Path(tmpdir) / "sessions.json")

                def fake_run_codex(prompt, session_id=None, reasoning_effort=None, progress_callback=None):
                    self.assertIsNotNone(progress_callback)
                    progress_callback({"type": "thread.started", "thread_id": "019e-progress"})
                    progress_callback(
                        {
                            "type": "item.completed",
                            "item": {
                                "type": "agent_message",
                                "text": "我已经定位到问题，下一步会做最小修复。",
                            },
                        }
                    )
                    return "019e-progress", "done"

                server.run_codex = fake_run_codex
                client = FakeClient()

                server.process_message(client, stream_message("/progress on"))
                server.process_message(client, stream_message("continue"))

                self.assertTrue(any("进展：\n我已经定位到问题" in message for message in client.messages))
                self.assertFalse(any("Codex 会话已启动" in message for message in client.messages))
                self.assertFalse(any("019e-progress" in message for message in client.messages))
                self.assertEqual(client.messages[-1], "done")
            finally:
                server.SESSION_STORE = original_store
                server.run_codex = original_run_codex

    def test_final_reply_dedupes_last_progress_text(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_store = server.SESSION_STORE
            original_run_codex = server.run_codex
            try:
                server.SESSION_STORE = server.ConversationSessionStore(Path(tmpdir) / "sessions.json")

                def fake_run_codex(prompt, session_id=None, reasoning_effort=None, progress_callback=None):
                    self.assertIsNotNone(progress_callback)
                    progress_callback(
                        {
                            "type": "item.completed",
                            "item": {
                                "type": "agent_message",
                                "text": "same final text",
                            },
                        }
                    )
                    return "019e-progress", "same final text"

                server.run_codex = fake_run_codex
                client = FakeClient()

                server.process_message(client, stream_message("/progress on"))
                server.process_message(client, stream_message("continue"))

                self.assertEqual(sum(message == "进展：\nsame final text" for message in client.messages), 1)
                self.assertEqual(client.messages[-1], "Codex 已完成；最终内容与上一条进展相同。")
            finally:
                server.SESSION_STORE = original_store
                server.run_codex = original_run_codex

    def test_progress_steps_mode_sends_stage_events(self):
        client = FakeClient()
        callback = server.build_progress_callback(client, stream_message("continue"), progress_mode="steps")

        callback({"type": "thread.started", "thread_id": "019e-progress"})

        self.assertTrue(any("进度：Codex 会话已启动" in message for message in client.messages))
        self.assertFalse(any("019e-progress" in message for message in client.messages))

    def test_progress_summaries_do_not_echo_raw_command_or_tool(self):
        command_summary = server.summarize_progress_event(
            {
                "type": "item.started",
                "item": {
                    "type": "command_execution",
                    "command": "curl -H 'Authorization: Bearer sk-secret' https://example.test",
                },
            }
        )
        tool_summary = server.summarize_progress_event(
            {
                "type": "item.started",
                "item": {
                    "type": "tool_call",
                    "name": "secret_tool_name",
                },
            }
        )
        completed_summary = server.summarize_progress_event(
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "echo still-secret",
                },
            }
        )

        self.assertEqual(command_summary, "Codex 正在运行命令。")
        self.assertEqual(tool_summary, "Codex 正在调用工具。")
        self.assertEqual(completed_summary, "Codex 完成命令执行。")
        self.assertNotIn("sk-secret", command_summary)
        self.assertNotIn("secret_tool_name", tool_summary)
        self.assertNotIn("still-secret", completed_summary)

    def test_progress_text_redacts_session_ids_and_tokens(self):
        summary = server.summarize_progress_text_event(
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": (
                        "继续 session 019e009c-49ad-7183-bda6-01f81861952b, "
                        "Authorization: Bearer sk-secretvalue, OPENAI_API_KEY=sk-anothersecret"
                    ),
                },
            }
        )

        self.assertIn("<session-id>", summary)
        self.assertIn("Authorization: Bearer <redacted>", summary)
        self.assertIn("OPENAI_API_KEY=<redacted>", summary)
        self.assertNotIn("019e009c-49ad-7183-bda6-01f81861952b", summary)
        self.assertNotIn("sk-secretvalue", summary)
        self.assertNotIn("sk-anothersecret", summary)

    def test_logs_do_not_emit_full_prompt_or_session_id(self):
        buffer = StringIO()
        with redirect_stdout(buffer):
            server.log_session_event(
                "completed",
                "stream:1:test",
                existing_session_id="019e009c-49ad-7183-bda6-01f81861952b",
                next_session_id="019e012d-3c18-7542-8bb3-3512f52d1da7",
            )
            server.log_codex_command(
                "resume",
                "/tmp/work",
                [
                    "exec",
                    "resume",
                    "019e009c-49ad-7183-bda6-01f81861952b",
                    "prompt with OPENAI_API_KEY=sk-secretvalue",
                ],
            )

        output = buffer.getvalue()
        self.assertIn("019e009c...952b", output)
        self.assertIn("<prompt chars=", output)
        self.assertNotIn("019e009c-49ad-7183-bda6-01f81861952b", output)
        self.assertNotIn("019e012d-3c18-7542-8bb3-3512f52d1da7", output)
        self.assertNotIn("OPENAI_API_KEY=sk-secretvalue", output)

    def test_progress_send_failure_does_not_raise_again(self):
        class FailingClient(FakeClient):
            def send_message(self, request):
                raise RuntimeError("zulip unavailable")

        callback = server.build_progress_callback(FailingClient(), stream_message("continue"))
        callback(
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": "intermediate update",
                },
            }
        )
        callback(
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": "second update",
                },
            }
        )

    def test_progress_override_survives_auto_rebuild(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_store = server.SESSION_STORE
            original_run_codex = server.run_codex
            calls = []
            try:
                server.SESSION_STORE = server.ConversationSessionStore(Path(tmpdir) / "sessions.json")
                thread_key = "stream:1:test"
                server.SESSION_STORE.set(thread_key, "019e-existing-session")
                server.SESSION_STORE.set_progress(thread_key, "text")

                def fake_run_codex(prompt, session_id=None, reasoning_effort=None, progress_callback=None):
                    calls.append(session_id)
                    if session_id:
                        return session_id, "session not found"
                    return "019e-rebuilt-session", "rebuilt"

                server.run_codex = fake_run_codex
                client = FakeClient()

                server.process_message(client, stream_message("continue"))

                self.assertEqual(calls, ["019e-existing-session", None])
                self.assertEqual(server.SESSION_STORE.get_progress(thread_key), "text")
                self.assertEqual(server.SESSION_STORE.get(thread_key), "019e-rebuilt-session")
            finally:
                server.SESSION_STORE = original_store
                server.run_codex = original_run_codex

    def test_stream_codex_output_handles_split_json_lines(self):
        event = (
            '{"type":"item.completed","item":{"type":"agent_message",'
            '"text":"split hello"}}\n'
        )
        seen = []
        child = FakeChild([event[:20], event[20:45], event[45:]])

        output = server.stream_codex_output(
            child,
            timeout=1,
            mode="test",
            progress_callback=seen.append,
        )

        self.assertEqual(output, event)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["item"]["text"], "split hello")

    def test_effort_update_preserves_progress_override(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_store = server.SESSION_STORE
            try:
                server.SESSION_STORE = server.ConversationSessionStore(Path(tmpdir) / "sessions.json")
                client = FakeClient()

                server.process_message(client, stream_message("/progress on"))
                server.process_message(client, stream_message("/effort high"))

                thread_key = "stream:1:test"
                self.assertEqual(server.SESSION_STORE.get_effort(thread_key), "high")
                self.assertEqual(server.SESSION_STORE.get_progress(thread_key), "text")
            finally:
                server.SESSION_STORE = original_store


if __name__ == "__main__":
    unittest.main()

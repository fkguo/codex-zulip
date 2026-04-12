import html
import json
import os
import re
import shlex
import tempfile
import threading
import time
from pathlib import Path

import pexpect
import zulip


def load_env():
    env = dict(os.environ)
    dotenv_path = Path(__file__).with_name(".env")
    if dotenv_path.exists():
        for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env.setdefault(key.strip(), value.strip().strip("'").strip('"'))
    return env


ENV = load_env()
SESSION_STORE_PATH = Path(
    ENV.get("CODEX_ZULIP_SESSION_STORE", Path(__file__).with_name(".codex-zulip-sessions.json"))
)
THREAD_LOCKS = {}
THREAD_LOCKS_GUARD = threading.Lock()


class ConversationSessionStore:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._sessions = self._load()

    def _load(self):
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}

        normalized = {}
        for key, value in data.items():
            if isinstance(value, str):
                normalized[key] = {"session_id": value, "updated_at": 0}
            elif isinstance(value, dict) and isinstance(value.get("session_id"), str):
                normalized[key] = {
                    "session_id": value["session_id"],
                    "updated_at": value.get("updated_at", 0),
                }
        return normalized

    def _save_locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            prefix=f"{self.path.name}.",
            suffix=".tmp",
            dir=self.path.parent,
            encoding="utf-8",
            delete=False,
        ) as tmp:
            json.dump(self._sessions, tmp, ensure_ascii=True, indent=2, sort_keys=True)
            tmp.write("\n")
            tmp_path = Path(tmp.name)
        tmp_path.replace(self.path)

    def get(self, key):
        with self._lock:
            entry = self._sessions.get(key)
            if not entry:
                return None
            return entry.get("session_id")

    def set(self, key, session_id):
        with self._lock:
            self._sessions[key] = {
                "session_id": session_id,
                "updated_at": int(time.time()),
            }
            self._save_locked()

    def delete(self, key):
        with self._lock:
            if key in self._sessions:
                del self._sessions[key]
                self._save_locked()

    def touch(self, key):
        with self._lock:
            entry = self._sessions.get(key)
            if not entry:
                return
            entry["updated_at"] = int(time.time())
            self._save_locked()


SESSION_STORE = ConversationSessionStore(SESSION_STORE_PATH)


def chunk_text(text, max_length=3500):
    normalized = (text or "").strip()
    if not normalized:
        return ["Codex returned an empty response."]

    chunks = []
    start = 0
    while start < len(normalized):
        chunks.append(normalized[start : start + max_length])
        start += max_length
    return chunks


def strip_html_to_text(content):
    normalized = (content or "").strip()
    if not normalized:
        return ""

    normalized = normalized.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    normalized = normalized.replace("</p>", "\n").replace("</li>", "\n").replace("<li>", "- ")
    normalized = re.sub(r"<[^>]+>", "", normalized)
    return html.unescape(normalized).strip()


def is_reset_command(text):
    normalized = (text or "").strip().lower()
    return normalized in {"/reset", "reset", "reset session", "/reset-session"}


def is_fresh_command(text):
    normalized = (text or "").strip()
    return normalized.startswith("/fresh ") or normalized == "/fresh"


def is_session_command(text):
    normalized = (text or "").strip().lower()
    return normalized in {"/session", "session", "session id"}


def strip_fresh_command(text):
    normalized = (text or "").strip()
    if normalized == "/fresh":
        return ""
    if normalized.startswith("/fresh "):
        return normalized[len("/fresh ") :].strip()
    return normalized


def get_codex_settings():
    codex_bin = ENV.get("CODEX_BIN", "codex")
    model = ENV.get("OPENAI_MODEL", "gpt-5.4")
    workdir = ENV.get("CODEX_WORKDIR", str(Path.cwd()))
    timeout = int(ENV.get("CODEX_TIMEOUT_SECONDS", "900"))
    sandbox = ENV.get("CODEX_SANDBOX", "workspace-write")
    extra_args = ENV.get("CODEX_EXTRA_ARGS", "").strip()
    full_auto = ENV.get("CODEX_FULL_AUTO", "0") == "1"
    return codex_bin, model, workdir, timeout, sandbox, extra_args, full_auto


def build_codex_exec_args(prompt, output_file):
    codex_bin, model, workdir, timeout, sandbox, extra_args, full_auto = get_codex_settings()
    args = [
        "exec",
        "--model",
        model,
        "--color",
        "never",
        "--skip-git-repo-check",
        "--output-last-message",
        output_file,
        "--json",
    ]
    if sandbox:
        args.extend(["--sandbox", sandbox])
    if full_auto:
        args.append("--full-auto")
    if extra_args:
        args.extend(shlex.split(extra_args))
    args.append(prompt)
    return codex_bin, args, timeout, workdir


def build_codex_resume_args(session_id, prompt, output_file):
    codex_bin, model, workdir, timeout, _sandbox, _extra_args, full_auto = get_codex_settings()
    args = [
        "exec",
        "resume",
        "--model",
        model,
        "--skip-git-repo-check",
        "--output-last-message",
        output_file,
        "--json",
    ]
    if full_auto:
        args.append("--full-auto")
    args.extend([session_id, prompt])
    return codex_bin, args, timeout, workdir


def clean_codex_output(text):
    lines = (text or "").splitlines()
    filtered = []
    for line in lines:
        stripped = line.strip()
        lower = stripped.lower()

        if not stripped:
            filtered.append(line)
            continue

        if stripped.startswith("WARNING: proceeding, even though we could not update PATH:"):
            continue
        if lower.startswith("thinking"):
            continue
        if lower.startswith("working"):
            continue
        if lower.startswith("running"):
            continue
        if lower.startswith("checking"):
            continue
        if lower.startswith("searching"):
            continue
        if lower.startswith("reading"):
            continue
        if lower.startswith("tool call"):
            continue
        if lower.startswith("exec_command"):
            continue
        if lower.startswith("apply_patch"):
            continue
        if lower.startswith("function call"):
            continue
        if lower.startswith("response_item"):
            continue
        if lower.startswith("commentary"):
            continue
        filtered.append(line)

    cleaned = "\n".join(filtered).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned


def read_output_file(path):
    output_path = Path(path)
    if not output_path.exists():
        return ""
    return output_path.read_text(encoding="utf-8").strip()


def parse_codex_json_events(text):
    session_id = None
    messages = []

    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue

        event_type = event.get("type")
        if event_type == "thread.started":
            session_id = event.get("thread_id") or session_id
        if event_type != "item.completed":
            continue

        item = event.get("item") or {}
        if item.get("type") != "agent_message":
            continue

        text = item.get("text")
        if text:
            messages.append(text)

    return session_id, "\n\n".join(messages).strip()


def stream_codex_output(child, timeout, mode):
    chunks = []
    while True:
        try:
            chunk = child.read_nonblocking(size=4096, timeout=timeout)
        except pexpect.TIMEOUT:
            raise
        except pexpect.EOF:
            break

        if not chunk:
            continue

        chunks.append(chunk)
        for line in chunk.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError:
                print(f"[codex_stream] mode={mode} text={stripped}", flush=True)
                continue
            print(
                "[codex_stream]"
                f" mode={mode}"
                f" event={event.get('type', '-')}"
                f" data={json.dumps(event, ensure_ascii=True)}",
                flush=True,
            )

    return "".join(chunks)


def run_codex(prompt, session_id=None):
    with tempfile.NamedTemporaryFile(prefix="codex-last-message-", suffix=".txt", delete=False) as tmp:
        output_file = tmp.name

    mode = "resume" if session_id else "new"
    if session_id:
        codex_bin, args, timeout, workdir = build_codex_resume_args(session_id, prompt, output_file)
    else:
        codex_bin, args, timeout, workdir = build_codex_exec_args(prompt, output_file)
    log_codex_command(mode, workdir, [codex_bin, *args])

    child_env = os.environ.copy()
    child_env.update(ENV)
    child = pexpect.spawn(
        codex_bin,
        args=args,
        encoding="utf-8",
        timeout=timeout,
        env=child_env,
        cwd=workdir,
    )

    try:
        raw_output = stream_codex_output(child, timeout, mode)
    except pexpect.TIMEOUT:
        child.close(force=True)
        return session_id, f"Codex timed out after {timeout} seconds."
    finally:
        if child.isalive():
            child.close()

    try:
        final_output = read_output_file(output_file)
    finally:
        Path(output_file).unlink(missing_ok=True)

    parsed_session_id, json_output = parse_codex_json_events(raw_output)
    effective_session_id = parsed_session_id or session_id
    cleaned_output = clean_codex_output(raw_output)
    exit_code = child.exitstatus if child.exitstatus is not None else child.signalstatus
    log_codex_result(mode, exit_code, raw_output, final_output)
    if exit_code not in (0, None):
        if final_output:
            return effective_session_id, final_output
        fallback_output = json_output or cleaned_output
        return effective_session_id, f"Codex exited with status {exit_code}.\n\n{fallback_output}".strip()

    return effective_session_id, final_output or json_output or cleaned_output or "Codex finished without returning text."


def is_invalid_session_result(text):
    normalized = (text or "").lower()
    patterns = [
        "session not found",
        "thread not found",
        "invalid thread",
        "invalid session",
        "could not find thread",
        "no conversation found",
        "failed to resume",
        "resume failed",
    ]
    return any(pattern in normalized for pattern in patterns)


def make_conversation_key(message):
    if message.get("type") == "stream":
        topic = message.get("topic") or message.get("subject") or ""
        return f"stream:{message.get('stream_id')}:{topic}"

    recipient_id = message.get("recipient_id")
    if recipient_id is not None:
        return f"dm:{recipient_id}"

    recipients = []
    for recipient in message.get("display_recipient") or []:
        email_addr = recipient.get("email")
        if email_addr and email_addr != ENV.get("ZULIP_EMAIL"):
            recipients.append(email_addr)
    return f"dm:{'|'.join(sorted(set(recipients)))}"


def get_thread_lock(thread_key):
    with THREAD_LOCKS_GUARD:
        lock = THREAD_LOCKS.get(thread_key)
        if lock is None:
            lock = threading.Lock()
            THREAD_LOCKS[thread_key] = lock
        return lock


def extract_prompt(message):
    content = strip_html_to_text(message.get("content", ""))
    return content.strip()


def build_status_text(session_id, force_fresh):
    if force_fresh:
        return "正在强制启动一个新的 Codex 会话，请稍等。"
    if session_id:
        return "正在继续当前 Zulip 对话的 Codex 会话，请稍等。"
    return "正在启动当前 Zulip 对话的 Codex 会话，请稍等。"


def build_reply_request(message, content):
    if message.get("type") == "stream":
        return {
            "type": "stream",
            "to": message.get("display_recipient") or message.get("stream_id"),
            "topic": message.get("topic") or message.get("subject") or "",
            "content": content,
        }

    recipients = []
    for recipient in message.get("display_recipient") or []:
        email_addr = recipient.get("email")
        if email_addr and email_addr != ENV.get("ZULIP_EMAIL"):
            recipients.append(email_addr)
    if not recipients and message.get("sender_email") and message.get("sender_email") != ENV.get("ZULIP_EMAIL"):
        recipients.append(message["sender_email"])

    return {
        "type": "direct",
        "to": sorted(set(recipients)),
        "content": content,
    }


def send_message(client, message, content):
    request = build_reply_request(message, content)
    print(f"[zulip_send] request={json.dumps(request, ensure_ascii=True)}", flush=True)
    result = client.send_message(request)
    if result.get("result") != "success":
        raise RuntimeError(f"Failed to send Zulip message: {result}")


def post_chunks(client, message, text):
    for chunk in chunk_text(text):
        send_message(client, message, chunk)


def should_skip_message(message):
    sender_email = message.get("sender_email")
    if not sender_email:
        return True
    if sender_email == ENV.get("ZULIP_EMAIL"):
        return True
    return False


def process_message(client, message):
    prompt = extract_prompt(message)
    thread_key = make_conversation_key(message)
    try:
        if not prompt:
            send_message(client, message, "给我一个具体任务，再让我调用 Codex。")
            return

        lock = get_thread_lock(thread_key)
        with lock:
            if is_session_command(prompt):
                current_session_id = SESSION_STORE.get(thread_key)
                reply = (
                    f"当前 Zulip 对话的 Codex session id: `{current_session_id}`"
                    if current_session_id
                    else "当前 Zulip 对话还没有 Codex session。"
                )
                send_message(client, message, reply)
                return

            if is_reset_command(prompt):
                previous_session_id = SESSION_STORE.get(thread_key)
                SESSION_STORE.delete(thread_key)
                log_session_event("reset", thread_key, existing_session_id=previous_session_id)
                send_message(client, message, "当前 Zulip 对话的 Codex 会话已重置。")
                return

            force_fresh = is_fresh_command(prompt)
            effective_prompt = strip_fresh_command(prompt) if force_fresh else prompt
            if not effective_prompt:
                send_message(client, message, "`/fresh` 后面要跟具体任务。")
                return

            existing_session_id = None if force_fresh else SESSION_STORE.get(thread_key)
            log_session_event(
                "fresh_attempt" if force_fresh else ("resume_attempt" if existing_session_id else "new_attempt"),
                thread_key,
                existing_session_id=existing_session_id,
            )
            send_message(client, message, build_status_text(existing_session_id, force_fresh))

            next_session_id, result = run_codex(effective_prompt, session_id=existing_session_id)
            print(
                "[codex_result]"
                f" thread_key={thread_key}"
                f" result_length={len(result or '')}",
                flush=True,
            )

            if existing_session_id and is_invalid_session_result(result):
                log_session_event(
                    "resume_failed_rebuild",
                    thread_key,
                    existing_session_id=existing_session_id,
                )
                SESSION_STORE.delete(thread_key)
                send_message(client, message, "当前 Zulip 对话的 Codex 会话不可恢复，正在自动重建新会话。")
                next_session_id, result = run_codex(effective_prompt)

            if next_session_id and next_session_id != existing_session_id:
                SESSION_STORE.set(thread_key, next_session_id)
            elif next_session_id:
                SESSION_STORE.touch(thread_key)

            log_session_event(
                "completed",
                thread_key,
                existing_session_id=existing_session_id,
                next_session_id=next_session_id,
            )
            post_chunks(client, message, result)
    except Exception as exc:
        print(
            "[process_error]"
            f" thread_key={thread_key}"
            f" error={exc!r}",
            flush=True,
        )
        try:
            send_message(client, message, f"处理失败: `{exc!r}`")
        except Exception as send_exc:
            print(f"[process_error] failed_to_report={send_exc!r}", flush=True)


def start_background_job(client, message):
    thread = threading.Thread(
        target=process_message,
        args=(client, message),
        daemon=True,
    )
    thread.start()


def validate_env():
    required = [
        "ZULIP_SITE",
        "ZULIP_EMAIL",
        "ZULIP_API_KEY",
    ]
    missing = [name for name in required if not ENV.get(name)]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")


def build_zulip_client():
    return zulip.Client(
        email=ENV["ZULIP_EMAIL"],
        api_key=ENV["ZULIP_API_KEY"],
        site=ENV["ZULIP_SITE"],
    )


def register_message_queue(client):
    result = client.register(event_types=["message"])
    if result.get("result") != "success":
        raise RuntimeError(f"Failed to register Zulip event queue: {result}")
    queue_id = result["queue_id"]
    last_event_id = result["last_event_id"]
    print(
        "[zulip_queue]"
        f" queue_id={queue_id}"
        f" last_event_id={last_event_id}",
        flush=True,
    )
    return queue_id, last_event_id


def handle_event(client, event):
    event_type = event.get("type")
    if event_type == "heartbeat":
        return
    if event_type != "message":
        print(f"[zulip_event] ignored={json.dumps(event, ensure_ascii=True)}", flush=True)
        return

    message = event.get("message") or {}
    print(
        "[zulip_event]"
        f" message_id={message.get('id', '-')}"
        f" type={message.get('type', '-')}"
        f" sender={message.get('sender_email', '-')}"
        f" key={make_conversation_key(message)}",
        flush=True,
    )
    if should_skip_message(message):
        return
    start_background_job(client, message)


def run_event_loop(client):
    queue_id, last_event_id = register_message_queue(client)
    while True:
        try:
            result = client.get_events(queue_id=queue_id, last_event_id=last_event_id)
        except Exception as exc:
            print(f"[zulip_poll_error] error={exc!r}", flush=True)
            time.sleep(5)
            continue

        if result.get("result") != "success":
            code = result.get("code", "")
            print(f"[zulip_poll_error] result={json.dumps(result, ensure_ascii=True)}", flush=True)
            if code == "BAD_EVENT_QUEUE_ID":
                queue_id, last_event_id = register_message_queue(client)
                continue
            time.sleep(5)
            continue

        for event in result.get("events", []):
            last_event_id = max(last_event_id, event.get("id", last_event_id))
            handle_event(client, event)


def log_session_event(event, thread_key, existing_session_id=None, next_session_id=None):
    print(
        "[session]"
        f" event={event}"
        f" thread_key={thread_key}"
        f" existing_session_id={existing_session_id or '-'}"
        f" next_session_id={next_session_id or '-'}",
        flush=True,
    )


def log_codex_command(mode, workdir, args):
    print(
        "[codex_cmd]"
        f" mode={mode}"
        f" cwd={workdir}"
        f" args={json.dumps(args, ensure_ascii=True)}",
        flush=True,
    )


def log_codex_result(mode, exit_code, raw_output, final_output):
    raw_preview = (raw_output or "").strip().replace("\n", "\\n")
    if len(raw_preview) > 500:
        raw_preview = raw_preview[:500] + "...<truncated>"
    final_preview = (final_output or "").strip().replace("\n", "\\n")
    if len(final_preview) > 500:
        final_preview = final_preview[:500] + "...<truncated>"
    print(
        "[codex_exit]"
        f" mode={mode}"
        f" exit_code={exit_code}"
        f" raw_preview={json.dumps(raw_preview, ensure_ascii=True)}"
        f" final_preview={json.dumps(final_preview, ensure_ascii=True)}",
        flush=True,
    )


def main():
    validate_env()
    client = build_zulip_client()
    print("[startup] codex-zulip is running", flush=True)
    run_event_loop(client)


if __name__ == "__main__":
    main()

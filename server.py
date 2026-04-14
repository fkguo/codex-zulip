import html
import json
import os
import re
import shlex
import tempfile
import threading
import time
from base64 import b64encode
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

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
TEXT_ATTACHMENT_EXTENSIONS = {
    ".c",
    ".cc",
    ".cpp",
    ".css",
    ".csv",
    ".go",
    ".h",
    ".hpp",
    ".html",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".log",
    ".md",
    ".py",
    ".rb",
    ".rs",
    ".sh",
    ".sql",
    ".text",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
MARKDOWN_LINK_PATTERN = re.compile(r"(!?)\[([^\]]*)\]\(([^)]+)\)")


class MessageContentParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.links = []
        self.images = []
        self._current_href = None
        self._current_link_parts = []

    def handle_starttag(self, tag, attrs):
        attrs_map = dict(attrs)
        if tag == "br":
            self.parts.append("\n")
            return
        if tag == "li":
            self.parts.append("- ")
            return
        if tag == "a":
            self._current_href = attrs_map.get("href")
            self._current_link_parts = []
            return
        if tag == "img":
            src = attrs_map.get("src")
            alt = attrs_map.get("alt", "")
            if src:
                self.images.append({"src": src, "alt": alt})

    def handle_endtag(self, tag):
        if tag in {"p", "div", "li"}:
            self.parts.append("\n")
            return
        if tag == "a":
            href = self._current_href
            text = "".join(self._current_link_parts).strip()
            if href:
                self.links.append({"href": href, "text": text or href})
            self._current_href = None
            self._current_link_parts = []

    def handle_data(self, data):
        if not data:
            return
        self.parts.append(data)
        if self._current_href is not None:
            self._current_link_parts.append(data)


def parse_message_content(content):
    normalized = (content or "").strip()
    if not normalized:
        return "", []

    parser = MessageContentParser()
    parser.feed(normalized)
    parser.close()

    markdown_links = []

    def replace_markdown_link(match):
        _is_image, label, href = match.groups()
        cleaned_href = href.strip()
        cleaned_label = (label or "").strip() or Path(urlparse(cleaned_href).path).name or cleaned_href
        markdown_links.append({"href": cleaned_href, "text": cleaned_label})
        return cleaned_label

    text = html.unescape("".join(parser.parts))
    text = MARKDOWN_LINK_PATTERN.sub(replace_markdown_link, text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = text.strip()

    links = []
    seen = set()
    for item in [*parser.links, *parser.images, *markdown_links]:
        href = item.get("href") or item.get("src")
        if not href or href in seen:
            continue
        links.append(
            {
                "href": href,
                "text": item.get("text") or item.get("alt") or Path(urlparse(href).path).name or href,
            }
        )
        seen.add(href)
    return text, links


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
    text, _links = parse_message_content(content)
    return text


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


def format_zulip_display_math(body):
    normalized = (body or "").strip()
    if not normalized:
        return ""
    return f"\n```math\n{normalized}\n```\n"


def has_explicit_math_delimiters(text):
    normalized = (text or "").strip()
    if not normalized:
        return False
    pairs = [
        (r"\[", r"\]"),
        (r"\(", r"\)"),
        ("$$", "$$"),
        ("$", "$"),
    ]
    for left, right in pairs:
        if normalized.startswith(left) and normalized.endswith(right):
            inner = normalized[len(left) : len(normalized) - len(right)].strip()
            if inner:
                return True
    return False


def unwrap_math_delimiters(text):
    normalized = (text or "").strip()
    pairs = [
        (r"\[", r"\]"),
        (r"\(", r"\)"),
        ("$$", "$$"),
        ("$", "$"),
    ]
    for left, right in pairs:
        if normalized.startswith(left) and normalized.endswith(right):
            inner = normalized[len(left) : len(normalized) - len(right)].strip()
            if inner:
                return inner
    return normalized


def normalize_plain_zulip_math(text):
    if not text:
        return text

    def replace_bracket_display(match):
        return format_zulip_display_math(match.group(1))

    def replace_line_display_dollars(match):
        return format_zulip_display_math(match.group(1))

    def replace_multiline_dollars(match):
        body = match.group(1)
        if "\n" not in body:
            return match.group(0)
        return format_zulip_display_math(body)

    def replace_paren_inline(match):
        body = match.group(1).strip()
        if not body:
            return match.group(0)
        return f"$${body}$$"

    def replace_single_inline(match):
        body = match.group(1)
        if not body:
            return match.group(0)
        if body[0].isspace() or body[-1].isspace():
            return match.group(0)
        return f"$${body}$$"

    text = re.sub(r"(?s)\\\[\s*(.*?)\s*\\\]", replace_bracket_display, text)
    text = re.sub(r"(?m)^[ \t]*\$\$([^\n]+?)\$\$[ \t]*$", replace_line_display_dollars, text)
    text = re.sub(r"(?s)(?<!\$)\$\$(.+?)\$\$(?!\$)", replace_multiline_dollars, text)
    text = re.sub(r"(?s)\\\((.+?)\\\)", replace_paren_inline, text)
    text = re.sub(r"(?<!\\)(?<!\$)\$(?!\$)([^$\n]+?)(?<!\\)\$(?!\$)", replace_single_inline, text)
    return text


def normalize_zulip_math_markup(text):
    if not text:
        return text

    fenced_parts = re.split(r"(```[\s\S]*?```)", text)
    normalized_parts = []
    for fenced_part in fenced_parts:
        if fenced_part.startswith("```") and fenced_part.endswith("```"):
            normalized_parts.append(fenced_part)
            continue

        inline_parts = re.split(r"(`[^`\n]+`)", fenced_part)
        for inline_part in inline_parts:
            if inline_part.startswith("`") and inline_part.endswith("`"):
                inline_body = inline_part[1:-1]
                if has_explicit_math_delimiters(inline_body):
                    normalized_parts.append(f"$${unwrap_math_delimiters(inline_body)}$$")
                else:
                    normalized_parts.append(inline_part)
            else:
                normalized_parts.append(normalize_plain_zulip_math(inline_part))

    return "".join(normalized_parts)


def parse_codex_timeout(value, default=900):
    try:
        timeout = int(value)
    except (TypeError, ValueError):
        timeout = default
    return None if timeout <= 0 else timeout


def get_codex_settings():
    codex_bin = ENV.get("CODEX_BIN", "codex")
    model = ENV.get("OPENAI_MODEL", "gpt-5.4")
    workdir = ENV.get("CODEX_WORKDIR", str(Path.cwd()))
    timeout = parse_codex_timeout(ENV.get("CODEX_TIMEOUT_SECONDS", "900"))
    sandbox = ENV.get("CODEX_SANDBOX", "workspace-write")
    extra_args = ENV.get("CODEX_EXTRA_ARGS", "").strip()
    full_auto = ENV.get("CODEX_FULL_AUTO", "0") == "1"
    return codex_bin, model, workdir, timeout, sandbox, extra_args, full_auto


def get_attachment_settings():
    base_dir = Path(
        ENV.get("CODEX_ZULIP_ATTACHMENT_DIR", Path(__file__).with_name(".codex-zulip-downloads"))
    )
    max_attachments = int(ENV.get("CODEX_ZULIP_MAX_ATTACHMENTS", "8"))
    max_bytes = int(ENV.get("CODEX_ZULIP_MAX_ATTACHMENT_BYTES", str(10 * 1024 * 1024)))
    inline_text_bytes = int(ENV.get("CODEX_ZULIP_INLINE_TEXT_BYTES", "20000"))
    download_timeout = int(ENV.get("CODEX_ZULIP_DOWNLOAD_TIMEOUT_SECONDS", "60"))
    return base_dir, max_attachments, max_bytes, inline_text_bytes, download_timeout


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


def is_attachment_link(href):
    parsed = urlparse(href)
    return "/user_uploads/" in (parsed.path or "")


def build_attachment_download_dir(message):
    base_dir, _max_attachments, _max_bytes, _inline_text_bytes, _download_timeout = get_attachment_settings()
    message_id = message.get("id", "unknown")
    directory = base_dir / str(message_id)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def absolutize_url(href):
    return urljoin(ENV["ZULIP_SITE"], href)


def build_basic_auth_header():
    raw = f"{ENV['ZULIP_EMAIL']}:{ENV['ZULIP_API_KEY']}".encode("utf-8")
    return f"Basic {b64encode(raw).decode('ascii')}"


def sanitize_attachment_name(name, fallback="attachment"):
    candidate = Path(name or fallback).name.strip() or fallback
    return candidate


def allocate_attachment_path(directory, name):
    target = directory / sanitize_attachment_name(name)
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    for index in range(1, 1000):
        candidate = directory / f"{stem}-{index}{suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not allocate attachment path for {name!r}")


def fetch_temporary_upload_url(client, source_url):
    parsed = urlparse(source_url)
    match = re.search(r"/user_uploads/([^/]+)/(.+)$", parsed.path or "")
    if not match:
        return source_url

    realm_id = match.group(1)
    filename = match.group(2)
    result = client.call_endpoint(url=f"user_uploads/{realm_id}/{filename}", method="GET")
    if result.get("result") != "success" or not result.get("url"):
        raise RuntimeError(f"Failed to get temporary file URL: {result}")
    return absolutize_url(result["url"])


def download_remote_file(url, destination, max_bytes, timeout, use_auth):
    headers = {}
    if use_auth:
        headers["Authorization"] = build_basic_auth_header()

    request = Request(url, headers=headers)
    with urlopen(request, timeout=timeout) as response:
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > max_bytes:
            raise RuntimeError(f"Attachment exceeds {max_bytes} bytes.")

        data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise RuntimeError(f"Attachment exceeds {max_bytes} bytes.")

    destination.write_bytes(data)
    return len(data)


def try_decode_inline_text(path, max_bytes):
    if path.suffix.lower() not in TEXT_ATTACHMENT_EXTENSIONS:
        return None

    data = path.read_bytes()
    if len(data) > max_bytes:
        return None
    if b"\x00" in data:
        return None

    try:
        return data.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None


def download_message_attachments(client, message, links):
    attachments = []
    directory = build_attachment_download_dir(message)
    _base_dir, max_attachments, max_bytes, inline_text_bytes, download_timeout = get_attachment_settings()

    candidates = [item for item in links if is_attachment_link(item.get("href", ""))]
    for item in candidates[:max_attachments]:
        source_url = absolutize_url(item["href"])
        filename = item.get("text") or Path(urlparse(source_url).path).name or "attachment"
        local_path = allocate_attachment_path(directory, filename)
        attachment = {
            "display_name": sanitize_attachment_name(filename),
            "source_url": source_url,
            "local_path": str(local_path),
        }

        try:
            temporary_url = fetch_temporary_upload_url(client, source_url)
            size_bytes = download_remote_file(
                temporary_url,
                local_path,
                max_bytes=max_bytes,
                timeout=download_timeout,
                use_auth=False,
            )
        except Exception as temp_exc:
            print(
                "[zulip_attachment]"
                f" message_id={message.get('id', '-')}"
                f" source_url={json.dumps(source_url, ensure_ascii=True)}"
                f" fallback=direct_get"
                f" error={temp_exc!r}",
                flush=True,
            )
            try:
                size_bytes = download_remote_file(
                    source_url,
                    local_path,
                    max_bytes=max_bytes,
                    timeout=download_timeout,
                    use_auth=True,
                )
            except Exception as download_exc:
                attachment["error"] = repr(download_exc)
                attachments.append(attachment)
                print(
                    "[zulip_attachment]"
                    f" message_id={message.get('id', '-')}"
                    f" source_url={json.dumps(source_url, ensure_ascii=True)}"
                    f" status=failed"
                    f" error={download_exc!r}",
                    flush=True,
                )
                continue

        attachment["size_bytes"] = size_bytes
        attachment["inline_text"] = try_decode_inline_text(local_path, inline_text_bytes)
        attachments.append(attachment)
        print(
            "[zulip_attachment]"
            f" message_id={message.get('id', '-')}"
            f" source_url={json.dumps(source_url, ensure_ascii=True)}"
            f" local_path={json.dumps(str(local_path), ensure_ascii=True)}"
            f" size_bytes={size_bytes}",
            flush=True,
        )

    return attachments


def build_codex_prompt(user_prompt, attachments):
    normalized_prompt = (user_prompt or "").strip() or "The user sent attachments without additional text."
    sections = [
        "You are replying through a Zulip bridge.",
        "If you want the bridge to upload a local file back to Zulip, include one line exactly as:",
        "ZULIP_UPLOAD: /absolute/or/relative/path/to/file",
        "Keep any normal user-facing explanation outside those directive lines.",
        "When writing math for Zulip, format inline math as $$...$$.",
        "Format display math as fenced math blocks:",
        "```math",
        "...",
        "```",
        "Never wrap math in backticks.",
        "Keep shell commands, file paths, filenames, and code in backticks or normal code fences.",
        "",
        "User message:",
        normalized_prompt,
    ]

    if attachments:
        sections.extend(["", "Downloaded Zulip attachments:"])
        for index, attachment in enumerate(attachments, start=1):
            sections.append(f"{index}. filename: {attachment['display_name']}")
            sections.append(f"   local_path: {attachment['local_path']}")
            sections.append(f"   source_url: {attachment['source_url']}")
            if attachment.get("size_bytes") is not None:
                sections.append(f"   size_bytes: {attachment['size_bytes']}")
            if attachment.get("error"):
                sections.append(f"   download_error: {attachment['error']}")
            inline_text = attachment.get("inline_text")
            if inline_text:
                sections.append(f"   inline_text_utf8:\n{inline_text}")

    return "\n".join(sections).strip()


def split_upload_directives(text):
    upload_paths = []
    visible_lines = []

    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("ZULIP_UPLOAD:"):
            path = stripped[len("ZULIP_UPLOAD:") :].strip().strip("`")
            if path and path not in upload_paths:
                upload_paths.append(path)
            continue
        visible_lines.append(line)

    visible_text = "\n".join(visible_lines).strip()
    return visible_text, upload_paths


def resolve_upload_path(path_text):
    path = Path(path_text).expanduser()
    if path.is_absolute():
        return path
    _codex_bin, _model, workdir, _timeout, _sandbox, _extra_args, _full_auto = get_codex_settings()
    return Path(workdir) / path


def escape_markdown_link_text(text):
    return (text or "").replace("[", "(").replace("]", ")")


def upload_requested_files(client, requested_paths):
    uploaded_links = []
    errors = []

    for raw_path in requested_paths:
        resolved_path = resolve_upload_path(raw_path)
        if not resolved_path.exists() or not resolved_path.is_file():
            errors.append(f"未上传 `{raw_path}`：文件不存在。")
            continue

        try:
            with resolved_path.open("rb") as fp:
                result = client.upload_file(fp)
            if result.get("result") != "success":
                raise RuntimeError(f"upload failed: {result}")
            url = result.get("url") or result.get("uri")
            filename = result.get("filename") or resolved_path.name
            if not url:
                raise RuntimeError(f"missing upload URL: {result}")
            uploaded_links.append(f"[{escape_markdown_link_text(filename)}]({url})")
            print(
                "[zulip_upload_file]"
                f" local_path={json.dumps(str(resolved_path), ensure_ascii=True)}"
                f" url={json.dumps(url, ensure_ascii=True)}",
                flush=True,
            )
        except Exception as exc:
            errors.append(f"未上传 `{raw_path}`：`{exc!r}`")
            print(
                "[zulip_upload_file]"
                f" local_path={json.dumps(str(resolved_path), ensure_ascii=True)}"
                f" error={exc!r}",
                flush=True,
            )

    return uploaded_links, errors


def build_final_reply(result_text, uploaded_links, upload_errors):
    sections = []
    normalized_result = (result_text or "").strip()
    if normalized_result:
        sections.append(normalized_result)
    if uploaded_links:
        sections.append("已上传文件：\n" + "\n".join(uploaded_links))
    if upload_errors:
        sections.append("\n".join(upload_errors))

    if sections:
        return "\n\n".join(sections).strip()
    return "Codex finished without returning text."


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


def extract_message_context(message):
    prompt, links = parse_message_content(message.get("content", ""))
    return prompt.strip(), links


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
    normalized = normalize_zulip_math_markup(text)
    if normalized != text:
        print(
            "[zulip_format]"
            f" normalized_math=1"
            f" original_length={len(text or '')}"
            f" normalized_length={len(normalized or '')}",
            flush=True,
        )
    for chunk in chunk_text(normalized):
        send_message(client, message, chunk)


def should_skip_message(message):
    sender_email = message.get("sender_email")
    if not sender_email:
        return True
    if sender_email == ENV.get("ZULIP_EMAIL"):
        return True
    return False


def process_message(client, message):
    prompt, links = extract_message_context(message)
    thread_key = make_conversation_key(message)
    try:
        attachment_links = [item for item in links if is_attachment_link(item.get("href", ""))]
        if not prompt and not attachment_links:
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
            if not effective_prompt and not attachment_links:
                send_message(client, message, "`/fresh` 后面要跟具体任务。")
                return

            existing_session_id = None if force_fresh else SESSION_STORE.get(thread_key)
            log_session_event(
                "fresh_attempt" if force_fresh else ("resume_attempt" if existing_session_id else "new_attempt"),
                thread_key,
                existing_session_id=existing_session_id,
            )
            send_message(client, message, build_status_text(existing_session_id, force_fresh))

            attachments = download_message_attachments(client, message, links)
            codex_prompt = build_codex_prompt(effective_prompt, attachments)
            next_session_id, result = run_codex(codex_prompt, session_id=existing_session_id)
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
                next_session_id, result = run_codex(codex_prompt)

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
            visible_result, upload_paths = split_upload_directives(result)
            uploaded_links, upload_errors = upload_requested_files(client, upload_paths)
            final_reply = build_final_reply(visible_result, uploaded_links, upload_errors)
            post_chunks(client, message, final_reply)
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

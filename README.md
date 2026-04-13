# codex-zulip

一个最小可用的 Zulip 机器人桥接服务，基于 `zulip` Python API 和 `pexpect`：

- Zulip 通过实时事件队列把消息事件提供给本机进程
- Python 进程调用本机 `codex exec` / `codex exec resume`
- 再把 Codex 的最终输出发回原来的 Zulip 对话

## 当前能力

- 支持频道消息和私聊消息
- 默认用 `gpt-5.4` 调用本机 `codex exec`
- 按 Zulip 对话维度复用 Codex session
- 支持把 Codex 长输出分片发送
- 支持下载 Zulip 消息里上传的附件到本地，再把本地路径提供给 Codex
- 支持 Codex 通过显式指令上传本地文件回当前 Zulip 对话
- screen 端会实时打印 Codex 的 JSON 事件流和调试日志
- 支持 `/reset`、`/fresh`、`/session` 控制当前 Zulip 对话的 Codex session

## 目录结构

- `server.py`: Zulip 事件轮询、会话复用、Codex 调用
- `requirements.txt`: Python 依赖
- `.env.example`: 环境变量模板
- `.codex-zulip-sessions.json`: Zulip 对话到 Codex session 的本地缓存

## 安装

1. 进入项目目录

```bash
cd /ssd/home/pz/codex-zulip
```

2. 安装依赖

```bash
pip install -r requirements.txt
```

3. 复制环境变量模板

```bash
cp .env.example .env
```

4. 填写 `.env`

```env
OPENAI_MODEL=gpt-5.4

ZULIP_SITE=https://your-zulip.example.com
ZULIP_EMAIL=codex-bot@example.com
ZULIP_API_KEY=your_zulip_api_key

CODEX_BIN=codex
CODEX_WORKDIR=/ssd/home/pz
CODEX_TIMEOUT_SECONDS=900
CODEX_SANDBOX=danger-full-access
CODEX_FULL_AUTO=0
CODEX_EXTRA_ARGS=
CODEX_ZULIP_SESSION_STORE=/ssd/home/pz/codex-zulip/.codex-zulip-sessions.json
CODEX_ZULIP_ATTACHMENT_DIR=/ssd/home/pz/codex-zulip/.codex-zulip-downloads
CODEX_ZULIP_MAX_ATTACHMENTS=8
CODEX_ZULIP_MAX_ATTACHMENT_BYTES=10485760
CODEX_ZULIP_INLINE_TEXT_BYTES=20000
CODEX_ZULIP_DOWNLOAD_TIMEOUT_SECONDS=60
```

5. 先确认本机 `codex` 已登录可用

```bash
codex exec --skip-git-repo-check "reply with exactly OK"
```

6. 启动服务

```bash
python3 server.py
```

## Zulip Bot 配置

1. 在 Zulip 管理后台创建一个 bot

- 打开组织设置里的 `Bots`
- 创建一个新 bot，例如 `codex-bot`
- 记录 bot 的:
  - `email`
  - `API key`

2. 获取 Zulip 站点地址

- 例如 `https://your-zulip.example.com`
- 这个地址填到 `.env` 的 `ZULIP_SITE`

3. 把 bot 凭据填到 `.env`

- `ZULIP_EMAIL`
- `ZULIP_API_KEY`
- `ZULIP_SITE`

4. 把 bot 订阅到你希望它能响应的频道

- 如果 bot 没订阅某个频道，就收不到该频道的消息事件
- 私聊不需要频道订阅，直接给 bot 发私聊即可

## 会话复用

当前实现是“按 Zulip 对话划分 Codex session”。

- 频道消息的 key 是 `stream:{stream_id}:{topic}`
- 同一个 stream 但不同 topic，会对应不同的 Codex session
- 私聊优先使用 `recipient_id` 作为 key
- 不同频道、不同 topic、不同私聊对话之间不会共享上下文
- 服务会把 `conversation_key -> session_id` 写到 [`.codex-zulip-sessions.json`](/ssd/home/pz/codex-zulip/.codex-zulip-sessions.json)
- `server.py` 重启后，会继续复用本地缓存里的 session id
- 同一个对话内部会串行处理消息，避免并发 `resume` 导致上下文错乱

当前命令都只作用于“当前 Zulip 对话”：

- `/reset`: 清掉当前对话的 Codex session
- `/fresh 你的任务`: 忽略当前对话旧 session，这条消息强制创建一个新 session
- `/session`: 返回当前对话正在使用的 Codex session id

## 附件处理

- 如果用户在 Zulip 消息里上传了文件，服务会解析消息 HTML 里的 `user_uploads` 链接
- 附件会被下载到 `CODEX_ZULIP_ATTACHMENT_DIR/<message_id>/`
- 小型 UTF-8 文本附件会额外把内容摘录直接注入 prompt
- 其他附件会以本地路径形式提供给 Codex，让它按需继续读取
- 单条消息默认最多处理 8 个附件，单文件默认最大 10 MiB

如果希望 Codex 把本地文件上传回当前 Zulip 对话，需要让它在最终回复里单独输出这样的行：

```text
ZULIP_UPLOAD: /absolute/or/relative/path/to/file
```

- 可以输出多行 `ZULIP_UPLOAD: ...`
- 桥接层会先上传文件，再把返回的 Zulip 文件链接和正常文本说明一起发回原对话
- 相对路径会按 `CODEX_WORKDIR` 解析

## 运行日志

终端里可以看到这些调试日志：

- `[zulip_queue]`: 事件队列注册成功
- `[zulip_event]`: 收到一条 Zulip 消息事件
- `[session]`: 当前对话命中了新会话还是 resume
- `[codex_cmd]`: 实际调用的 `codex` 命令
- `[codex_stream]`: Codex 的实时 JSON 事件流
- `[codex_exit]`: Codex 退出码和输出摘要
- `[zulip_attachment]`: 入站附件下载日志
- `[zulip_upload_file]`: 出站文件上传日志
- `[zulip_send]`: 发回 Zulip 的消息请求

## 最小排障

- bot 收不到频道消息:
  通常是 bot 没订阅该频道
- bot 能收到消息但回不去:
  优先检查 `ZULIP_EMAIL` / `ZULIP_API_KEY` / `ZULIP_SITE`
- `codex exec` 正常但服务里失败:
  先看终端里的 `[codex_cmd]` 和 `[codex_exit]`
- `resume` 失败:
  在对应对话里发送 `/reset` 或 `/fresh 你的任务`

## 后续建议

- 增加“只响应被提及的频道消息”模式
- 改进 Zulip HTML 消息到纯文本 prompt 的转换
- 增加白名单频道 / topic 过滤
- 增加 systemd 服务和日志落盘

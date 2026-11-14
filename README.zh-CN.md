# mac-relay（中文）

一个自托管的轻量任务中继。box 上的 bot 把任务放进队列，Mac 上的 worker 用本机工具（hermes、DeepSeek harness、`codex exec`、确定性 `schwab` CLI）执行。结果和文件回传到 box 后，只给发起任务的那个 bot 发**一次** `done`/`failed` 唤醒。Mac 接单由 `relay-send` 在同一次调用里轮询到；`started` 不再 webhook。

* 只用 Python 3 标准库。数据存在 SQLite 里。同一个任务 id 重复入队不会重复执行；每个任务只保留第一个结果，失败任务只上报一次 `failed`。
* 全部用长轮询，不用定时器、心跳或 cron。
* 鉴权用 Bearer 共享密钥，密钥从 `RELAY_SECRET_FILE` 指向的文件读取，不会写进日志。

## 服务器安装（systemd + Caddy）
参考 README.md 的 “Server install”。服务监听 `127.0.0.1:18790`。把 `deploy/Caddyfile.snippet`（`handle_path /relay/*`）贴进站点块，先 `caddy validate`，再 reload。部署前按 `deploy/CHECKLIST.md` 删掉旧的 `yobiru-selfhost-relay` 块。

## 不使用 VPS

上面的 systemd + Caddy 仍是主路径。下面三种是不想要 VPS 时的替代。步骤、URL，以及和这条 VPS 路径的取舍，见 [docs/no-vps.zh-CN.md](docs/no-vps.zh-CN.md)（English: [docs/no-vps.md](docs/no-vps.md)）。服务器仍是 `relay/server.py`：没有 TLS；默认只监听 `127.0.0.1`，不会自动出现在 tailnet 上。没有反代时 URL 不要加 `/relay`，那个前缀只来自 Caddy 的 `handle_path /relay/*`。

1. **中继在 Mac 上。** box 和 Mac 在同一个 Tailscale。box 的 `RELAY_URL=http://<Mac 的 100.x>:18790`。Mac 必须保持醒着。ACL 和 Mac 防火墙收紧时没有公网入口。
2. **中继在 box 上。** box 跑服务器，`RELAY_URL=http://127.0.0.1:18790`。Mac worker 的 `primary_url=http://<box 的 100.x>:18790`。Mac 睡眠时队列还在；worker 不在线时任务不会执行。
3. **本机中继加隧道。** 服务器留在 Mac 或 box 上。Cloudflare Tunnel 或 Tailscale Funnel 给另一侧一个 HTTPS 基址。隧道直接转到端口 18790 时，这个 URL 不加 `/relay`。地址超出 tailnet，共享密钥是必须的。

只有一个地址时不要设 `RELAY_FALLBACK_URL` 或多余的 `endpoints`。有两个中继时（例如方案 2 为主、VPS 为备）都列上，见 [docs/channels.zh-CN.md](docs/channels.zh-CN.md)。

## 接口与文件大小

`GET /health` 不鉴权。默认返回 `{"ok": true, "soft_body": 16777216, "max_body": 536870912}`。`soft_body` 来自 `RELAY_MAX_SOFT_BODY`（默认 16MiB），只限制 `multipart/*`。超过这个 `Content-Length` 的 multipart 返回 413，并带 `Connection: close`；服务器不读这个 body，也不交给邮件解析器。`max_body` 来自 `RELAY_MAX_BODY`（默认 512MiB），是其余请求体的硬上限，包括流式 PUT。

Worker 的输出走另一条路径。每个 stdout、stderr 和 outbox 文件先 `PUT /tasks/{id}/files/{name}?kind=out`（`application/octet-stream`，必须带 `Content-Length`，从磁盘流式写出，上限是 `RELAY_MAX_BODY`），再向 `/tasks/{id}/result` POST `application/json`：

```json
{"status": "done", "summary": "stdout 最后一行", "files": ["stdout.txt", "notes.txt"]}
```

`files` 是已经落在该任务 `out` 目录里的文件名数组。没有 PUT 过的名字会被忽略。第一个结果仍然有效，后来的重复提交不会覆盖。`GET /files/{id}/in|out/{name}` 按 1MiB 一块往外写，并带上 `Content-Length`。`worker.json` 里的 `max_soft_file`（默认 16MiB）只在 worker 启动时写进日志，不改变上传方式：worker 始终是 PUT 再加 JSON。

任务 id 和 sender 仍是 ASCII：`[A-Za-z0-9._-]{1,128}`。附件和结果的**文件名**是单个路径段，可以是非 ASCII（例如 `验收结果.md`）。空名、`.`、`..`，或包含斜杠、反斜杠、NUL，或超过 128 个字符（或 247 个 UTF-8 字节）时返回 `400 {"error":"bad name"}`。

**任务输入附件（`bin/relay-send --file`）**：v2 服务器上先以 `staged` 建任务，每个文件用分块可续传 PUT（`kind=in`，校验 sha256）上传，再 `POST /tasks/{id}/commit` 才可被认领。旧服务器仍用 multipart，上限 16MiB。分块 PUT、Range 下载等 v2 接口见 [docs/channels.zh-CN.md](docs/channels.zh-CN.md)。

已经在跑的中继要升级：换上这份 `relay/server.py` 和 `mac/relay-worker.py`，然后在同一窗口里重启服务器和 worker。systemd 单元不用加新变量。旧 worker 仍然可以 POST 不超过 16MiB 的 multipart 结果。更大的 multipart 结果会得到 413，直到那台 worker 更新；服务器会关掉连接，而不是把 body 缓冲进内存。Caddy 的 `request_body max_size 512MB` 对齐的是 `RELAY_MAX_BODY`，不会把 16MiB 的 multipart 上限抬高。

## box 端
* `bin/relay-send <slug> <hermes|deepseek|codex|schwab> '<prompt>' [--file f]`：入队后把任务 id 打在 stdout，并在同进程里轮询 `GET /tasks/{id}`，直到 `state` 为 `running`（worker 已 POST `/started`）或已经是 `done`/`failed`。默认等 45 秒（`RELAY_STARTED_WAIT_S`）。接单成功时 stderr 记一行 `accepted`，exit 0。超时仍入队成功时，stderr 写「已入队、尚未接单」，exit 2，stdout 仍是任务 id。入队失败 exit 1。
* `bin/relay-box-agent`：常驻进程，长轮询 `done`/`failed`。完成后把文件下载到 `/workspace/relay-results/<id>/`，写 `result.json`，再 POST **一次**最终状态。不向 sender webhook 发 `status=started`。若仍收到 started 事件，只 ack，并把 `.wake-started` 写成 `skipped/no-webhook`。按 `/workspace/relay/wake/<slug>.url` 和 `.key` 唤醒。body 始终是 task_id、status、summary、sender、result_path。缺配置时最终状态记一条错误，不重试；`.wake` 至多一次。

## Mac 端
复制 `mac/relay-worker.py` 和 `examples/worker.example.json`，修改其中的 URL 和工具命令模板，再用 `mac/com.example.mac-relay.worker.plist` 生成 LaunchAgent（替换 `__占位符__`）。每个任务作为独立的后台子进程运行，没有超时。工具写进 outbox 的非空文件会连同 stdout、stderr 以流式 PUT 上传，结果 POST 是 JSON（`files` 列出这些文件名）。worker 重启后会补传已完成但还没确认上传的结果。某一个任务上传失败不会挡住同一次扫描里的其他任务。HTTP 错误会把响应体写进日志。连续 `upload_4xx_quarantine` 次（默认 3）HTTP 4xx 会在任务目录里写下 `quarantine` 标记并不再重试；次数记在该目录的 `upload-4xx` 文件里（重启还在）。任务文件会留在原地。

**多通道：** `endpoints` 是有序的 `{"name", "url"}` 列表（第一个为主）。worker 同时长轮询所有通道；同一任务 id 被认领两次也只执行一次。结果回到接单的通道；连续 `mirror_after_s`（默认 60 秒）既没成功也没有上传进展时，镜像到其他通道交付，并在 `closeout_after_s`（默认 600 秒）后关闭原通道上的陈旧行。旧配置 `primary_url` / `fallback_url` 仍可用；`fail_threshold`、`cooldown_s`、`conn.json`、`reset-primary` 不再使用。`stdin_prompt_over`（`{"工具": 字节数}`）让长 prompt 走 stdin。

box 端：`RELAY_FALLBACK_URL` 是逗号分隔的备用基址列表。`relay-box-agent` 同时在所有通道上长轮询结果，同一任务 id 只唤醒一次。`relay-send` 发到第一个可用通道；`RELAY_HANDOFF_S`（默认 20 秒）内 Mac 未接单，就把同一任务 id 转交下一个通道并撤回原副本。访问 `100.x`、`*.ts.net`、localhost 不走代理。长 prompt 用 `relay-send <slug> <tool> - < prompt.txt` 或 `--prompt-file`。改了 env 要重启 box-agent。每条任务只唤醒一次（`done` 或 `failed`）。接单不走 webhook。详情和故障情形见 [docs/channels.zh-CN.md](docs/channels.zh-CN.md)。

## 测试
运行 `python3 tests/selftest.py`，结果写在 TESTS.md。

MIT 许可证。

# mac-relay（中文）

一个自托管的轻量任务中继。box 上的 bot 把任务放进队列，Mac 上的 worker 用本机工具（hermes、DeepSeek harness、`codex exec`）执行。结果和文件回传到 box 后，只给发起任务的那个 bot 发**一次**唤醒。

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

只有一个地址时不要设 `RELAY_FALLBACK_URL` / `fallback_url`。两个基址都存在时才设，例如隧道 URL 再加 `http://100.x:18790`。

## box 端
* `bin/relay-send <slug> <hermes|deepseek|codex> '<prompt>' [--file f]`：入队后马上返回任务 id。
* `bin/relay-box-agent`：常驻进程，用一条长轮询连接等结果。拿到结果后把文件下载到 `/workspace/relay-results/<id>/`，写 `result.json`，再按 `/workspace/relay/wake/<slug>.url` 和 `.key` 给发送方的 webhook POST 一次。POST 内容只有 task_id、status、summary、sender、result_path。如果缺少某个 slug 的配置，只记一条错误日志，不会重试。

## Mac 端
复制 `mac/relay-worker.py` 和 `examples/worker.example.json`，修改其中的 URL 和工具命令模板，再用 `mac/com.example.mac-relay.worker.plist` 生成 LaunchAgent（替换 `__占位符__`）。每个任务作为独立的后台子进程运行，没有超时。工具写进 outbox 的文件会连同 stdout 一起上传。worker 重启后会补传已完成但还没确认上传的结果。

`fallback_url`（比如 Tailscale 地址）只在主地址连续失败 `fail_threshold` 次之后才启用。切过去以后会一直用备用地址，直到冷却期过后探测到主地址恢复，或者手动执行 `touch <state_dir>/reset-primary` 才切回，不会来回切换。重试退避是指数增长的，有上限。

盒子端同理：在 box env 里设 `RELAY_FALLBACK_URL`（比如中转服务器的 Tailscale 地址 `http://100.x.y.z:18791`），`relay-box-agent` 在主地址连续 `RELAY_FAIL_THRESHOLD`（默认 3）次失败（连接错误或 HTTP 5xx）后切到备用，每 `RELAY_COOLDOWN_S`（默认 300）秒最多探一次主地址 `/health`，恢复才切回。备用请求不走代理。日志里会有 `connection -> fallback` / `connection -> primary`。`relay-send` 在主地址失败时也会用 `RELAY_FALLBACK_URL` 重试一次（按 task id 幂等）。改了 env 要重启 box-agent。每条结果仍然只唤醒一次。

## 测试
运行 `python3 tests/selftest.py`，结果写在 TESTS.md。

MIT 许可证。

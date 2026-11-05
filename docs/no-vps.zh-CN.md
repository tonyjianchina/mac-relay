# 不使用 VPS 部署 mac-relay

主路径仍然是 Linux VPS：systemd 在 `127.0.0.1:18790` 上跑 `relay/server.py`，Caddy 终结 HTTPS，并用 `handle_path /relay/*` 去掉路径前缀。步骤在 [README.md](../README.md) 的 “Server install” 和 [deploy/CHECKLIST.md](../deploy/CHECKLIST.md)。中文概览在 [README.zh-CN.md](../README.zh-CN.md)。

这一页是不想要那台 VPS 时的三种布局，按考虑顺序排列。三种跑的是同一个服务器，不是另一种产品模式。代码里没有 “Tailscale 模式” 或 “隧道模式”。你只是设置监听地址、端口，以及 box 和 Mac 本来就会拼接的基址。

用第一个和你现有网络匹配的布局。

| | VPS + Caddy | 1. Mac + Tailscale | 2. Box + Tailscale | 3. 隧道 |
|---|---|---|---|---|
| 进程在哪 | VPS | Mac | box | Mac 或 box |
| TLS | Caddy | mac-relay 本身没有 | mac-relay 本身没有 | 隧道，不是 mac-relay |
| 客户端基址 | `https://<host>/relay` | `http://<Mac 的 100.x>:18790` | box 用 `http://127.0.0.1:18790`，Mac 用 `http://<box 的 100.x>:18790` | `https://<tunnel-host>`（隧道直接转到 `:18790` 时不要加 `/relay`） |
| 谁能连上 | 你公布的公网主机名 | tailnet | tailnet | 互联网（Funnel、quick tunnel，或命名隧道的主机名） |
| Mac 睡眠时队列还在 | 在 | 不在 | 在 | 只有服务器不在这台 Mac 上时才在 |

和 VPS 路径的取舍写在每一节里。

## 三种布局都适用的规则

### 你在配置的服务器

`relay/server.py` 是标准库的 `ThreadingHTTPServer`。它读这些环境变量：

| 变量 | 必填 | 默认 | 作用 |
|---|---|---|---|
| `RELAY_SECRET_FILE` | 是 | — | bearer 密钥文件。未设置、读不到、或去掉空白后短于 16 字节时，进程直接退出 |
| `RELAY_DATA_DIR` | 否 | `./data` | SQLite 和任务文件。相对路径跟着进程的工作目录 |
| `RELAY_BIND` | 否 | `127.0.0.1` | 监听地址 |
| `RELAY_PORT` | 否 | `18790` | 监听端口 |

仓库里的 `deploy/mac-relay.service` 故意写着 `RELAY_BIND=127.0.0.1`，这样只有本机反代能连上。默认值不是 tailnet 监听。Tailscale 对端或任何其他机器要连这个进程，启动前必须把 `RELAY_BIND` 设成 `0.0.0.0`，或设成那块网卡的地址。从*另一台*机器上检查。对 `127.0.0.1` 的 `curl` 在绑定 `127.0.0.1` 和 `0.0.0.0` 时都会成功，不能证明绑定已经改掉。

`0.0.0.0` 会在每块 IPv4 网卡上接受连接，包括局域网和公网网卡，不只是 Tailscale。Tailscale ACL 管不到这些网卡。用主机防火墙把 TCP `18790` 限制在 tailnet，或者只绑定 Tailscale 地址。只绑定 `100.x.y.z` 时，`http://127.0.0.1:18790` 连不上；本机客户端也得改用 `http://100.x.y.z:18790`。

服务器不说 TLS。客户端 URL 里的 `https://` 只有在别的东西终结 TLS 再把 HTTP 转过来时才成立（VPS 路径上的 Caddy，或布局 3 的隧道）。

服务器不读 `RELAY_URL`。这个变量只给 box 上的工具用。

### 密钥

生成一份密钥，服务器、box、Mac worker 上放同一份字节。服务器用 `hmac.compare_digest` 比较 `Authorization: Bearer …` 和文件去掉空白后的内容。box 读 `RELAY_SECRET_FILE`。worker 读 `worker.json` 里的 `secret_file`。

```sh
openssl rand -hex 32 > /path/to/secret
chmod 600 /path/to/secret
```

`openssl rand -hex 32` 写出 64 个十六进制字符（32 字节熵）。读取时会去掉空白，所以末尾换行之后仍是 64 字节，满足最短 16 字节。不要提交这个文件。`.gitignore` 只忽略仓库里面名为 `secret` 的文件和 `*.secret`。用 SSH 或 Tailscale 拷贝，不要走 git remote。

### URL 形状

客户端把 `base.rstrip("/") + path` 拼起来。`path` 是 `/health`、`/tasks`、`/tasks/next`、`/results/next` 这些。基址必须是映射到服务器根路径的那个 URL。

- VPS + Caddy：基址是 `https://<host>/relay`，因为 `handle_path /relay/*` 在转到 `127.0.0.1:18790` 之前会去掉这段前缀。检查清单上的冒烟是 `https://<host>/relay/health`。
- 没有反代：基址是源站根，例如 `http://100.x.y.z:18790` 或 `http://127.0.0.1:18790`。不要加 `/relay`。`server.py` 不会剥这个前缀，`/relay/health` 是 404。
- 隧道指向 `http://127.0.0.1:18790`：同一条规则。公网源站就是基址，不加 `/relay`，除非你又在前面放了 Caddy 片段。

`examples/box.env.example` 和 `examples/worker.example.json` 写的是 VPS 的 URL，备用地址端口是 `18791`。那个端口是检查清单里的第二个 Caddy 监听，不是 `server.py` 里的第二个端口。下面的布局不要抄那两个 URL。

### 故障切换

故障切换是可选的，只有设置了第二个基址才会发生。

- box：`RELAY_FALLBACK_URL`。不设置就没有备用。`relay-box-agent` 在主地址连续失败 `RELAY_FAIL_THRESHOLD` 次后切换（默认 3）。失败指连接错误或 HTTP 状态 ≥ 500。HTTP 4xx 会把失败计数清零，不会切换。切换后停在备用地址上，每 `RELAY_COOLDOWN_S` 秒最多探一次 `主地址 + /health`（默认 300），成功才切回。模式只在内存里；重启 agent 会从主地址重新开始。`relay-send` 把阈值强制设成 1，如果这一次失败导致切到备用，会在备用地址上再入队一次。入队按任务 id 幂等。
- Mac worker：`worker.json` 里的 `fallback_url`，加上 `fail_threshold`（默认 3）和 `cooldown_s`（默认 300）。同样是粘住不放。worker 把模式写在 `<state_dir>/conn.json`。重启后会继续用备用地址，直到探测成功，或者你 `touch <state_dir>/reset-primary`，或者 `kill -HUP <worker-pid>`。如果你删了 `fallback_url`，但 `conn.json` 里还写着 `"mode": "fallback"`，下一次请求会去找一个不存在的 URL。回到单地址时删掉 `conn.json`，或 touch `reset-primary`。

单端点布局不要设 `RELAY_FALLBACK_URL` 和 `fallback_url`。不要把它们设成和主地址一样。

备用请求用带 `ProxyHandler({})` 的 opener，因此忽略 `HTTP_PROXY` 和 macOS 系统代理。主地址请求用 urllib 的默认 opener，会跟着环境变量走，在 macOS 上还会跟着系统代理走。worker 的可选字段 `fallback_source_ip` 只给备用的 `http://` 连接绑定源地址（这台 Mac 自己的 Tailscale IPv4），让范围路由可以优先走 Tailscale 的 utun。它不作用于主地址，这个 handler 也不覆盖 `https://` 备用地址。box 客户端没有 `fallback_source_ip`。

[TESTS-REAL.md](../TESTS-REAL.md) 里有一次真实运行：Mac 的系统代理（Shadowrocket）把 `100.64.0.0/10` 抓走了，备用地址全部失败。上面的代理绕过只加在备用 URL 上。写在 `RELAY_URL` 或 `primary_url` 里的 tailnet 地址不会绕过代理。

### 冒烟

`GET /health` 不鉴权，返回 `{"ok": true}`。其他路径没有 bearer 时返回 401。`/health` 开放不等于队列开放。

```sh
curl -sS "http://127.0.0.1:18790/health"          # 在跑服务器的那台机器上
curl -sS -o /dev/null -w '%{http_code}\n' \
  "http://127.0.0.1:18790/tasks/next"             # 期望 401
```

把源站换成那个客户端真正使用的基址。你要的是这段 JSON，不是代理返回的 HTML 404。

box agent 和 worker 的其余部分不变。唤醒文件、工具 argv 模板，以及 `mac/relay-worker.py` 的 LaunchAgent，都在 README 里。工具模板仍以 `examples/worker.example.json` 为准。

## 1. 中继在 Mac 上，box 走 Tailscale

服务器和 worker 都在 Mac 上。box 和 Mac 在同一个 tailnet。box 把任务发到 Mac 的 Tailscale IPv4 地址。mac-relay 本身不需要公网端口。

| | 值 |
|---|---|
| 服务器 | Mac，`RELAY_BIND=0.0.0.0`，`RELAY_PORT=18790` |
| box 的 `RELAY_URL` | `http://<Mac 的 100.x>:18790` |
| box 的 `RELAY_FALLBACK_URL` | 不设置 |
| worker 的 `primary_url` | `http://127.0.0.1:18790`（优先）或同一个 `http://<Mac 的 100.x>:18790` |
| worker 的 `fallback_url` | 不设置 |

worker 优先用 `http://127.0.0.1:18790`。那就是同一个进程，而且主地址不会绕过系统代理。只有确实要绕出这台机器时，worker 才用 Tailscale URL；只要 `RELAY_BIND` 包含那个地址，它就是合法基址。

### 前提

- Mac 上有 Python 3（`/usr/bin/python3`，和 worker plist 用的是同一个解释器）。
- Mac 和 box 上 Tailscale 都在线，tailnet ACL 允许 box 对 Mac 打开 TCP `18790`。ACL 写法是 Tailscale 的：<https://tailscale.com/kb/1018/acls>。
- Mac 保持醒着并在网上。launchd 的 `KeepAlive` 会把死掉的进程拉起来，但不会阻止睡眠。合盖和空闲睡眠会把服务器连同队列一起带走。保持醒着是 macOS 的设置（`caffeinate` 或节能），不是 mac-relay 的开关。
- Mac 上有这份仓库的克隆，用来跑 `relay/server.py`。README 安装的 worker 副本只有 `mac/relay-worker.py`。

### 密钥、监听和端口

```sh
mkdir -p ~/.config/mac-relay/data ~/Library/Logs/mac-relay
openssl rand -hex 32 > ~/.config/mac-relay/secret
chmod 600 ~/.config/mac-relay/secret
```

把这个文件拷到 box 上 `RELAY_SECRET_FILE` 指向的路径，权限 `600`。这台 Mac 上的 worker 可以用同一个 `~/.config/mac-relay/secret`。服务器的数据目录和 worker 的 `state_dir` 要分开。服务器把 `relay.db` 放在 `RELAY_DATA_DIR`。worker 把任务目录放在 `state_dir`。

`RELAY_DATA_DIR` 用绝对路径。LaunchAgent 不设工作目录时，工作目录是 `/`，默认的 `./data` 会变成 `/data`。

`RELAY_BIND=0.0.0.0` 才能同时满足两件事：box 的 `http://<Mac 的 100.x>:18790` 能到达进程，worker 的 `http://127.0.0.1:18790` 也能用。只绑定 Tailscale 地址会丢掉 localhost。保持默认的 `127.0.0.1` 会丢掉 box。

### 运行服务器

先在前台跑，直到冒烟通过。加载 LaunchAgent 之前用 Ctrl-C 停掉，把端口让出来。

```sh
export RELAY_SECRET_FILE="$HOME/.config/mac-relay/secret"
export RELAY_DATA_DIR="$HOME/.config/mac-relay/data"
export RELAY_BIND=0.0.0.0
export RELAY_PORT=18790
/usr/bin/python3 /path/to/mac-relay/relay/server.py
```

标准错误里应有 `mac-relay listening on 0.0.0.0 18790`。在 Mac 上：

```sh
curl -sS http://127.0.0.1:18790/health
curl -sS "http://$(tailscale ip -4):18790/health"
```

两条都必须打出 `{"ok": true}`。如果只有 localhost 成功，进程仍然绑在 `127.0.0.1`。

然后单独做一个 LaunchAgent，不要和 worker 的 agent 混在一起。launchd 不展开 `~` 和 `$HOME`。把 `YOU` 和克隆路径换成你的。

`~/Library/LaunchAgents/com.you.mac-relay.server.plist`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.you.mac-relay.server</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/Users/YOU/src/mac-relay/relay/server.py</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>RELAY_BIND</key><string>0.0.0.0</string>
    <key>RELAY_PORT</key><string>18790</string>
    <key>RELAY_DATA_DIR</key><string>/Users/YOU/.config/mac-relay/data</string>
    <key>RELAY_SECRET_FILE</key><string>/Users/YOU/.config/mac-relay/secret</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/Users/YOU/Library/Logs/mac-relay/server.log</string>
  <key>StandardErrorPath</key><string>/Users/YOU/Library/Logs/mac-relay/server.log</string>
</dict>
</plist>
```

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.you.mac-relay.server.plist
```

以后改了 plist：先 `launchctl bootout gui/$(id -u)/com.you.mac-relay.server`，再 `bootstrap`。如果日志是 `RELAY_SECRET_FILE is required` 或 `secret too short`，`KeepAlive` 会一直重启这个立刻退出的进程，直到文件修好。

worker 按 README 的 “Mac install” 安装，URL 用下面的 `worker.json`，不要用示例文件里的 URL。worker 的 plist 模板是 `mac/com.example.mac-relay.worker.plist`。它的 label 不能和服务器的 label 相同。

### box.env

在 box 上，`~/.mac-relay.env`（README 在启动 `bin/relay-box-agent` 和 `bin/relay-send` 之前 source 的那个文件）：

```sh
RELAY_URL=http://100.x.y.z:18790
RELAY_SECRET_FILE=/path/to/relay/secret
RELAY_RESULTS_DIR=/workspace/relay-results
RELAY_WAKE_DIR=/workspace/relay/wake
```

`100.x.y.z` 是 Mac 上 `tailscale ip -4` 打出来的地址，不是 box 自己的，也不是 `*.ts.net` 名字——除非你故意在前面放了 TLS 代理。没有 `/relay` 后缀，也没有 `:18791`。不要设 `RELAY_FALLBACK_URL`。

唤醒文件和 README 一样：`/workspace/relay/wake/<slug>.url` 和 `<slug>.key`。然后：

```sh
set -a; . ~/.mac-relay.env; set +a
nohup bin/relay-box-agent >> /workspace/relay/agent.log 2>&1 &
```

改了 env 要重启 agent。这个文件里不要放 `RELAY_BIND`；这个布局里 box 不跑服务器。

### worker.json

`~/.config/mac-relay/worker.json`：

```json
{
  "primary_url": "http://127.0.0.1:18790",
  "secret_file": "~/.config/mac-relay/secret",
  "state_dir": "~/Library/Application Support/mac-relay",
  "worker_name": "mac",
  "fail_threshold": 3,
  "cooldown_s": 300,
  "max_backoff_s": 60,
  "tools": {
    "hermes": ["/path/to/hermes", "run", "--prompt-file", "{prompt_file}", "--out", "{outbox}"],
    "deepseek": ["/path/to/deepseek-harness", "--prompt-file", "{prompt_file}", "--inbox", "{inbox}"],
    "codex": ["codex", "exec", "-m", "gpt-6.1-sol", "{prompt}"]
  }
}
```

不要写 `fallback_url` 和 `fallback_source_ip`。`tools` 的值是 `examples/worker.example.json` 里的占位命令，换成你真正要跑的命令。`secret_file` 和 `state_dir` 会展开 `~`。服务器 LaunchAgent 的环境变量不会。

另一个合法的 `primary_url` 是 `http://100.x.y.z:18790`（这台 Mac 自己的 Tailscale 地址）。它得扛得住系统代理。没有特殊原因就用 `127.0.0.1`。

### 故障切换

不用。每一侧只有一个基址。这个 URL 挂了的时候，box 和 worker 会按自己的退避重试长轮询。你自己不设第二个 URL，它们不会切到别处。

只有真的有第二个地址时才值得加（布局 3）。造一个指向同一台机器的备用地址没有用。

### 好处和代价

相对 VPS 路径：没有第二台机器，没有 Caddy，也没有公网 DNS 名。队列只在这台 Mac 醒着并且进程在跑的时候存在。到 box 的流量是 Tailscale 里面的 HTTP，不是检查清单里的 HTTPS 虚拟主机。服务器不是只绑 localhost，所以除了 tailnet ACL，Mac 防火墙也决定局域网上谁还能打开 `18790`。把 `100.64.0.0/10` 抢走的 VPN 或代理，会让 box 连不上 Mac 的 Tailscale 地址；worker 的代理绕过在这里用不上，因为 box 走的是主地址。

### 冒烟

在 Mac 上，“运行服务器” 里的两条 curl 都返回 `{"ok": true}`。

在 box 上，tailnet 在线时：

```sh
curl -sS --max-time 5 http://100.x.y.z:18790/health
curl -sS -o /dev/null -w '%{http_code}\n' --max-time 5 http://100.x.y.z:18790/tasks/next
```

期望 `{"ok": true}` 和 `401`。连接被拒绝说明那个地址上没有进程在听（绑定仍是 localhost，或服务器停了）。超时说明路由、ACL、防火墙，或 Mac 在睡。然后 `bin/relay-send <slug> codex 'ping'` 应打出任务 id，worker 日志里能看到它启动。

## 2. 中继在 box 上，Mac 走 Tailscale

服务器跑在 Linux box 上（本来就跑 `relay-box-agent` 的那台）。Mac 不托管队列，只通过 Tailscale 长轮询。不需要公网端口。

| | 值 |
|---|---|
| 服务器 | box，`RELAY_BIND=0.0.0.0`，`RELAY_PORT=18790` |
| box 的 `RELAY_URL` | `http://127.0.0.1:18790` |
| box 的 `RELAY_FALLBACK_URL` | 不设置 |
| worker 的 `primary_url` | `http://<box 的 100.x>:18790` |
| worker 的 `fallback_url` | 不设置 |

### 前提

- box 上有 Python 3，以及一个可写的数据目录。
- box 和 Mac 上 Tailscale 都在线。Mac 必须被允许对 box 打开 TCP `18790`（<https://tailscale.com/kb/1018/acls>）。
- box 保持在线。只有你希望任务真正执行时，Mac 才需要在 tailnet 上。

### 密钥、监听和端口

密钥文件规则同上。box 上有两套环境：一套给服务器进程，一套给 agent。不要把 `RELAY_URL` 放进服务器环境里，然后指望服务器去读它。

监听地址必须同时包含 localhost（给 agent）和 Tailscale 地址（给 Mac）。这就是 `RELAY_BIND=0.0.0.0`。仓库里的 unit 仍然写着 `127.0.0.1`。你得改掉。只绑定 Tailscale 地址的话，`RELAY_URL=http://127.0.0.1:18790` 会失败。

`0.0.0.0` 也会在 box 的公网或局域网地址上监听。用防火墙把 TCP `18790` 限制在 tailnet，否则那些网卡也能访问 `/health` 并尝试 bearer。

### 运行服务器

**systemd（和 VPS 安装同一个 unit，绑定不同）。** 按 README “Server install” 做到密钥和 `deploy/mac-relay.service`（`User=mac-relay`，`RELAY_DATA_DIR=/var/lib/mac-relay`，`RELAY_SECRET_FILE=/etc/mac-relay/secret`）。这个 unit 设了 `ProtectHome=true`，所以路径必须留在 `/etc` 和 `/var/lib`，不能放到 `/home` 下面。

在 `systemctl enable --now` 之前，改已安装的 unit，用下面这一行换掉 localhost 那一行：

```
Environment=RELAY_BIND=0.0.0.0
```

`sudo systemctl daemon-reload && sudo systemctl restart mac-relay`。不要粘贴 `deploy/Caddyfile.snippet`。这个布局没有 `/relay` 前缀，也不要建检查清单里的 `18791` 端口。

**不用 systemd。** 用你现有的账号跑一个长期进程：

```sh
install -d -m 700 "$HOME/.config/mac-relay"
openssl rand -hex 32 > "$HOME/.config/mac-relay/secret"
chmod 600 "$HOME/.config/mac-relay/secret"
export RELAY_SECRET_FILE="$HOME/.config/mac-relay/secret"
export RELAY_DATA_DIR="$HOME/.config/mac-relay/data"
export RELAY_BIND=0.0.0.0
export RELAY_PORT=18790
nohup python3 /path/to/mac-relay/relay/server.py >> "$HOME/.config/mac-relay/server.log" 2>&1 &
```

无论哪种，在 box 上：

```sh
curl -sS http://127.0.0.1:18790/health
```

必须打出 `{"ok": true}`。这不能证明绑定。从 Mac 上执行 `curl -sS --max-time 5 http://<box 的 100.x>:18790/health` 才能证明。`<box 的 100.x>` 在 box 上用 `tailscale ip -4` 读取。

### box.env

```sh
RELAY_URL=http://127.0.0.1:18790
RELAY_SECRET_FILE=/etc/mac-relay/secret
RELAY_RESULTS_DIR=/workspace/relay-results
RELAY_WAKE_DIR=/workspace/relay/wake
```

如果服务器是不用 systemd 的那个进程，`RELAY_SECRET_FILE` 改成 `$HOME/.config/mac-relay/secret`。agent 和服务器都要能读这个文件。不要设 `RELAY_FALLBACK_URL`。

按 README source 这个文件并启动 `bin/relay-box-agent`。唤醒文件不变。

### worker.json

按 README “Mac install” 安装。`~/.config/mac-relay/worker.json` 的 `tools` 从 `examples/worker.example.json` 抄过来（这里不重复）：

```json
{
  "primary_url": "http://100.x.y.z:18790",
  "secret_file": "~/.config/mac-relay/secret",
  "state_dir": "~/Library/Application Support/mac-relay",
  "worker_name": "mac",
  "fail_threshold": 3,
  "cooldown_s": 300,
  "max_backoff_s": 60,
  "tools": {}
}
```

把 `100.x.y.z` 换成 box 的 Tailscale 地址，把 `tools` 换成真实的 argv 模板。空的 `tools` 对象能让 worker 启动，但每个任务都会失败（`no command template`）。不要写 `fallback_url` 和 `fallback_source_ip`。如果旧配置有过备用地址，删掉残留的 `<state_dir>/conn.json`。

这个 `primary_url` 是 `http://100.x`，走默认 opener，会跟着 Mac 的系统代理。如果 Shadowrocket 或其他 VPN 把 `100.64.0.0/10` 送进代理，worker 就到不了 box。在那个代理里排除 tailnet 是操作系统的设置。mac-relay 不会为主地址做这件事。没有备用地址时，`fallback_source_ip` 不起作用。

### 故障切换

不用。Mac 只有一个 URL。box 访问的是 localhost。Mac 离线时，服务器把任务留在 SQLite 队列里；worker 再次长轮询之前不会执行。box 离线时，没有别的地方可以入队。

### 好处和代价

相对 VPS 路径：一直开着的是你已经有的 box，不是一台 VPS；防火墙和 ACL 收紧时仍然没有公网入口。你放弃的是 Caddy 的 HTTPS 虚拟主机和 `/relay` 前缀。Mac 通过 Tailscale 里的明文 HTTP 访问 box，而且这个地址是 worker 的主地址，所以系统代理可以把它打断（见上面的共用规则）。box 必须在 tailnet 上可达。Mac 睡眠或离线时任务留在队列里，这一点和 VPS 路径一样；和布局 1 不同，布局 1 里睡着的 Mac 就是停掉的服务器。

### 冒烟

在 box 上：`curl -sS http://127.0.0.1:18790/health` → `{"ok": true}`。

在 Mac 上：`curl -sS --max-time 5 http://100.x.y.z:18790/health` → `{"ok": true}`，同一 URL 加上路径 `/tasks/next` 且不带 bearer → `401`。然后从 box 上 `relay-send`，应被 worker 领走。

## 3. 本机中继加隧道

服务器仍然跑在 Mac 或 box 上，绑定在隧道能够到的地址。Cloudflare Tunnel 或 Tailscale Funnel 提供一个 HTTPS URL。box 不能加入 tailnet，或者你需要一个稳定的 HTTPS 基址、又不走 VPS + Caddy 时，用这个布局。

服务器仍然没有 TLS，也没有 `/relay` 路由。隧道终结 TLS，再转发到 `http://127.0.0.1:18790`。客户端基址是隧道的源站，不加 `/relay`，也不加 `:18790`，前提是隧道公布的是服务器根路径。如果你又把 `deploy/Caddyfile.snippet` 放在前面，基址要加上 `/relay`，并且隧道必须指向 Caddy，而不是 Python 端口。下面的步骤不这样做。

| | 只有隧道 | 隧道和 Tailscale 都能到 |
|---|---|---|
| 服务器绑定 | `127.0.0.1`（默认） | `0.0.0.0`，这样 `100.x:18790` 也能到 |
| 和服务器在同一台机器上的客户端 | `http://127.0.0.1:18790` | `http://127.0.0.1:18790` |
| 远端客户端的主地址 | `https://<tunnel-host>` | `https://<tunnel-host>` |
| 远端客户端的备用 | 不设置 | `http://<服务器的 100.x>:18790` |

box agent 和 Mac worker 里，谁和服务器在同一台机器上，谁就用 localhost。这条连接不依赖隧道。另一侧用公网 URL。远端能走 Tailscale 时，私网地址比公网 URL 更好；下面的故障切换一行就是两边都留着的做法，和代码一致（公网做主地址，tailnet 做备用，备用请求跳过代理）。

### 前提

- 一台已经按布局 1 或布局 2 跑起来的服务器，或者同一条 `python3 relay/server.py` 命令，一开始用默认绑定。
- `cloudflared` 或 Tailscale CLI，按那个产品自己的文档安装。这个仓库不带这两个二进制。
- bearer 密钥，规则见上面。隧道不会替 mac-relay 做鉴权。`/health` 是公开的。其他路径都要 bearer。

先在那台机器上确认 `curl -sS http://127.0.0.1:18790/health` 返回 `{"ok": true}`，再把隧道指到 localhost。

如果远端还要把 `http://<服务器的 100.x>:18790` 当备用，把 `RELAY_BIND` 改成 `0.0.0.0`，并从远端重新检查这个 URL。下面写的 Funnel 和 `cloudflared` 连的是 `127.0.0.1`。进程如果只绑在 Tailscale 地址上，收不到它们的流量。`0.0.0.0` 两边都覆盖。

### Cloudflare Tunnel

文档：<https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/>。要一个重启后还在的主机名，看 <https://developers.cloudflare.com/tunnel/setup/>。

quick tunnel 跑在和服务器同一台机器上。把 Cloudflare 示例里的端口换成中继的端口：

```sh
cloudflared tunnel --url http://127.0.0.1:18790
```

`cloudflared` 会打出一个 `https://<random>.trycloudflare.com` URL。这个源站就是客户端基址。进程停了它就失效，下次启动主机名会变。Cloudflare 把 quick tunnel 标成测试工具：没有可用性保证，同时进行的请求超过 200 个会返回 HTTP 429。429 是 4xx，box agent 和 worker 把它当成应用层响应，不算故障切换的失败。它们记一条错误，按退避重试，不会因为 429 换 URL。

不要在这个服务前面加 Cloudflare quick tunnel 的 `--allowed-mail`。Cloudflare 的文档把它描述成浏览器一次性 PIN，并写明不支持非交互客户端。`relay-send`、`relay-box-agent` 和 `relay-worker.py` 都是非交互的。它们只发 `Authorization: Bearer`。

要稳定的名字，在 Cloudflare 的控制台或 API 里建一个命名隧道，把公布的服务 URL 设成 `http://127.0.0.1:18790`。你绑上去的主机名就是基址。登录、DNS 和 Cloudflare 的配置文件在他们的安装文档里；这一页除了这个服务 URL，不额外加参数。

quick tunnel 不支持 Server-Sent Events。mac-relay 不用 SSE。它把长轮询握着大约 50 秒（worker 和 box agent 里是 `wait=50`；服务器把 `wait` 封顶在 55）。这个数字写死在代码里。没有环境变量或 `worker.json` 字段可以改。隧道如果提前关掉这些连接，客户端把它当成连接错误，退避之后再试（worker 的上限是 `max_backoff_s`，默认 60；`bin/relay-box-agent` 里的上限是 60 秒）。

### Tailscale Funnel

文档：<https://tailscale.com/kb/1223/funnel>。当前文档用下面这条命令暴露本地端口：

```sh
tailscale funnel 18790
```

CLI 会打出一个公网的 `https://<node>.<tailnet>.ts.net`，它的 `/` 转到 `http://127.0.0.1:18790`。这个源站不加路径后缀、不加端口，就是客户端基址。文档里的命令在前台跑（`Press Ctrl+C to exit`）。按那台机器上的 CLI 实际支持的方式让它一直跑；加参数之前先看 `tailscale funnel --help`。Tailscale 1.52 改过这个 CLI。

和这里有关的要求与限制，来自那些文档：

- 打开 MagicDNS 和 HTTPS 证书，并且允许这个节点使用 Funnel（管理控制台可以打开）。
- 公网监听端口是 443、8443 和 10000。当前示例里 `tailscale funnel 18790` 把本地端口公布在 HTTPS 源站上，所以客户端不要在 `https://` URL 上写 `:18790`。
- 互联网上拿到这个 URL 的人都能打开它。Funnel 不是受 ACL 限制的 tailnet 地址。
- 在 macOS 上，Funnel 需要开源版本的 Tailscale。沙箱里的 Mac App Store 客户端不能做 Funnel。
- Funnel 有不可配置的带宽限制。

Tailscale Serve（`tailscale serve 18790`，<https://tailscale.com/kb/1242/tailscale-serve>）转发同一个本地端口，但只在 tailnet 内部。布局 1 和 2 不需要它；它们直接用 `http://100.x:18790`。Serve 和 Funnel 不能同时占同一个公网端口。后执行的命令生效；在那个端口上执行 `serve` 会把它变成私有的。

### box.env 和 worker.json

**服务器在 Mac 上，box 不在 tailnet 里。** box 没有 `100.x` 路由，所以它用隧道。worker 和服务器在一起，所以它用 localhost。

box 的 `~/.mac-relay.env`：

```sh
RELAY_URL=https://<tunnel-host>
RELAY_SECRET_FILE=/path/to/relay/secret
RELAY_RESULTS_DIR=/workspace/relay-results
RELAY_WAKE_DIR=/workspace/relay/wake
```

worker 的 `primary_url`：

```json
"primary_url": "http://127.0.0.1:18790"
```

不设 `RELAY_FALLBACK_URL`，也不设 `fallback_url`。`<tunnel-host>` 是 quick tunnel 的主机名、命名隧道的主机名，或 `<node>.<tailnet>.ts.net`，没有 `/relay`，也没有端口。

**隧道和 Tailscale 都能到达服务器。** 远端客户端（服务器在 Mac 上时是 box；服务器在 box 上时是 Mac worker）：

```sh
RELAY_URL=https://<tunnel-host>
RELAY_FALLBACK_URL=http://100.x.y.z:18790
RELAY_FAIL_THRESHOLD=3
RELAY_COOLDOWN_S=300
```

```json
{
  "primary_url": "https://<tunnel-host>",
  "fallback_url": "http://100.x.y.z:18790",
  "fallback_source_ip": "100.a.b.c",
  "secret_file": "~/.config/mac-relay/secret",
  "state_dir": "~/Library/Application Support/mac-relay",
  "worker_name": "mac",
  "fail_threshold": 3,
  "cooldown_s": 300,
  "max_backoff_s": 60,
  "tools": {}
}
```

`100.x.y.z` 是跑服务器那台机器的 Tailscale 地址。`100.a.b.c` 是这台 Mac 自己的 Tailscale 地址，只写在 worker 上，而且只在 `fallback_url` 是 `http://` 的时候写。不要把 `examples/worker.example.json` 里那段说明文字抄进 `fallback_source_ip`。和服务器在同一台机器上的客户端仍然只用 `http://127.0.0.1:18790`。空的 `tools` 对象用 `examples/worker.example.json` 里的内容换掉。

这样拆是有意的：tailnet URL 放在备用地址上，因为那条路径会跳过系统代理。把 `http://100.x:18790` 放进 `primary_url`、把隧道放进 `fallback_url`，就和代码里的代理绕过反了。

即使服务器就跑在 box 上，也可以把 box 指到 `https://<tunnel-host>`。那样会从隧道绕一圈回来。和服务器在一起的 agent 优先用 `http://127.0.0.1:18790`，这样隧道断了也不会挡住本机入队。

### 故障切换

没有第二行示例里的两个 URL，就不要设置。远端客户端两个都有时，用平时的规则：隧道 URL 上连续 3 次连接错误或 HTTP 5xx，然后粘在 `http://100.x:18790` 上，`cooldown_s` 之后再探主地址的 `/health`。`relay-send` 第一次失败就会试备用。隧道和 tailnet 一起断了，没有第三个地址。

本机的 localhost 客户端不做故障切换。如果你以前存过备用模式，去掉 `fallback_url` 之后清掉 `<state_dir>/conn.json`，或 touch `reset-primary`。

### 好处和代价

相对 VPS 路径：你不用跑 VPS，也不用 Caddy，同时能拿到一个 HTTPS URL。TLS 止于 Cloudflare 或 Tailscale 的 Funnel 代理；`server.py` 在 localhost 上说的仍是 HTTP。加固靠的是隧道产品加上 bearer 密钥，不是 `deploy/mac-relay.service` 里的 `ProtectSystem` 和专用用户，除非你另外装了那个 unit。Funnel 和 Cloudflare quick tunnel 对拿到 URL 的人是开放的。密钥是必须的，也是这些客户端发送的唯一鉴权。quick tunnel 的主机名不稳定。macOS 上的 Funnel 需要开源客户端。这不能代替检查清单里的公网虚拟主机，也不能代替 `deploy/Caddyfile.snippet` 里的请求体大小限制和长轮询超时。

队列在哪，跟跑 `server.py` 的机器走。Mac 睡着时：服务器在 Mac 上则队列停（布局 1），服务器在 box 上则队列还在（布局 2）。隧道断了只挡住远端客户端。localhost 客户端仍然能跟进程说话。

### 冒烟

在服务器那台机器上：

```sh
curl -sS http://127.0.0.1:18790/health
```

在任何应该走隧道的机器上，用打出来的源站，不要多加路径：

```sh
curl -sS --max-time 20 "https://<tunnel-host>/health"
curl -sS -o /dev/null -w '%{http_code}\n' --max-time 20 "https://<tunnel-host>/tasks/next"
```

期望 `{"ok": true}` 和 `401`。如果 `/health` 是 HTML 404，隧道没有指向服务器根路径，或者客户端加了服务器上根本不存在的 `/relay` 前缀。如果配了 tailnet 备用，先从远端确认 `curl -sS --max-time 5 http://100.x.y.z:18790/health` 也返回 `{"ok": true}`，再设置 `RELAY_FALLBACK_URL` / `fallback_url`。然后让 `relay-send` 走 `RELAY_URL`。

# 多通道：主中继与备用中继

mac-relay 可以同时跑在多个中继服务器上，例如 box 上经 Tailscale 直连的中继（主）和 VPS 上的中继（备）。没有会“卡住”的粘性切换状态：每个组件同时连所有通道，靠任务 id 把各通道上的副本对应起来。

| 组件 | 配置 | 多通道时的行为 |
|---|---|---|
| `relay-send` | `RELAY_URL`（主）、`RELAY_FALLBACK_URL`（逗号分隔，按顺序） | 发到第一个能入队的通道。`RELAY_HANDOFF_S`（默认 20 秒）内 Mac 没接单，就把同一个任务 id 发到下一个通道，并在前一个通道上撤回（仍在排队时）。总等待 `RELAY_STARTED_WAIT_S`（默认 45 秒），轮询所有发过的通道。 |
| `relay-box-agent` | 同上 | 同时长轮询每个通道的 `/results/next`。同一任务 id 只交付一次（下载文件、写 `result.json`、唤醒一次），来自其他通道的副本只 ack。 |
| Mac worker | `worker.json` 的 `endpoints`（有序列表，第一个为主） | 同时长轮询每个通道的 `/tasks/next`。结果先回到接单的通道（主优先）；这些通道连续失败 `mirror_after_s`（默认 60 秒）后，把任务镜像到其他通道并在那里交付。 |

通道状态变化只记一次日志：`channel <name> down: …` / `channel <name> recovered after N s`。连接错误、超时、HTTP 5xx 记为 down；下一次请求成功即恢复，无需手动重置。

## 示例：box 中继经 Tailscale 为主，VPS 中继为备

box（`box.env`）：

```sh
RELAY_URL=http://127.0.0.1:18790
RELAY_FALLBACK_URL=https://relay.example.com/relay,http://100.x.y.z:18791
```

Mac（`worker.json`）：

```json
"endpoints": [
  {"name": "box",    "url": "http://100.a.b.c:18790"},
  {"name": "vps-ts", "url": "http://100.x.y.z:18791"},
  {"name": "vps",    "url": "https://relay.example.com/relay"}
],
"mirror_after_s": 60
```

`100.a.b.c` 是 box 的 Tailscale 地址（box 中继用 `RELAY_BIND=127.0.0.1,100.a.b.c`，不要 `0.0.0.0`）。`100.x.y.z:18791` 是 VPS 上 Caddy 的 Tailscale 监听。访问 `100.x`、`*.ts.net`、`127.*`、`localhost` 时所有通道都绕过 `HTTP(S)_PROXY` 和 macOS 系统代理。旧配置的 `primary_url` / `fallback_url` 仍可用，等价于两项的列表。

## 至多执行一次、不丢任务

- **Mac 上的台账。** 任务目录就是台账。某任务 id 第一次被认领时在锁内写 `task.json`；之后任何通道再认领同一个 id，只确认（`/started`），日志 `duplicate claim … not run again`。前提是只有一台 worker 主机。
- **晚到的副本。** 来不及撤回（已被认领）的副本撞上台账，只确认不执行。
- **只唤醒一次。** box-agent 按任务加锁，在 POST webhook 前写 `.wake`。之后的副本（镜像、收尾、重投）看到 `.wake` 只 ack。
- **陈旧行。** 镜像后，原通道上任务仍显示 running。worker 在 `closeout_after_s`（默认 600 秒）后补一个不带文件的结果，box-agent 只 ack。
- **进程丢失**（如重启）只上报一次 `failed`。
- **升级前的目录。** 旧版 worker 留下、从未启动进程的任务目录不会被延迟执行；只有带 v2 `claimed` 标记的目录会续传附件。

## 大文件

- **结果（Mac → box）。** 分块 `PUT …?offset=&total=&sha256=`（8 MiB）。服务器保留 `.upload` 半成品；offset 不对返回 `409 {"partial": n}`；文件对外可见前校验 sha256（不符返回 `422` 并删除半成品）。`GET /uploads/{id}/{name}?kind=` 返回续传点，断线、worker 重启、服务器重启后都能续传。发送方卡住但不断开时，`RELAY_CHUNK_STALL_S`（默认 30 秒）后释放。续传点比上一次大说明通道慢但在工作，会重置镜像计时，不会为它放弃慢通道。
- **下载（box-agent、worker 附件）。** 先写 `<name>.partial`，用 `Range` 续传，按服务器记录的大小和 sha256 校验后才改名，半成品永远不会以正式文件名出现。
- **附件（box → Mac）。** v2 服务器上 `relay-send` 先以 `staged` 建任务，逐个分块 PUT `--file`，再 commit；全部附件完整且校验通过前任务不可被认领。旧服务器仍用 multipart（16 MiB 上限）。
- **长 prompt。** `relay-send … -` 从 stdin 读，`--prompt-file` 从文件读。worker 对超过 `stdin_prompt_over[tool]` 字节的 prompt 改走 stdin（prompt 参数为 `"-"`），不放 argv。

## 协议 v2 与旧服务器

`GET /status`（需鉴权）列出 `features`：`status, chunked, range, staged, withdraw, mirror, exclude, relpath`。旧服务器返回 404，客户端自动退回旧调用：每个文件一次流式 PUT（不能续传）、multipart 附件最多 16 MiB、结果子目录路径被压平（`a/b.txt` → `a__b.txt`）、不能撤回（由台账去重）。新旧混用可以工作；要在某个通道上获得续传和 sha256，就升级那台服务器。

## 故障情形

| 情形 | 结果 |
|---|---|
| 入队前主通道已挂 | `relay-send` 发到下一个通道，worker 本来就在轮询它。 |
| box 能连主通道、Mac 连不上 | `RELAY_HANDOFF_S` 后转交，主通道副本撤回。 |
| 任务运行中主通道挂掉 | `mirror_after_s` 后结果走备用；只唤醒一次；主通道的行稍后收尾。 |
| 上传中途主通道断开 | 重试时从服务器的续传点继续；只有 `mirror_after_s` 内毫无进展才镜像。 |
| worker 在任务或上传中途重启 | 任务进程独立继续；新 worker 续传。 |
| box-agent 下载中途重启 | `.partial` 用 `Range` 续传；校验后才唤醒。 |
| 同一任务 id 出现在两个通道 | 只执行一次（台账）；第二份只确认；唤醒一次。 |
| 所有通道都挂 | 结果在 Mac 上退避重试，任务留在各自队列；不丢。 |
| 服务器卡死 | 每个请求有硬超时和停滞看门狗；该通道标记为 down。 |

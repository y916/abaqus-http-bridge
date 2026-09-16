# Abaqus HTTP Bridge

在 **Abaqus/CAE 内部**常驻一个 HTTP 服务，让你用任意语言/任意工具（Python、curl、PyCharm、CI 脚本、AI agent）远程驱动正在运行的 CAE 会话：执行 Abaqus Python、建模、提交作业、读 ODB、抓视口截图。

**核心特点：不冻结 CAE 窗口。** 空闲时对 GUI 零负担，只有真正执行命令时才占用——和你手敲 CAE 命令行一样。

- **版本**：1.3.5
- **许可**：MIT
- **要求在下方「兼容性」一节**

---

## 1. 它是什么

```
 你的客户端（PyCharm / 脚本 / curl / AI agent）
        │  HTTP  http://127.0.0.1:49321
        ▼
┌──────────────── Abaqus/CAE ─────────────────┐
│  一个插件包，两个进程半边：                    │
│                                              │
│  abaqus_http_bridge.py         ← 内核进程     │
│    GET  /health  探活（不碰 Abaqus 对象）    │
│    GET  /status   会话信息                   │
│    POST /execute  在活内核里 exec Python      │
│    POST /stop     关闭                       │
│                                              │
│  abaqus_http_bridge_plugin.py  ← GUI 进程     │
│    菜单：启用 / 重启 / 停止 / 状态 / 端点      │
│    定时器：唯一能让内核持续服务的方式          │
└──────────────────────────────────────────────┘
```

端点一共 4 条，另有两个别名：`/health`（别名 `/ready`，**唯一免 token 的路由**）、
`/status`（别名 `/ping`）、`/execute`、`/stop`。完整定义见第 6 节。

两个文件分工明确：

| 文件                           | 跑在哪                 | 为什么                                         |
| ------------------------------ | ---------------------- | ---------------------------------------------- |
| `abaqus_http_bridge.py`        | **内核** `ABQcaeK.exe` | 只有内核里有 `mdb` / `session`                 |
| `abaqus_http_bridge_plugin.py` | **GUI** `ABQcaeG.exe`  | `abaqusGui` 只能在这里导入；定时器也只能在这里 |

内核侧不导入 `abaqusGui`（在内核里会抛 `ImportError: Module abaqusGui can only be used in Abaqus/CAE GUI`），所以它既能被 GUI 插件加载，也能被 `script=` 启动脚本加载。

---

## 2. 兼容性

| 项目       | 要求                                                                               |
| ---------- | ---------------------------------------------------------------------------------- |
| Abaqus     | **2024 或更新**（需要 Python 3）。已在 **Abaqus 2024（Python 3.10.5）** 上完整验证 |
| 操作系统   | Windows（已验证）。理论上 Linux 也可，但 GUI 菜单与定时器部分未验证                |
| 第三方依赖 | **无**。全部使用 Abaqus 2024 自带 Python 的标准库                                  |

---

## 3. 安装

### 3.1 自动安装（推荐）

```bat
:: Windows
install.bat
```

```bash
# Linux / WSL / macOS
python3 install.py
```

安装器把**一个目录**放到 Abaqus 插件根下：

```
%USERPROFILE%\abaqus_plugins\abaqus_http_bridge\
```

（从 WSL 运行时会自动识别并装进 **Windows** 用户目录，不会误装到 Linux 家目录。）

可选参数：

```bat
install.bat --plugins-dir D:\abaqus_plugins
python install.py --dry-run
```

### 3.2 手动安装

把压缩包里的 `plugin/` 整个目录复制成
`%USERPROFILE%\abaqus_plugins\abaqus_http_bridge\`，然后**重启 Abaqus/CAE**。

> 为什么一个目录就够：Abaqus 只**扫描** `abaqus_plugins` 并导入各包的 `*_plugin.py`，
> 它**不会**把该目录加进任何进程的 `sys.path`（实测：内核 `sys.path` 里没有
> `abaqus_plugins`，直接 import 该目录下的模块两个进程都失败）。够用的原因是被测实到
> 的两点：GUI 插件**有** `__file__`，可以把自身目录 insert 给内核；而
> `script=` 脚本**没有** `__file__` 但有 `sys.argv`，`bootstrap.py` 据此找到自己。

### 3.3 验证安装

重启 CAE 后检查 `%USERPROFILE%\.abaqus-http-bridge\gui_plugin.json`：

```json
{
  "plugin": "abaqus_http_bridge",
  "version": "1.3.5",
  "plugin_dir": "C:\\Users\\me\\abaqus_plugins\\abaqus_http_bridge",
  "config_source": "...\\bridge_config.json",
  "config_enabled": false,
  "kernel_bootstrap_sent": true,
  "pump_armed": true,
  "menu_namespace": "__main__"
}
```

（节选；实际文件里还有 `gui_plugin_loaded`、`bridge_dir`、定时器间隔、`buttons` 等字段，
引导失败时才会多出 `error`。）

## 4. 配置

编辑插件目录下的 **`bridge_config.json`**：

```json
{
  "_comment": ["Abaqus HTTP Bridge configuration.", "..."],
  "enabled": false,
  "host": "127.0.0.1",
  "port": 49321,
  "port_candidates": [
    8791, 18152, 33001, 34001, 49321, 51234, 56789, 52345, 60001, 65000
  ],
  "token": "",
  "allow_port_fallback": true,
  "timeout": 120,
  "max_body_bytes": 8388608,
  "log_requests": true,
  "state_dir": ""
}
```

| 键                    | 默认        | 说明                                                                                                        |
| --------------------- | ----------- | ----------------------------------------------------------------------------------------------------------- |
| **`enabled`**         | `false`     | **`true` 时：CAE 一启动就自动打开端口**。GUI 插件和 `script=` / `noGUI=` 启动脚本都认这个开关，不用点菜单、不用设任何环境变量。`false` 时端口默认关闭，只能手动开 |
| **`port`**            | `49321`     | 主端口                                                                                                      |
| `port_candidates`     | 见上        | 主端口被占用时依次尝试                                                                                      |
| **`token`**           | `""`        | 非空时，**除 `/health`、`/ready` 外的每个请求**都必须带 `X-Bridge-Token` 头，否则 401                       |
| `host`                | `127.0.0.1` | 主机，**谨慎改成 `0.0.0.0`**                                                                                |
| `allow_port_fallback` | `true`      | 设 `false` 则主端口不可用时报错，而不是换端口                                                               |
| `timeout`             | `120`       | 默认超时秒数（仅作参考/透传，**不会中断**已开始的执行）                                                     |
| `max_body_bytes`      | `8388608`   | 请求体上限                                                                                                  |
| `log_requests`        | `true`      | 是否把每个请求写进 `bridge.log`                                                                             |
| `state_dir`           | `""`        | 空 = `~/.abaqus-http-bridge`                                                                                |

> 这个文件的键与代码里的 `DEFAULT_CONFIG` 一一对应，两边要一起改。安装器只在目标位置
> **没有**该文件时才写入，永远不会覆盖你改过的版本。

**优先级**：环境变量 > `bridge_config.json` > 内置默认。配置文件查找顺序：
`ABAQUS_HTTP_CONFIG` → 插件目录 → `~/.abaqus-http-bridge/`。

改完**重启 Abaqus/CAE** 生效。

### 开启自动启动的例子

```json
{ "enabled": true, "port": 8791, "token": "mysecret" }
```

重启 CAE 后端口自动打开，客户端需带 token：

```bash
python client/bridge_client.py --token mysecret status
```

## 5. 使用

### 5.1 开启端口（**默认关闭**）

端口**不会**随 Abaqus 自动打开。必须显式请求：

| 方式         | 操作                                                                            |
| ------------ | ------------------------------------------------------------------------------- |
| **图形界面** | `Plug-ins > Abaqus HTTP Bridge > Start Bridge`                                  |
| **启动脚本** | `abaqus cae script=<BRIDGE_DIR>\bootstrap.py`（需设 `ABAQUS_HTTP_AUTOSTART=1`，或让配置里的 `enabled` 为 `true`） |

> **`script=` 和 `noGUI=` 的区别在这里很关键**：`ABAQUS_HTTP_BLOCKING` 默认为 `0`，
> 意思是「只开监听，由 GUI 插件的定时器泵驱动」。可见的 GUI 会话就该用这个默认值。
> **无头 `noGUI=` 会话没有定时器可泵，必须显式设 `ABAQUS_HTTP_BLOCKING=1`**，否则端口
> 连得上但永远不应答——bootstrap 遇到这种情况会打印一条警告。

默认端口 **49321**。若被占用，会自动尝试候选列表并**把实际端口写入状态文件**：

```
%USERPROFILE%\.abaqus-http-bridge\bridge.json
```

### 5.2 关闭

- 菜单：`Plug-ins > Abaqus HTTP Bridge > Stop Bridge`
- 客户端：`POST /stop`
- 或直接把 CAE 关掉

### 5.3 用附带的独立客户端

不需要任何第三方库：

```bash
# 会话信息（模型 / 作业 / 视口）
python client/bridge_client.py status

# 探活（不碰 Abaqus 对象，会话再忙也立刻返回）
python client/bridge_client.py health

# 执行一行代码
python client/bridge_client.py exec "print(mdb.models.keys())"

# 执行一个脚本文件
python client/bridge_client.py run myscript.py
```

`--host` / `--port` / `--token` 都是覆盖项：默认从状态文件里读，显式传了就以传的为准。

作为库使用：

```python
from client.bridge_client import AbaqusBridge

ab = AbaqusBridge(port=49321)          # 或 AbaqusBridge.from_state_file()

print(ab.status()["models"])           # ['Model-1']
print(ab.execute("result = list(mdb.models.keys())")["return_value"])

# 长时间命令请显式给超时
ab.execute("mdb.models['Model-1'].parts['P'].generateMesh()", timeout=300)
```

### 5.4 用 curl

```bash
curl http://127.0.0.1:49321/health

curl -X POST http://127.0.0.1:49321/execute \
  -H "Content-Type: application/json" \
  -d '{"code": "from abaqus import mdb\nresult = sorted(mdb.models.keys())", "timeout": 30}'
```

---

## 6. HTTP API 参考

所有响应都是 JSON。

### `GET /health` — 探活

**不触碰任何 Abaqus 对象**，因此即使会话繁忙也能立即返回。适合做健康检查。

**这是唯一不需要 token 的路由**（`/ready` 是它的别名），因为健康检查不该要凭证。

```json
{
  "ok": true,
  "version": "1.3.5",
  "transport": "http",
  "port": 49321,
  "pid": 27420,
  "thread": "MainThread",
  "touches_abaqus": false
}
```

### `GET /status` — 会话信息

别名 `/ping`。需要 token（若已设置）。

```json
{
  "python": "3.10.5 ...",
  "executable": "...ABQcaeK.exe",
  "pid": 27420,
  "cwd": "C:\\Users\\me\\abaqus_bridge",
  "abaqus_version": "None",
  "models": ["Model-1"],
  "viewports": ["Viewport: 1"],
  "jobs": [],
  "bridge": {
    "version": "1.3.5",
    "transport": "http",
    "port": 49321,
    "running": true,
    "mode": "listener-only (GUI timer pumps)",
    "processed": 10,
    "uptime_seconds": 340,
    "requires_token": false
  }
}
```

> **`bridge.mode` 很有用**：它告诉你正在跟哪个 Abaqus 说话。
> `listener-only (GUI timer pumps)` = 你眼前那个可见的 GUI 会话；
> `headless-blocking-serve` = 一个无窗口的独立内核（由 `abaqus_launch_cae` 之类拉起）；
> `adopted (served by pid N)` = 端口上已经有一个别的进程在服务，本次没有新建监听。
> 配合 `pid` 可与任务管理器核对。

### `POST /execute` — 执行 Python

请求：

```json
{
  "code": "from abaqus import mdb\nresult = sorted(mdb.models.keys())",
  "timeout": 60
}
```

- `code`（必填）：要执行的 Python 源码
- `timeout`（选填）：秒，默认 120，**仅作记录/客户端参考**，服务端不会中断已开始的执行。
  传非数字会得到 400 而不是 500

**成功**（HTTP 200）：

```json
{
  "ok": true,
  "id": null,
  "result": {
    "ok": true,
    "return_value": ["Model-1"],
    "has_result": true,
    "stdout": "",
    "stderr": "",
    "error_type": "None",
    "core_error": "None"
  }
}
```

**内核报错**（HTTP 200，但 `result.ok == false`）：

```json
{
  "ok": true,
  "result": {
    "ok": false,
    "return_value": null,
    "has_result": false,
    "error_type": "builtins.KeyError",
    "core_error": "'NoSuchModel'",
    "stdout": "",
    "stderr": "",
    "recovery": {
      "parent_object_path": "parts",
      "possible_keys": [],
      "missing_key": "NoSuchModel"
    },
    "code_excerpt": "     1| from abaqus import mdb\n>>   2| result = mdb.models['NoSuchModel']...",
    "traceback_tail": "..."
  }
}
```

**请求体非法**（HTTP 400）：`{"ok": false, "error": "body must be {\"code\": \"...\", ...}"}`

### 关键语义

**① 用 `result` 变量返回数据**

```python
from abaqus import mdb
result = sorted(mdb.models.keys())      # ← 这个值会回到客户端
```

单表达式会直接求值并返回；多行代码则取 `result` 变量的值。

`result` **在每次执行前都会被清空**，所以没设置它的请求返回 `null`，而不会带上一次
调用的残留值。`has_result` 字段告诉你到底属于哪种情况——`return_value` 是 `null` 时，
`has_result: false` 表示代码压根没设 `result`。

**② 只有 `print()` 的内容会进 `stdout`**，返回值走 `return_value`。

**③ 命名空间跨请求保留**

多次 `/execute` 共享一个命名空间，所以 Python 局部变量是**持续存在**的：

```bash
# 第一次
{"code": "counter = 0"}
# 第二次 → 2
{"code": "counter = counter + 2\nresult = counter"}
```

这也意味着前一次调用留下的名字可能影响后一次——建议用明确的变量名，或主动 `del`。

**④ 请求串行执行**

内核状态（`mdb` / `session`）不是线程安全的，所以所有 `/execute` 通过一把锁排队。并发请求会依次处理，不会交错。

**⑤ 长命令会占用 CAE 窗口**

这是**固有限制**：内核执行期间 GUI 会等它返回。和手敲 CAE 命令完全一样。

### `POST /stop` — 关闭

```json
{ "ok": true, "result": { "success": true, "message": "stop requested" } }
```

---

## 7. 环境变量

在内核桥启动前设置（GUI 插件会读同名变量）。

| 变量                       | 默认                    | 说明                                          |
| -------------------------- | ----------------------- | --------------------------------------------- |
| `ABAQUS_HTTP_HOST`         | `127.0.0.1`             | **不要改成 `0.0.0.0`**，见第 9 节安全         |
| `ABAQUS_HTTP_PORT`         | `49321`                 | 主端口；被占用时走候选列表                    |
| `ABAQUS_HTTP_TOKEN`        | 空                      | 若设置，请求必须带 `X-Bridge-Token` 头（`/health`、`/ready` 除外）。**设为空串可清掉配置文件里的 token** |
| `ABAQUS_HTTP_AUTOSTART`    | 取自 `enabled`          | 覆盖配置里的 `enabled`；两者都没开就是不开    |
| `ABAQUS_HTTP_BLOCKING`     | `0`                     | `1`=阻塞式服务（无头必须用）；`0`=只开监听、由 GUI 定时器泵 |
| `ABAQUS_HTTP_TIMEOUT`      | 取自 `timeout`          | 覆盖默认超时秒数                              |
| `ABAQUS_HTTP_SLICE`        | `0.05`                  | 每次 `handle_request` 的时间片上限（秒）      |
| `ABAQUS_HTTP_STATE_DIR`    | `~/.abaqus-http-bridge` | 状态文件与日志目录                            |
| `ABAQUS_HTTP_CONFIG`       | —                       | 指定 `bridge_config.json` 路径                |
| `ABAQUS_HTTP_BRIDGE_DIR`   | 自动探测                | 指定 `abaqus_http_bridge.py` 所在目录（引导失败时的救命开关） |
| `ABAQUS_HTTP_LOG_REQUESTS` | 取自 `log_requests`     | 覆盖请求日志开关                              |
| `ABAQUS_TEST_OUT`          | 见第 12 节              | `selftest_http.py` 自检报告的输出路径         |

先设再启动的例子：

```bat
set ABAQUS_HTTP_PORT=8791
set ABAQUS_HTTP_TOKEN=mysecret
abaqus cae script=%USERPROFILE%\abaqus_plugins\abaqus_http_bridge\bootstrap.py
```

> 路径就是第 3 节的安装位置。`script=` 是可见 GUI 会话，保持
> `ABAQUS_HTTP_BLOCKING` 的默认值 `0` 即可。

---

## 8. 为什么这样设计

**为什么内核里不能简单起一个后台 HTTP 线程？**

因为内核空闲时拿不到 CPU，所以必须由 GUI **周期性地把它请回来**。FOX 定时器是唯一的途径。

本插件的服务模型是：

```
GUI 定时器（桥在服务时每 100ms，未服务时每 500ms，出错退避时每 2000ms）
  └─ sendCommand("__main__.mcp_serve_slice(1)")
        └─ 内核：select(监听套接字, timeout=0)     ← 关键
             有请求 → 处理 → 返回
             无请求 → 微秒级返回
```

**零超时的 `select` 是关键**：没有待处理连接时立即返回，所以空闲 tick 对 GUI 几乎零成本。只有真正收到请求时才占用，且占用时长 = 该命令的执行时长。

桥**没有**开启时，tick 连内核都不碰——只读一次状态文件。

定时器参数（都定义在 `abaqus_http_bridge_plugin.py` 的 `BridgePump` 里，可按需调整）：

| 参数                    | 值  | 含义                                                             |
| ----------------------- | --- | ---------------------------------------------------------------- |
| `WARMUP_TICKS`          | 12  | 插件加载后先空转 ~6s（12 × 500ms），等 CAE 起来再碰内核          |
| `MAX_REQUESTS_PER_TICK` | 1   | 每个 tick 最多处理 1 个请求，即约 10 req/s 的上限                |
| `COOLDOWN_S`            | 5   | 一次内核调用失败后暂停服务 5s，然后**重试**——退避而不是永久停用  |
| `OK_STREAK_TO_CLEAR`    | 3   | 连续 3 次正常后清空错误状态，回到 100ms 的正常节奏               |

> 最后两行是 1.3.5 的修复重点：早先 `errors` 是个只增不减的计数器，一次瞬时失败
> 就会让桥在整个 CAE 会话里彻底不再服务，连菜单里的 `Restart` 也救不回来。

### 无头模式

如果你不需要看界面（脚本化、批量、CI、AI agent 自动跑），用无头模式最省事：

```bat
set ABAQUS_HTTP_AUTOSTART=1
set ABAQUS_HTTP_BLOCKING=1
abaqus cae noGUI=%USERPROFILE%\abaqus_plugins\abaqus_http_bridge\bootstrap.py
```

`ABAQUS_HTTP_BLOCKING=1` 在这里**不是可选项**：无头会话没有 GUI 定时器去泵监听套接字，
默认的 `0` 只会把端口打开却不应答。忘了设的话 bootstrap 会打印警告提醒你。

没有 GUI 进程，就没有窗口会被占用。`mdb` / `session` / 作业 / ODB 全部可用，**唯一失去的是视口截图**（无头会话没有可渲染的视口）。

---

## 9. 安全

`POST /execute` 等价于**在 Abaqus 内核里任意执行代码**。

- 只绑定 `127.0.0.1`。**非常谨慎**改成 `0.0.0.0`（改成对外监听等于把机器的控制权交出去）
- 插件本身**没有**参数白名单、没有沙箱。任何能访问该端口的人都能执行任意代码
- 多用户机器上建议设 `ABAQUS_HTTP_TOKEN`，客户端请求带 `X-Bridge-Token` 头
- 端口不要通过防火墙或端口转发暴露出去
- **`/health` 和 `/ready` 不校验 token**（健康检查不该要凭证）。它们只回报版本、端口、
  pid，不碰任何 Abaqus 对象、不泄露模型内容——但确实是无需凭证即可访问的
- **`bridge.json` 里以明文保存 token**。这是有意的：附带客户端据此自动取到凭证，调用方
  不必被口头告知密钥。代价是该文件等同于一份凭证，请按凭证对待
  （不要把它拷进共享目录、日志收集器或备份快照）

---

## 10. 故障排查

| 现象                                                                      | 原因 / 处理                                                                                                                                    |
| ------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| 客户端报连接被拒                                                          | 端口默认关闭。点菜单 `Start Bridge`，或设 `ABAQUS_HTTP_AUTOSTART=1`                                                                            |
| `WinError 10013` 绑不上端口                                               | 落在 Windows 保留端口段。插件会自动换端口，实际端口看 `bridge.json`                                                                            |
| 菜单里看不到 `Abaqus HTTP Bridge`                                         | `%USERPROFILE%\abaqus_plugins\abaqus_http_bridge\` 下必须有 `__init__.py` 和 `abaqus_http_bridge_plugin.py`。改完要**重启 CAE**                |
| 点菜单报 `AttributeError: module '__main__' has no attribute 'mcp_start'` | 内核引导没成功。看 `gui_plugin.json` 里的 `kernel_bootstrap_sent` 与 `error` 字段                                                              |
| CAE 启动即崩溃（`SEGMENTATION FAULT` / `ipc_TOO_LITTLE_SENT`）            | 通常是残留状态文件让定时器在启动阶段就驱动内核。**删掉 `%USERPROFILE%\.abaqus-http-bridge\bridge.json` 再启动**；1.3.5 已按 mtime 拒绝陈旧状态 |
| 端口一直处于可连状态，但你没开 Abaqus                                     | 有残留进程。任务管理器结束 `ABQcaeK.exe` / `ABQcaeG.exe` / `SMAPython.exe`                                                                     |
| 端口可连，但所有请求都超时                                                | 无头会话没设 `ABAQUS_HTTP_BLOCKING=1`，没有定时器在泵。启动时会有对应警告                                                                      |
| 界面在跑长命令时无反应                                                    | 改用无头模式                                                                                                                                   |
| `import abaqusGui` 报 `can only be used in Abaqus/CAE GUI`                | 内核侧代码不得导入 `abaqusGui`。只有 GUI 插件文件可以                                                                                          |

**日志与状态文件**（默认在 `%USERPROFILE%\.abaqus-http-bridge\`）：

| 文件              | 内容                                                          |
| ----------------- | ------------------------------------------------------------- |
| `bridge.json`     | 实际端口、pid、模式、运行状态、token（明文，见第 9 节）        |
| `gui_plugin.json` | GUI 插件是否加载、内核引导是否成功、定时器间隔                 |
| `bridge.log`      | 所有请求日志（含来源 IP 与状态码）。超过 5 MB 自动轮转为 `bridge.log.1`（只保留一份） |
| `selftest_out.json` | 只在跑过第 12 节的自检后出现                                  |

---

## 11. 卸载

1. 停止端口（菜单 `Stop Bridge`）
2. 删除 `%USERPROFILE%\abaqus_plugins\abaqus_http_bridge\`
3. 删除 `%USERPROFILE%\.abaqus-http-bridge\`
4. 重启 Abaqus/CAE

---

## 12. 目录结构

压缩包：

```
abaqus-http-bridge-1.3.5/
├── README.md                          本文档
├── LICENSE                            MIT
├── MANIFEST.txt                       内容清单
├── install.py                         安装器（跨平台，自动识别 WSL）
├── install.bat                        Windows 便捷入口
├── plugin/                            → 整个目录装成 ~/abaqus_plugins/abaqus_http_bridge/
│   ├── __init__.py
│   ├── abaqus_http_bridge_plugin.py       GUI 菜单 + 定时器泵
│   ├── abaqus_http_bridge.py              内核侧 HTTP 服务（内核安全）
│   ├── bootstrap.py                       启动脚本（abaqus cae script=<此文件>）
│   ├── selftest_http.py                   无头自检
│   └── bridge_config.json                 配置（键与 DEFAULT_CONFIG 一一对应）
└── client/
    ├── bridge_client.py               独立 Python 客户端（库 + 命令行）
    └── example.py                     使用示例
```

自检脚本会把结果写到 `<状态目录>/selftest_out.json`（可用 `ABAQUS_TEST_OUT` 改路径），
并在进入泵循环**之前**先落盘，因为 `noGUI=` 下 Abaqus 会吞掉 stdout：

```bat
abaqus cae noGUI=%USERPROFILE%\abaqus_plugins\abaqus_http_bridge\selftest_http.py
```

运行中的目录结构：

```
%USERPROFILE%\abaqus_plugins\abaqus_http_bridge\   ← 代码和配置
%USERPROFILE%\.abaqus-http-bridge\                 ← bridge.json / bridge.log / gui_plugin.json
```

## 13. 已知限制

1. **长命令会占用 CAE 界面**——固有，与被占用的执行时长等值
2. **不支持 Abaqus < 2024**（要求Python 3）
3. **无头模式下没有视口截图**
4. **同一时刻只有一个进程能持有端口**；第二个实例会"收养"已存在的桥，因此不能同时驱动两个会话
5. **`timeout` 参数不会中断已开始的执行**——想停只能去 Abaqus 里 Ctrl+C
6. **没有参数校验或沙箱**，`/execute` 就是任意代码执行

---

_Abaqus HTTP Bridge 1.3.5 — MIT License_

_© 2026 Thompson Labs. 完整条款见 [LICENSE](LICENSE)。_

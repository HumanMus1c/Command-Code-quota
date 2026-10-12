# 服务侧转 MCP 容器 —— 改造方案

状态：**待评审，未实施**。评审通过前先做 §5 的 M-1..M-4 实测，实测不过就放弃本方案。

## 0. 一句话

把"插件自身服务"从**自研守护**（HKCU `Run` 键 → `entry.py --watch` 跟随循环）改成**宿主托管的
MCP（Model Context Protocol，模型上下文协议）stdio sidecar**；界面注入与"带端口启动"那一步**不变**。

## 1. 目标与非目标

- **G-1 生命周期外包**：服务的启停由宿主负责，删掉登录自启动键与跟随循环。
- **G-2 单实例语义不变**：`inject.lock` 仍然要——迁移期两条入口会并存。
- **G-3 卸载干净**：只摘我们自己那一条 `mcpServers` 条目，用户其它条目一字不动。
- **NG-1 不解决"双击启动就自带看板"**：注入要求宿主带环回调试端口，端口只能由启动参数给（§1 否证：
  宿主没有"用户级默认命令行"文件，`insertCSS`/`preload` 一类渲染层钩子 0 命中）。
- **NG-2 不改宿主安装目录**（N-2）。NG-3 不做面板对齐。

## 2. 实测依据（详见 `DESIGN.md` §1 的"官方扩展面 / 否证的扩展点"两行）

`~/.commandcode/mcp.json` → `mcpServers.{名字}`，条目字段 `{name, transport, command, args, url, env, enabled}`；
`transport:"stdio"` 必须有 `command`，否则走 `url`；`enabled !== false` 即默认启用；
宿主以 `{...process.env, ...entry.env}` 起子进程；读取顺序 `mcp.json` → `.mcp.json` → project 作用域。

## 3. 现状 → 目标 对照

| 环节 | 现在 | MCP 后 |
| --- | --- | --- |
| 谁启动服务 | 登录时 `HKCU\...\Run` → `pythonw entry.py --watch` → `follow()` 循环 | 宿主按 `mcp.json` spawn |
| 谁停止服务 | `POST /api/quit`、`uninstall.ps1`；宿主退出后循环空转等端口 | 宿主退出时回收（**待实测 M-3**） |
| 谁触发注入 | `follow()` 每 2.5s `attach()` | 服务进程内现有注入线程（`run_foreground(inject=True)` 同一套） |
| 单实例 | `watchdog.lock` + `inject.lock` | 只剩 `inject.lock`（HTTP 端口绑不上即退） |
| 崩溃自愈 | 无（`Run` 键只在登录时生效） | 取决于宿主是否重启子进程（**待实测 M-2**） |
| 日志 | 无（`pythonw` 无控制台，这是上一轮 `--watch` 静默死亡没被发现的原因） | stderr → 宿主日志目录，另存 `runtime/gauge.log` |

## 4. 设计要点

- **4.1 新增 `app/mcp_server.py`**：最小 JSON-RPC 2.0 over stdio（`initialize` / `tools/list` /
  `tools/call` / `ping` / `shutdown`）。硬纪律：**stdout 只允许协议字节**，任何日志走 stderr 或文件，
  否则宿主侧解析会被我们的输出打断（M-6）。握手要快（`initialize` 立即返回，服务线程后台起）。
- **4.2 `install.ps1` 写 `mcp.json`**：读-改-写，保留其它条目；写入复用"临时文件 + `os.replace`"的原子写规范，
  但目标在用户配置目录而非 runtime dir ⇒ `write_json_atomic` 需要显式接受一个允许路径，不能开后门式绕过 §5。
  条目形如：`"goat-gauge": {"transport":"stdio","command":"<repo>\\.venv\\Scripts\\python.exe",
  "args":["<repo>\\entry.py","--mcp"],"env":{"GOATGAUGE_DATA":"..."},"enabled":true}`。
  键名 `transport` vs `type` 由 M-1 定，**写错等于静默不加载**。
- **4.3 `uninstall.ps1`**：摘除该条目（含"文件里只有我们一条时删文件"的分支），数据目录仍默认保留。
- **4.4 过渡开关**：`install.ps1 -NoMcp` 保留旧 `Run` 键路线。两条同时开时由 `inject.lock` 决胜负（已实现），
  但必须真机验一次（M-5）。
- **4.5 `entry.py --mcp`**：新标志必须被真正的解析方接受——上一轮 `--watch`/`--follow` 不匹配导致登录自启动
  从来没生效过（`d515db5`），补 `EntryCLITests` 同款跨文件契约测试。
- **4.6 可选加值**：暴露一个 `goat_gauge_status` 工具（只回白名单数值/布尔，PII 不出），让 agent 也能查额度。
  这是 MCP 路线独有的能力，与注入无关，可单独砍掉。

## 5. 开工前必须实测的前置问题

| 编号 | 问题 | 判据 | 不过怎么办 |
| --- | --- | --- | --- |
| M-1 | 条目键名与必填字段 | 写一条最小 echo server，看宿主 `mcp-list` 里是否 `connected` | 按实测改 schema 常量 |
| M-2 | 子进程崩溃后宿主是否重启 | 杀 sidecar，等 60s 看是否复生 | 保留 `watchdog` 的自愈职责，收益减半 |
| M-3 | 宿主退出后是否回收子进程 | 宿主全退后 `tasklist` 里 sidecar PID 是否还在（Windows 上 node 系常留孤儿） | sidecar 自己监视父进程退出 |
| M-4 | **启动时机**：应用启动即 spawn，还是打开会话/首次用到工具才 spawn | 冷启动到 sidecar 首次心跳的墙钟差 | **若是按需启动，本方案不划算**（看板比现在更晚出现），直接放弃 |
| M-5 | 双入口并存时谁拿 `inject.lock`、页面显示谁 | 同时开 `Run` 键与 MCP，读 `--status` 与指纹 `src=` | 迁移期强制二选一 |
| M-6 | stdio 与 HTTP 18927 共存是否稳定 | 挂 30 分钟，看宿主侧连接是否断开 | 拆成两个进程（收益进一步下降） |

## 6. 收益与代价

**收益**：删掉登录自启动键与整个跟随循环（约 200 行 + 一整块已被真机证明脆弱的复杂度）；服务随宿主自然
启停；宿主自己的 MCP 面板成为可见的诊断入口。
**代价**：多出一组必须成立的宿主行为假设（M-1..M-4）；stdio 输出纪律；宿主更新频繁（0.1.51→0.1.52 只用了
不到一天），schema 一变我们就静默失效 ⇒ 必须有 §7。

## 7. 回退与降级

- sidecar 没起来（键名变更/宿主取消支持）⇒ `--status` 明确报 `sidecar=absent` 并提示 `-NoMcp` 路线；
  **不做** `SUPPORTED_VERSIONS` 式硬版本锁（N-2 的理由），改为"识别不了就不接管生命周期"。
- `Run` 键路线保留一个大版本周期，两条路线共存时后到者按现有锁语义退避。

## 8. 拆分与验收（评审通过后）

1. `[diag]` M-1..M-4 实测脚本 + 结果记录（**不动产品代码**）；
2. `[feat]` `app/mcp_server.py` + `entry.py --mcp` + 跨文件契约测试；
3. `[feat]` `install.ps1`/`uninstall.ps1` 读写 `mcp.json`（含"保留用户其它条目"的回归用例）；
4. `[fix]` `watchdog` 退场开关 `-NoMcp` 与 `--status` 的 `sidecar` 字段；
5. `[docs]` 条款定稿（L- 生命周期一节按新形态重写）。

**验收判据**：宿主启动后 `--status` 里 sidecar 的 PID 是 Command Code 的子进程（PID 谱系校验）且 18927 由它
持有；宿主全退后无残留进程；`mcp.json` 中用户其它条目逐字节不变；看板注入行为与今天完全一致（`boardLog`
无新增高度跳变、空闲窗口 `count=0` 仍成立）。

## 9. 建议

先只做第 1 步（实测脚本，不动产品），拿 M-1..M-4 的答案回来再决定是否继续。**尤其 M-4 是决定性的一条**：
如果宿主是"按需 spawn MCP 服务器"，那 sidecar 会让看板比现在更晚出现，换来的只是删掉一个已经跑通的守护——
不值得。

# GOAT Gauge × Command Code 宿主适配设计规范

本规范只覆盖**注入到 Command Code 桌面应用的读数条**这一种形态。独立 Web 面板（`app/web/index.html`、
`app.js`、`manifest.webmanifest`、`sw.js`、`app/chrome_mode.py`）已按 2026-10-11 的决定废弃，
不得在新代码里重新引入对它们的依赖。

格式约定：每条结论后标注 **\[实测\]**（本机命令/文件读取直接得出）、**\[静态\]**（读宿主构建产物推断）、
**\[未验证\]**（尚无真机证据，禁止据此报告"跑通"）。

---

## 1. 宿主事实基线（改锚点前先回看这张表）

| 项 | 结论 | 依据 |
| --- | --- | --- |
| 包名 / 版本 | `@commandcode/desktop` 0.1.52，Electron 43.3.0 / Chrome 150 | `resources/app/package.json` \[实测 2026-10-12\] |
| 自动更新 | `package.json` 带 `updateFeed`；`resources/app/**` 全部文件时间戳 = **2026-10-12 03:35**（本次排查当天），宿主号从 0.1.51 变 0.1.52 | \[实测\] ⇒ 任何写进安装目录的补丁**活不过一次更新**，除非按上游那样硬锁版本号 |
| 安装位置 | `D:\Program Files\Command Code\`，`resources/app` 为**未打包目录** | \[实测\] |
| 目录权限 | `out/renderer/index.html` 的 ACL 为 `BUILTIN\Users:(I)(RX)`；当前会话不在管理员组 | `icacls` \[实测\] |
| 单实例 | `app.on("second-instance", …)`（`out/main/index.js:32271`）⇒ 旧实例不退，带参数再启动只会聚焦旧窗口 | \[实测\] |
| 调试端口 | 主进程无 `app.commandLine.appendSwitch`（全文零命中）⇒ 宿主既不自开也不禁用 CDP | \[实测\] |
| 页面 CSP | `default-src 'self'; script-src 'self' 'wasm-unsafe-eval' 'sha256-zrvkeKAHvjk8g+o0GV59i098Rz4hc1p5vZJqJBHNJBU='; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-src 'none'; worker-src 'none'; object-src 'none'; base-uri 'none'` | `out/renderer/index.html` \[实测\] |
| Trusted Types | CSP **不含** `require-trusted-types-for` ⇒ 宿主未强制 Trusted Types | \[实测\] |
| 官方扩展面 | **有，但都不是渲染层容器**：① `~/.commandcode/mcp.json` 与 `~/.commandcode/.mcp.json`（另有 project 作用域），键形如 `mcpServers.{名字}:{name, transport, command, args, url, env, enabled}`，`transport:"stdio"` 必须有 `command`、否则用 `url`，`enabled !== false` 即默认启用，宿主用 `{...process.env, ...entry.env}` 起子进程；② `~/.commandcode/skills`（`initialSkills`/`skillsCatalogCache`，提示词层）；③ `settings.json` / `settings.local.json` / `AGENTS.md`；④ CLI `config.json` 实测顶层仅 `featureModels/reasoningEffort/starredModels` | `out/main/index.js` 的 `isEnabledEntry`/`inferredTransport`/`spawn` 调用点 \[实测静态\] |
| **否证的扩展点** | `contributes`=0、`extensionsDir`=0、`loadExtension`=0 ⇒ 无 VS Code 式扩展宿主；`statusLine` 的 5 处命中是宿主解析 `status:` 文本的**局部变量**（`/^status:\s*(\S+)[ \t]*$/m`），不是配置驱动的状态栏；`hooks` 命中是 husky 与 React hooks；`insertCSS`/`customCss`/`userCss`/`executeJavaScript` 全 0 ⇒ **宿主没有"用用户文件注入界面样式或脚本"的官方通道** | \[实测静态\] |
| 数据桥 | `window.electronAPI`：`accountOverview(options)` / `sessionStats(sessionId)` / `sessionContextBreakdown(sessionId)` / `getAppState()`，通道名 `account-overview`/`session-stats`/`session-context-breakdown` | `out/preload/index.js`、`out/main/index.js` \[静态\]，方法均可调用 \[实测\] |
| **接口信封** | 每次调用返回的是信封而不是裸对象：`accountOverview → {success, overview}`、`getAppState → {success, state}`、`sessionStats → {success, stats}`、失败为 `{success:false, error:string}`。**不剥壳就恒得 undefined** | `--probe` 真机回读 \[实测\] |
| `overview` 字段 | `user{id,name,email,userName,org?}` · `identity{kind,name,login,image}` · `credits{monthlyCredits,purchasedCredits,freeCredits,belowThreshold,creditThreshold,availableCredits?}` · `subscription{planId,status,currentPeriodStart,currentPeriodEnd}` · `usage{totalCount,completedCount,failedCount,totalTokens,totalTokensIn,totalTokensOut,totalCredits,totalCost,averageCost,successRate,models[]}` · `windowLimits{limited,exceeded,fiveHour{used,cap,exceeded,resetAt:number(ms)},weekly{同}}` | \[实测\] |
| `stats` 字段 | `sessionStats(threadId) → {branches:number, messages:number, contextUsage{current,limit}, tokens{input,output,cacheRead,cacheWrite}}` ⇒ **没有任何时长或时间戳字段**，命中率可由 `cacheRead/(input+cacheRead)` 算，tok/s 与 cr/h **不能由单次宿主调用得出** | \[实测\] |
| `sessionContextBreakdown` | 对当前活动线程返回 `{success:false, error:string}` ⇒ 不可依赖 | \[实测，仅此一线程\] |
| CDP 握手 | Chromium 150 拒绝**带 Origin** 的 WebSocket 升级（403，并提示加 `--remote-allow-origins`）。正确解法是**不发 Origin**（`websocket-client` 的 `suppress_origin=True`），加那个开关等于把端口让给本机任意网页进程 | \[实测\] |
| 界面锚点 | **真机普查（只读求值，未注入）**：`.cc-composer`=1、`.cc-composer-surface`=1、`.cc-composer-toolbar`=1、`textarea[rows="1"]`=1、`[contenteditable="true"]`=0 ⇒ 锚点存在且唯一。优先级仍按 `.cc-composer-surface` > `.cc-composer` > `.cc-composer-toolbar` > `textarea`；路径上无 `data-testid` | 真机 \[实测\] |
| 宿主容器几何 | `.cc-feed-body`=0，且 `.cc-composer` 的上溯四层是 `block/row`：`cc-composer w-full` → `mx-auto w-full px-` → `shrink-0 pb-2 pt-2` → `relative z-10 shrink-0 px-[v`。⇒ **"挂在同一 flex 列的兄弟位置"这一前提不成立**，真实容器是块流，宽度由容器决定而非 flex 分配。插入方式与 P-2 的测量假设都要按块流重做 | 真机 \[实测\] |
| 明暗与语言 | `data-theme="dark"` + `classList.contains('dark')=true`、`<html lang="en">`、文档协议 `file:` | 真机 \[实测\] |
| 语言 | i18next 单命名空间 `desktop`，`supportedLngs:["en","zh"]`，`fallbackLng:"en"`；切语言改写 `<html lang>`（zh → `zh-Hans`） | \[静态\] |
| 主题 | `<html data-theme>`（`light\|dark\|purple\|purple-dark`）+ `classList.toggle('dark', theme!=='light')` | \[静态\] |
| 真实上限量级 | 5 小时 3、本周 6 credits（**不是** 14/35/70） | \[实测\] |
| PII | `%APPDATA%\Command Code\app-state.json` 的 accountCache 含账号名、邮箱、头像 base64 | \[实测\] |

**宿主构建产物里混有旧版本残留**：`out/renderer/assets/` 同一组件存在多份不同 hash 文件（如 `BrowserPanel-*` 三份，
日期跨 2026-08-22 至 08-29），只有 `index.html` 钉住的 `index-DsjDLIUh.js` / `index-Wa25GrCf.css` 是活的 \[实测\]。
⇒ 任何"从产物里读到的锚点/字段"结论都必须回到 `index.html` 的引用链确认，不能只看文件存在。

---

## 2. 注入边界

- **N-1 唯一通道是 CDP（Chrome DevTools Protocol，浏览器调试协议）运行时注入**：`Page.addScriptToEvaluateOnNewDocument`
  + `Runtime.evaluate`。页面 CSP 的 `script-src 'self' + hash` 会拒绝注入的 `<script>` 元素，但 CDP 求值不受页面 CSP 约束。
- **N-2 永不修改宿主安装目录，也永不修改宿主 CSP。** 上游 Antigrative Dashboard 曾保留这条路
  （`compat/patch-loader.py` 写 `APP.write_bytes`），今天已降级为显式 `--legacy-loader`、硬锁单一宿主版本、
  且**默认安装会主动回滚该补丁**。这条路会把插件绑死在宿主的一个版本号上。本机还额外需要 UAC 提权（§1 ACL）。
- **N-3 挂载守卫按 protocol 而不是 origin 比较。** `file://` 文档的 `location.origin` 是 `"null"`，
  按 origin 比较会既不挂载又零报错（`app/inject.py:66-79` 的 `origin_of()` 已按此实现）。
- **N-4 全页只允许一份读数条**，且必须在 open Shadow DOM 内（`widget.js:851` `attachShadow({mode:"open"})`）。
  样式走 `style.textContent`（`style-src` 已允许 `'unsafe-inline'`），不注入宿主 `<head>`。
  **"一份"的判据是闭包所有权，不是节点存在性**：每个闭包只允许绘制自己 `build()` 出来的那个节点
  （`state.node`），`render()`/`refit()`/`tick()` 遇到"id 应答的节点不是我的"就一律不改写；
  接管时必须先调用上一任的 `__goatDispose()`（同版本也一样），否则宿主 React 摘掉节点的瞬间，
  旧闭包的心跳与轮询会**领养新节点**并按自己内存里的 layout 复画——真机实测后果是同一颗指标画两次、
  邻居被挤掉、状态点被指标覆盖，且 `__goatProbe()`（属于最后一任闭包）看着完全正常。
  `dispose` 必须撤销它挂上的**每一种**监听：`resize`、`MutationObserver`、`ResizeObserver`、定时器，
  以及 `visibilitychange`（漏掉这一条时，"已卸载"的闭包会在下一次切回前台重新开机）。
  回归用例：`check_widget.py` 的 `painter: the row stays exactly the layout across every poll`。
- **N-5 几何优先于覆盖。** 窄到放不下一个胶囊、或宿主可点击控件被真实遮挡时，整体隐藏（`blocked`），
  **绝不覆盖宿主控件、绝不改宿主任何分支的 flex/min-width**。dispose 前后宿主内联样式必须逐字节相同。
  两条**当前代码尚未满足**：
  - 遮挡判定要用**实际命中测试**而不是纯矩形相交：先过滤不相交矩形，再在交集中心与四角做
    `document.elementFromPoint`，只有打到宿主控件本身才算遮挡。纯几何会双向误判——按钮矩形与条相交但
    命中测试落到输入框（该显示却隐藏），以及浮动且实际绘制在交集处的按钮（该隐藏却没隐藏）。
    上游同类复核结论见 `docs/ROBUSTNESS.md:97-99`。
  - `blocked` 必须同时置 `inert` 与 `aria-hidden="true"`。现在只有 `widget.js:646` 的
    `visibility:hidden;pointer-events:none`，读屏仍会念出一条看不见的条、Tab 仍可能落进隐藏胶囊。
    **README 声称的"并置 `inert`"与代码不符**（`widget.js` 全文 `inert` 零命中），以本条为准：要么补齐代码，
    要么修正 README，二者不得继续分叉。
- **N-6 调试端口最小暴露**：宿主侧只用 `--remote-debugging-port=0 --remote-debugging-address=127.0.0.1`
  （随机端口 + 显式环回）。实际端口**只能**从 `%APPDATA%\Command Code\DevToolsActivePort` 首行读取
  （`inject.py:87-97`），禁止在脚本里写死端口号。发现与请求必须绕过系统代理，且拒绝任何非环回的
  `webSocketDebuggerUrl`（`inject.py:120`）。
- **N-7 卸载即无痕**：`__goatDispose()` 移除节点、观察器与定时器；`Page.removeScriptToEvaluateOnNewDocument`
  注销预加载。注册归属发起它的 socket，故守护进程退出后不会在后续导航中复活。
- **N-8 DevTools 握手必须不带 Origin**：Chromium 150 对携带 Origin 的 WebSocket 升级回 403。
  解法是 `websocket.create_connection(..., suppress_origin=True)`（`inject.py:148`），
  **禁止**改用 `--remote-allow-origins=*` 或按端口白名单放行——那等于把宿主窗口交给本机任意网页进程脚本化。

## 3. 锚点与歧义

- **A-1 锚点只用稳定语义类名**，按 §1 优先级取第一个可用；**禁止**用 `aria-label`、占位文案、按钮文字定位
  ——宿主带 en/zh 两套语言包，文案随语言变。
- **A-2 歧义即拒绝挂载**（当前代码**尚未**满足：`widget.js:632/733/866` 用 `querySelector` 取首个匹配）。
  候选多于 1 个时必须当作"无法确定归属"，本轮不挂载，等下一次心跳；不允许猜第一个。
- **A-3 可见性判定是挂载前置**：`isConnected` + 不在 `[hidden]/[inert]` 内 + `getClientRects().length > 0`
  + `visibility !== 'hidden'`（上游四条件见 `ide/surface.cjs:5`）。当前代码**尚未**满足：
  `widget.js:632/733/866` 直接 `querySelector` 取锚点，没有对候选做可见性过滤——不可见的节点会被当成有效锚点，
  挂上去之后几何校验只能事后 `blocked` 兜底，白占一次布局。
- **A-4 重挂载靠 MutationObserver + 1 秒心跳双保险**（`widget.js:940-942`），启动顺序上脚本可能早于
  `.cc-composer` 出现——预览页 `?late=1` 就是专门复现该顺序的夹具，改动挂载逻辑必须跑它。
- **A-5 异步结果必须带请求代次**（当前代码**尚未**满足：读数条改走 D2 后 `electronAPI.*` 全是异步 Promise，
  而宿主可在等待期间换 thread）。规则：
  - 每次**导航 / 切换 thread / 重新挂载**递增一个代次，并同时重置该会话的刷新预算，立即调度首次拉取；
  - 回调只有「代次仍是当前值 **且** 返回数据所属会话标识与请求时一致」才允许写状态；
  - 晚到的响应、异常分支、`finally` 一律不得改动新会话的状态；卸载与 `__goatDispose()` 必须先失效化所有
    in-flight 请求再拆节点。
  `sessionStats(sessionId)` / `sessionContextBreakdown(sessionId)` 必须回读比对请求时用的那个 `sessionId`，
  **不接受"最近一个会话"**；`sessionId` 取自 `getAppState()` 的 `appState.activeThreadId`，且只认当前输入框
  所属区域的会话（上游同类边界见 `ide/surface.cjs:57-90` 的 `active / new / unavailable` 三态，
  同类缺陷成因见 `docs/ROBUSTNESS.md:95`——跨路由全局 pending 让新会话在等旧请求）。
  新会话没有任何样本时显示"不可用/横线"，**不得**沿用上一个会话的数字。

## 4. 数据边界

- **D-1 读数条的数据只来自宿主**：`accountOverview()` 供额度/账期/请求数/TOKEN/费用/均价；会话级派生量待
  `sessionStats()` / `sessionContextBreakdown()` 结构经真机确认后使用。插件自身服务不得成为读数条的取数依赖。
- **D-2 无假数据**：字段缺失、为 `null`、非有限数 ⇒ **不渲染该胶囊**。禁止把缺失折算成 0
  （**现存缺陷**：`server.py:783` 无条件 `rates["hit_rate_pct"] = round(store.cache_stats("today").get("hit_rate", 0.0), 2)`
  ——桶表为空时取默认 0.0，会把"没有数据"显示成"命中 0.0%"）。
  "真 0"与"缺失"必须是两条不同代码路径。
  - **补充（2026-10-12，用户两次拍板后的最终口径）**：本条只约束**值**，不约束**槽位**。
    **初始化完成后，所有未被隐藏的指标一律常驻板面**，没有读数的那颗显示 `—`
    （em dash，语言中立，**不是 0**；类名 `idle` 而非 `muted`，以保留编辑器里的"隐藏"按钮）。
    首帧在"既无宿主也无推送"时只出 `waiting` 一颗，不铺一屏破折号。
    **Why**：整颗胶囊进出等于 60–90px 重排，跨过换行点就是 28↔49px 的整块高度跳动，
    而它恰好发生在用户打字时（采样窗口滑过阈值）；编辑态下同一次重排会把 取消/保存/添加 挤出可见区。
    中间形态（"会话内只增不减"）已被这条取代——**成员恒定**比"只增不减"更强，也顺带让 P-6 的
    槽位提交只剩"用户隐藏"一种触发路径。
- **D-3 进入 DOM 的只有两类值**：① 经 `isFinite` 守卫的数值（统一走 `trim/compact/money/toFixed`）；
  ② 本模块 i18n 词表常量。**任何宿主或上游字符串（模型名、`planId`、账号名、邮箱、错误文本）不得进入
  `innerHTML`**（`widget.js:523` 用字符串拼 markup，目前靠约定而非机制保证安全）。新增字段必须先加白名单再加渲染。
- **D-4 零网络**：读数条不发 `fetch`、不建 WebSocket、不引外部字体/图标（图标只用 `⚙ × ▾` 字符）。
  宿主 CSP 的 `connect-src 'self'` 也决定了这条路不该走。
- **D-5 PII 不出门**：宿主数据只在内存里渲染，不落盘、不进日志、不进 Git。诊断自述（探针）**只允许记录键名与类型**，
  且写入用户数据目录 `%LOCALAPPDATA%\GOATGauge\`，永不写进仓库。
- **D-6 单位符号不进语言表**（`cr/h`、`tok/s`、`h`/`m`/`d`），两种语言下一致；标签不得与宿主已有标签撞名。
- **D-7 吞吐归一口径**：生成速度按请求实际流式时长（`Σ duration_ms`）归一，**不是**首末记录墙钟跨度
  ——后者把空闲间隙算进分母，会低估一个量级。
- **D-9 月份进度条的分母只能估、不能编**：宿主侧没有任何月度上限字段——`subscription` 只有
  `planId/status/currentPeriodStart/currentPeriodEnd`，`credits` 只有余量与阈值，而
  `listPlans/getLatestPlan/readPlan` 是 **agent 计划文件**（`{path,title,content,mtime}`），不是套餐。
  取序为：① 本地服务从计费接口拿到的套餐上限；② 本计费周期内实测到的最大余量（以
  `currentPeriodStart` 为界存 `localStorage` 的 `goat-gauge-period-v1`，跨期即重置）；③ 两者都没有时
  退回"只显示余量、不画条"。月份胶囊的文本形态必须与 5h/周一致（`已用/上限` + 进度条 + 倒计时），
  不得再出现"7.16 cr 剩余"这种另一种口径。
- **D-11 演示与真实必须可机读区分**：`--demo` 的合成池与真实计费池曾在同一个 18927 端口上
  无法分辨，直接导致把合成的 `cap:70` 当成账户真实上限、把三轮诊断绕完才回头。现在：
  `live()` 输出 `mode: "live"|"demo"`，注入 payload 透传同一个字段，读数条把它写进指纹
  （`src=push:demo` 与 `src=push:live` 可分辨），`entry.py --status` 直接回读端口持有者的
  `gauge.mode` 与 `inject_mode`，不必再靠时间戳猜。注入锁记录持有者的模式，
  **演示实例不得取代真实实例的锁**（真实实例死后也一样）。未标 `mode` 的 payload
  在指纹里记成 `push:unknown`，不得默认当作真实数据。
- **D-12 速度类指标只允许来自"会动的计数器"**：`gen`（tok/s）已于 2026-10-12 从板面**删除**，改由
  `ctx`（上下文占用 `sessionStats.contextUsage{current,limit}`）顶替。依据是真机两张连续截图：
  用户新开会话产生了真实消耗（`5h 0→0.25`、`Week 2.84→3.09`、`Burn 0.00→0.79 cr/h`），
  但 `Reqs 857` 与 `Tok 367.57M` **一动不动** ⇒ `usage.totalCount` / `usage.totalTokensOut` 是
  带缓存的账期聚合值，对它们做差分永远得不到速率（`derivedRates()` 逻辑正确，原料是死的）。
  同一条判据适用于服务侧：`/api/live` 的 `rates.basis="none"`（窗口内 0 条 usage_records）时
  `requests_per_min`/`tokens_per_sec` 必为 `null` ⇒ 这类胶囊按 D-2 缺席，不得显示 0。
  `ctx` 与其余计量胶囊同形态（数值 + `role=progressbar`），数值走 `compact()`，
  且**不带倒计时装饰**（上下文窗口没有重置时刻）。
- **D-8 诊断自述按账号隔离且不猜测**：`probe.json` 一类自述写入
  `%LOCALAPPDATA%\GOATGauge\<accountHash>\`，哈希 = `sha256(userId)` 前 12 位。哈希隔离
  **不等于匿名化**（同哈希可跨文件关联，不许在 README 里写成隐私声明）；确认不了当前账号时**不猜、不写、
  不读别的账号目录**，离线启动也无法确认就整段跳过（上游同一立场：`docs/ROBUSTNESS.md:125,127`）。
  **键名本身可能就是标识符**——实测 `getAppState().state.deletedThreads` 是以线程 ID 为键的 map，
  逐键列出自述会把 13 个 ID 写上磁盘。所以自述必须做键归一：不符合 `^[A-Za-z][A-Za-z0-9]{0,24}$`
  的键、或键数超过 20 的对象，只输出 `{__map__: 数量, __value__: 值类型}`，不输出键名。

## 5. 生命周期（守护进程）

- **L-1 一个常驻守护，登录后由 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` 拉起**（`pythonw`，无窗口）。
  宿主没有 Antigravity 那样的官方插件容器（`sidecar` + `restart_policy`）可以外包生命周期，所以只能自持。
- **L-2 状态机跟随宿主**：`等待宿主` → `等待端口文件` → `已 attach` → `宿主退出/端口消失 → detach 回等待`。
  宿主缺席时禁止做任何 CDP 请求。无端口时**静默等待**，不重启宿主、不弹通知；带端口启动是一条显式的
  `gauge-launch.cmd`（照抄上游 `ide/manage.py:37-47`：detached spawn + `CREATE_NO_WINDOW`）。
- **L-3 单实例**：用户数据目录持 `watchdog.lock`（PID + 端口 + 启动时间）。抢不到锁的新实例**立刻退出**，
  绝不杀无关进程。两个守护并存会各自注册一次预加载脚本，表现为读数条重复或互相覆盖。
- **L-4 判活禁止依赖 HTTP 端点**：主判据是锁文件 + 端口归属 + 进程名。`/api/state` 与 `/api/quit`
  确实存在（`--status` 的 `gauge.reachable` 就是走它），但**只能证明 HTTP 线程活着**——
  2026-10-12 的实证：服务可达、`--status` 全绿，注入线程却早已因一次 `OSError` 死掉，用户重启宿主后看不到看板。
  ⇒ "注入是否活着"只能看 `inject.lock` 持有者的日志里有没有后续 `attached`，不得拿 `reachable` 顶替。
- **L-8 后台线程不得因单次传输异常终止**：`WidgetInjector.serve()` 的每一轮与 `attach()` 本身都要
  兜住任意异常（不只是 `InjectError`），转成 `last_error` + 一条日志后继续下一轮；
  注入线程的 `log` 必须与守护进程写同一个文件（`%LOCALAPPDATA%\GOATGauge\watchdog.log`），
  因为 `pythonw` 没有控制台——**无控制台 + 无落盘 = 失败不可见**。
  回归用例：`test_serve_keeps_looping_after_a_failing_cycle`、
  `test_attach_swallows_a_transport_error_instead_of_dying`（红/绿在 `git archive` 基线副本上做过）。
- **L-5 幂等**：同一份源码重复注入不得产生第二个节点；源码指纹变化时才清理并替换（`WIDGET_VERSION`
  `inject.py:24` 与 `widget.js` 内部版本必须同步，不一致的 payload 直接丢弃）。
  幂等**不等于"什么都不做"**：`window.__goatWidgetVersion` 与版本一致但节点已被宿主摘掉时，
  重注入必须走 N-4 的接管路径（先 dispose 再建），而不是"再挂一个闭包"或"领养别人的节点"。
- **L-6 写用户数据目录必须原子且不越界**（当前代码**尚未**满足：`watchdog.py` 与 `install.ps1` 尚未落地，
  现有 `credentials.py` 的 DPAPI 写入同样要按本条复核）：
  - 一律"**同目录 temp 文件写完 → `os.replace`**"，禁止就地截断重写（上游同一做法：`manage.py:36-40`）；
    同目录才保证同卷，rename 才是原子的。
  - 落盘路径必须先 `resolve()` 再校验仍在 `%LOCALAPPDATA%\GOATGauge\` 之内；**拒绝符号链接与越界目录**
    （上游：`manage.py:42-46,155-159`）。
  - 读侧对损坏 JSON / 半写文件按"忽略该文件"处理并继续运行，**实时显示不得因持久化失败而中断**，
    但要注明"未保存"（上游：`docs/ROBUSTNESS.md:29` 那一行故障矩阵）。
  - `watchdog.lock` 内容 = PID + 实际随机端口 + 启动时间；抢占失败的新实例立即退出（见 L-3），
    也绝不用写锁以外的方式判活（见 L-4）。
  - 任何被本插件写到宿主目录或用户目录的文件都必须出现在 `uninstall.ps1` 的清理清单里；
    宿主安装目录**不在其中**（见 N-2）。

## 6. 呈现

- **P-1 紧凑读数条，无图表**：数值 + 简单进度条，数字等宽对齐，不做多色调主题。
- **P-2 宽度阶梯只影响"怎么呈现"，永不影响"显示哪些指标"**：单行 → 换行（限高三行）→ 依次牺牲额度表内倒计时与
  进度条（只留数字）→ `tall` → `blocked`。测量前必须回到不换行状态再读 `scrollWidth`，否则已换行的行会被读成
  "不溢出"而错误退回单行。
- **P-3 配色一律解析宿主根元素 CSS 变量**（`--primary/--card/--background/--foreground/--ink-subtle/--border/
  --success/--warning/--destructive`），取不到才按当前明暗态回落内置默认；只有简洁浅色与简洁深色两套，
  默认跟随宿主。宿主切 `data-theme` 由心跳感知并就地重绘，不需要重新注入。
- **P-4 文案跟随宿主 `<html lang>`**：`zh-*` 中文，其余任意语言（含 `en`、`fr`）回落英文；语言切换心跳重绘。
- **P-5 状态只以颜色点表达**，数据年龄经 `title` 悬停呈现——文本增减会推动布局。
- **P-6 布局编辑**存 `localStorage` 的 `goat-gauge-layout-v2`；提交时只重写**当前真的在板上**的那些槽位，
  被用户**隐藏**的指标保持原位（成员恒定后，"暂时没数据"不再是离开板面的理由，见 D-2 补充），
  否则一次偶发空窗会永久打乱用户布局。所有事件在 Shadow DOM 内委托并
  `stopPropagation`，不漏给宿主输入框。
  **DOM 只有在"正好是这块板"时才算用户意图**：`commitDomOrder()` 先按当前 layout 的可见集合校验——
  出现重复 id、缺少某颗在板指标、或多出 layout 里没有的 id ⇒ 整笔提交作废。读 `localStorage` 时同样
  对 `order`/`hidden` 去重。理由：拖动结束是从 DOM 反读顺序的，若有别的写入者（见 N-4）把
  `week` 画了两遍并挤掉 `cache`，不去重的提交会把这份脏 DOM 固化成用户的布局，指标就**永久**少一个。
  回归用例：`storage: a duplicated order entry is read once`、
  `commit: a duplicated or missing metric in the row is never baked in`。
- **P-7 呈现状态必须可量化自证**（当前代码**尚未**满足：`__goatProbe()` 现只回读
  `mounted / version / host / hostError / pushed / layout / editing / metrics`，见 `widget.js:1004-1012`）。
  把它扩成一段**布局指纹**，至少含：`dataset.state`、`dataset.layout`、`dataset.edit`、当前语言标签、主题签名、
  在场指标的 `data-metric` 序列、`#row` 的 `scrollWidth` 与 `clientHeight`、与宿主可点击控件的相交数。
  上游同类做法是 `compat/inline-widget.cjs:198` 的 `probeKey()`——把样式文本、语言、会话、条内文本、
  状态点类名、各控件宽度拼成一个可比较的键。
  验收规则：**动效/布局类改动必须给出改动前后两份指纹的差异**；生命周期日志全绿而屏上零效果不算跑通
  （对应既有教训"begin/done 日志全绿也可能屏上零效果"）。
  指纹只允许含数值、布尔与自有类名——**不含账号数据、模型名、路径、宿主文本**。
- **P-8 无障碍语义必须显式声明**（当前代码只有一处 `widget.js:849` 的 `role="group"` 加若干 `aria-label`，
  进度条与遮挡态都缺语义）：
  - 计量条：`role="progressbar"` + `aria-valuemin="0"` + `aria-valuemax`=实际 cap + `aria-valuenow`=实际 used
    + `aria-label`=指标名；再加 `aria-valuetext` 整句（如"本周已用 4，共 6"），避免读屏把 `4/6` 念成"四比六"。
  - 等待宿主数据期间给交互按钮 `aria-busy="true"`；`aria-haspopup`/`aria-expanded` 已在
    （`widget.js:488`），下拉开合必须同步这两个值。
  - 齿轮、`×`、`添加` 保持原生 `<button type="button">`（已是）且各有中英文可访问名；
    `widget.js` 全文 `focus` 零命中 ⇒ 焦点可见性目前完全依赖浏览器默认样式，
    读数条在宿主深浅两套底色上都必须能看见当前焦点。
  - 语言切换时所有 `aria-label`/`aria-valuetext` 随词表一起重绘——整行重绘天然满足，写死成条款以防将来
    改成局部更新时漏掉。
  - `prefers-reduced-motion` 继续生效（`widget.js:705` 已用 `transition:none!important`），
    后续若加降级过渡动画，不得用 JS 动画绕过这条媒体查询。
- **P-9 降级是一个"层级"，只在层级真的改变时才写 class**：`-1` 单行、`0..N` 依次牺牲 N 个装饰、装饰牺牲完才放弃
    限高。数据轮询只做**读取式测量**并在证据不足时把层级往下走一步；往回升级需要"未降级时的宽度"，而这个宽度
    只能靠先取消降级来测——因此**唯一允许重判层级的触发条件是可用宽度真的变了**（`state.fitWidth` 对比）。
    整行重建**不**重判，只把已记录的层级重新贴到新 DOM 上；ResizeObserver 也**不**无条件重判，
    因为它同时因我们自己的高度变化而触发，"高度变→重判→换档→高度再变"就是自持振荡器。
    每次层级迁移由 `setLevel()` 计入 `levelChanges`（无界计数器）与 `shedChanges`（有界留痕），
    空闲窗口"层级没动"必须能直接读出来，而不是靠 MutationObserver 反推。
    **属性自洽不变式**：`fit()` 结束时 `data-layout` 必须是它能产生的档位之一（`full|wrapped|tall`）。
    任何非档位值——退出编辑器留下的 `editing`、遮挡守卫留下的 `blocked`、或空值——都**必须触发一次重判**，
    否则该属性不匹配任何规则，`#row` 退回基础样式 `nowrap + overflow:hidden`，把超出容器的部分**静默裁掉**
    （真机实测：最后一颗胶囊右边缘 1147px，容器右边缘 738px，**409px 画在框外**）。
- **P-10 板面几何必须自己留证**：抖动无法在夹具复现（夹具数据恒定），空闲窗口的真机采样也常常错过，
    所以读数条自己记录**每次高度或成员变化**（`boardLog`：时间戳、`clientHeight`、胶囊数、层级、档位、指标序列，
    环形 24 条 + 无界总数）。同理，编辑态动作行的可见性有三种候选成因（自身裁剪 / 祖先裁剪 / 宿主控件盖在上面），
    `__goatProbe().editor` 用 `elementFromPoint` 报告动作行中心"归谁所有"以及祖先溢出裁剪链，一次读取即可定性。

- **P-11 拖动重排是"移动元素"，不是"重画这一行"**（`paint()` 的 `movable` 分支）：成员与键集不变、
  只有顺序变时，必须用 `insertBefore` 把**现有元素**挪到位，并只挪真正不在位的那几个。三条硬要求：
  - **元素身份连续**：被按下的那颗胶囊的 DOM 节点要从按下一直活到松手。整行 `innerHTML` 重写会让
    `drag.node` 变成游离节点，`onDragMove` 下一拍直接 return——真机后果是**一次拖动最多只能跨一格**，
    抬起态 `dragging` 也随旧节点一起消失（"没有视觉反馈"的成因）。`patchNode()` 因此把 `dragging`
    和 `shed` 一样列为**不得撤销**的类：前者属于指针，后者属于宽度阶梯。
  - **让位必须看得见**：邻位胶囊用 WAAPI（Web Animations API）`animate()` 做 FLIP
    （First-Last-Invert-Play：先记首位置，挪完再反向位移回去，然后放开）160ms 平滑滑开；被拖的那颗
    不播放动画，改为抬起态（实线强调色描边 + 阴影 + `cursor:grabbing`）。
    `prefers-reduced-motion` 下不播放，直接落位（P-8 末句：不得用 JS 动画绕过媒体查询——这里是"媒体查询
    命中就完全不播"，不是绕过）。**不许**用内联 `style.transform` 实现：那会在每次轮询里留下属性写入，
    与"空闲零变更"的既有不变式冲突。
  - **松手必须结束**：`pointermove/pointerup/pointercancel` 挂在 `window`（外加 `setPointerCapture`），
    因为释放点经常落在板面外（输入框、消息流、窗口外）。只听 `row` 内的 `pointerup` 会把拖动**悬空留着**，
    此后光标不按键扫过板面就继续重排。另：`buttons === 0` 的 move 一律判定为"已松手"并收尾；
    一次拖动跨过了胶囊就抑制紧随其后的那一次 `click`——`×` 悬浮按钮外探 5px 压在邻颗胶囊上，
    否则"拖完顺手隐藏了一个指标"。
  判定跨格用**每帧一次**（`requestAnimationFrame` 合流）且只对"想要的顺序确实不等于当前顺序"才提交，
  空转的 move 不产生任何 DOM 写。回归用例：`check_widget.py` 的 `drag: one press crosses several chips
  and keeps its own element`、`drag: the held chip is visibly lifted and neighbours slide into place`、
  `drag: releasing outside the row ends it, and a bare hover never reorders`（三条都在旧构建上做过变红验证）。

- **P-12 编辑态禁止文本选择，任何状态都不得出现文本光标（I-beam）**：拖动重排与"选中一串数字"在指针
  手势上完全同形，浏览器一旦把按下-移动解释成选择，整块板面会被涂成选区色（真机截图实证）。
  落点：`:host{cursor:default}`（I-beam 只在 `cursor:auto` 的文本框上出现，读数条全是文本框，
  所以归一化放在宿主元素上继承，而不是逐颗胶囊补），`:host([data-edit="on"]) #row{user-select:none
  -webkit-user-select:none}`。**退出编辑后必须恢复可选择**（复制数字是真需求），且此时选择文本仍只用箭头光标。
  验证口径：合成 `PointerEvent` **触发不了原生选择**，所以"没有选区"这类断言若用派发事件来写就是空断言；
  必须走 CDP `Input.dispatchMouseEvent`（浏览器自己的命中测试与选择机制）。夹具两条：
  `drag: a real press-drag never selects the board's text in the editor`（v18 上实测选中文本 ⇒ 红）、
  `reading: outside the editor the numbers stay selectable, with an arrow caret`（v18 `caret:auto` ⇒ 红）。

## 7. 故障 × 防护 × 验证

| 情况 | 防护 / 恢复 | 验证方式 |
| --- | --- | --- |
| 宿主正常启动（无端口） | 守护静默等待，不重启宿主 | `watchdog` 状态自述 + 锁文件 |
| 端口文件消失 / 换端口 | 每轮重新读首行，端口变则重连 | `test_inject.py` 端口变化用例 |
| 脚本早于 `.cc-composer` 执行 | 心跳重试挂载 | 预览页 `?late=1` 夹具 |
| 拖动被浏览器当成文本选择，整块板面涂成选区色 | 编辑态 `user-select:none`，`:host{cursor:default}` 归一光标（见 P-12） | 夹具两条真鼠标输入断言（CDP `Input.dispatchMouseEvent`，v18 上红） |
| 宿主重渲染拆掉节点 | MutationObserver + 1 秒心跳重新锚定；重挂前按 N-4 领养校验，只认自己建的节点 | `test_widget.py`、`check_widget.py` |
| 上一任闭包没死，两个绘制者写同一行 | 接管一律先 `__goatDispose()`（含 `visibilitychange`），`render()` 只写 `state.node` | `painter: the row stays exactly the layout across every poll`（基线红） |
| 拖动一次只能挪一格、没有反馈 | 顺序变化用 `insertBefore` 移动现有元素；`dragging` 列为不可撤销类；邻位 WAAPI 让位 | `drag: one press crosses several chips…`、`drag: the held chip is visibly lifted…`（基线红） |
| 在板面外松手，之后光划过就重排 | `pointermove/up/cancel` 挂 `window` + `setPointerCapture`；`buttons===0` 即收尾 | `drag: releasing outside the row ends it…`（基线红） |
| 脏 DOM 被固化成用户布局 | 提交前按可见集合校验（重复/缺项即作废），读存储去重 | `commit: a duplicated or missing metric…`、`storage: a duplicated order entry…` |
| 候选 composer 多于一个 | 拒绝挂载，等下一轮（**待实现**，见 A-2） | 多节点 fixture |
| 锚点存在但不可见 | 先按四条件过滤候选再判歧义（**待实现**，见 A-3） | 隐藏面板内的第二个 composer fixture |
| 切换 thread 时旧响应晚到 | 代次 + 会话标识双校验才写状态；异常/`finally` 不改新会话（**待实现**，见 A-5） | 晚到响应、异常、卸载三个 fixture |
| 锁文件 / `probe.json` 半写或损坏 | 同目录 temp → `os.replace`；损坏 JSON 忽略并继续显示，注明未保存（见 L-6） | 并发写入、截断文件、磁盘失败用例 |
| 条已隐藏仍可被读屏念出 / Tab 落入 | `blocked` 同时置 `inert` + `aria-hidden`（**待实现**，见 N-5） | 键盘 Tab 序列 + 无障碍树快照 |
| 矩形相交但实际未遮挡 | 交集中心与四角 `elementFromPoint` 命中测试（**待实现**，见 N-5） | 覆盖 / 未覆盖两种 fixture 红绿对照 |
| 进度条无语义 | `role="progressbar"` + `aria-valuenow/min/max` + `aria-valuetext`（**待实现**，见 P-8） | 无障碍树断言 |
| 窗口变窄 / 中英切换 / 换主题 | 阶梯降级 → `blocked`；语言与主题心跳重绘 | 预览页三按钮夹具 |
| 数据缺失 | 不渲染胶囊（禁止折算成 0） | `test_live.py` + D-2 |
| 两个守护并存 | 锁文件抢占，失败方立即退出 | 手动并发 + 锁文件单测 |
| 宿主自动更新改了 DOM | 识别不了就不挂载，并留自述，不猜锚点 | 真机回归清单 |
| 卸载 | `__goatDispose()` + `removeScriptToEvaluateOnNewDocument` + 删 Run 键 | dispose 前后宿主样式一致 |

| 验证工装跑完留下 headless 浏览器（吃满内存与磁盘） | `shutdown_browser()`：CDP `Browser.close` → 等它自己退出 → 只在不退时 `taskkill /T /F` 杀整棵进程树 → 带重试删 profile；启动加 `--disable-session-crashed-bubble`/`--hide-crash-restore-bubble` 断掉崩溃自重启 | 修复前后各跑一次并计数：基线 `git archive` 副本 +1 profile/+7 进程，工作树 +0/+0（异常路径同验） |

## 8. 与上游 Antigrative Dashboard 的规范差异（不照抄清单）

| 上游规范 | 我们是否照抄 | 原因 |
| --- | --- | --- |
| `setMarkup()` 手写 createElement 模板编译器，拒 `on*`/`src`/`href`/`srcdoc` | **不照抄机制，照抄约束** | 上游宿主强制 Trusted Types；Command Code CSP 无 `require-trusted-types-for`（§1）。我们用 `innerHTML` 拼串，但必须守住 D-3 的值类型白名单并加回归测试 |
| 只认稳定 ID + 语义控件识别输入区 | **照抄** | 同一理由：宿主双语、类名比文案稳（A-1） |
| 生命周期外包给宿主插件容器 / 扩展宿主 | **部分照抄**（2026-10-12 复核）：`mcp.json` 的 `transport:"stdio"` 条目就是宿主托管的 sidecar，可替代我们的 HKCU Run 键 + 自研守护；但 `contributes`/扩展宿主确实没有，且**MCP 子进程拿不到渲染层** ⇒ 读数条 UI 仍走 CDP | 服务侧可外包，界面侧不可；见 `docs/MCP-SIDECAR.md` |
| aux pane 原生面板 | **不能** | 宿主无原生面板容器；也正因如此面板被废弃 |
| 免端口 `statusLine` 通道（其 CLI 路线） | **不能** | Command Code 主进程无该扩展点（§1 否证） |
| 宿主内 RPC 需凭据 + 端口归属 + PID 谱系校验 | **不需要** | 我们走 preload 桥 `window.electronAPI`，读数条就在 renderer 内 |
| 硬锁 `SUPPORTED_VERSIONS={'2.19.1'}` 的补丁适配器 | **不采用** | 见 N-2；改为"识别不了就不挂载"。**2026-10-12 复核澄清**：这个锁只存在于已退役的 `compat/patch-loader.py`（不匹配即 `raise`，`COMPATIBILITY.md:74` 限定为"仅为 Windows 2.19.1 历史迁移"）；上游主路线**不写宿主目录**（`cli/lifecycle.py:1` "No desktop mutation"），所以它能自由更新——与版本锁无关 |
| 宿主插件容器免掉的是"自研守护"，**不是端口** | 澄清（此前本仓库表述有误） | `COMPATIBILITY.md:18`：「插件需通过 loopback renderer 端口挂载：完整退出后带 `--remote-debugging-port=0 --remote-debugging-address=127.0.0.1` 参数启动。**普通启动不会自动打开此端口**」⇒ 上游与我们的约束同构 |
| 上游对"日常双击"给出的官方解法 | **照抄**（= 本仓库 S1） | `README.zh-CN.md:63`「将参数加到快捷方式目标末尾，后续继续使用该快捷方式」、`ide/README.md:43`、`GITHUB_RELEASE.md:43`；且 `ide/README.md:45` 独立确认了同一条坑：「已经运行的 IDE 用新参数启动不会重配置现有进程」 |
| 白名单数值持久化、不存正文/邮箱/凭据 | **照抄并收紧** | D-5 |
| 面板与读数条共用一个 collector | **不采用** | 面板废弃；单一数据源减少一处失败面 |

## 9. 验证边界（不许据此报"跑通"）

1. ~~Electron 43.3.0 是否接受 `--remote-debugging-port`~~ —— **已验证（2026-10-11 真机）**：接受，
   `--remote-debugging-port=0 --remote-debugging-address=127.0.0.1` 启动后 1.5 秒生成 `DevToolsActivePort`，
   实测随机端口 5980，且 `netstat` 归属校验判定为 `Command Code.exe`。握手另需 N-8 的免 Origin 规则。
2. 线上 DOM 的锚点与挂载 —— **已验证（真机注入 + `__goatProbe()` 回读，2026-10-11）**：
   `mounted=true`、`host=true`、`layout=full`、`state=live`；块流兄弟插入成立
   （读数条 848×28 位于 `top=689`，`.cc-composer` 起于 `top=717`，`overlaps=false`）；
   额度三窗、请求数、TOKEN、费用、均价共 7 项按真值渲染（实测上限 5h=3、周=6）。
   速度类胶囊**缺席**而非显示 0，符合 D-2；它们要等自采样实现（见 §4 D-7 与 §9.5）。
3. ~~三个接口的真实返回结构~~ —— **已验证**，见 §1 的信封与字段行。由此得到两条硬约束：
   `sessionStats` 没有任何时长字段 ⇒ 速度类指标只能靠**读数条自行采样**（D-2 要求样本不足时显示不可用而不是 0）；
   `sessionContextBreakdown` 对当前线程报错 ⇒ 不得作为数据源。
4. ~~`widget.js` 把信封本身存进 `state.host`~~ —— **已修**（连同 `mount()` 里"display 不是 flex 就拒绝挂载"
   这第二个静默失效一起；后者使真机永远挂不上且零报错）。工装同步补了三条用例：信封必须读出额度、
   不可见的 composer 不算锚点、两个可见 composer 时一律不挂载。
5. 本轮已落地并在真机复查的条款：**A-5 请求代次**、**N-5 命中测试 + `inert`/`aria-hidden`**、
   **P-7 布局指纹**、**P-8 无障碍语义**（真机回读：`role=progressbar` 带 `aria-valuenow/max`、
   `aria-busy=false`、可见时 `inert=false`、指纹串含 `w847xh27|src=-+host+-`）。
   另外新发现并修掉两个只在真机才会现形的缺陷：
   - `WidgetInjector.attach()` 持会话锁后又经 `evaluate()` 取同一把非可重入锁 ⇒ **首次 reconcile 自死锁**
     （守护进程每 2.5 秒 reconcile 一次，等于必然触发）；锁改为可重入。
   - `WidgetInjector.evaluate()` 把 `awaitPromise` 写死为 `false` ⇒ 任何异步表达式只拿到 Promise 句柄
     （表现为"返回空对象"），跨进程诊断必须直连 `CdpConnection` 并显式 await。
6. **单实例复测已通过（2026-10-11 22:4x，`docs` 更新）**：只保留一个服务实例后，
   `inject.lock` 的 pid 与 18927 端口的持有者**是同一个进程**，`live.rates.hit_rate_pct = null`，
   页面上 `cache/gen/rate` 三颗胶囊缺席、**没有画出"命中 0.0%"**；月份胶囊为
   `Month 2.84/10` + `aria-valuenow/max = 2.84/10`，与 `live.monthly = {cap:10, remaining:7.158}`
   逐项吻合（`used = cap - remaining`），指纹尾部 `mplan` 记下来自套餐池；`burn 0.00 cr/h`
   来自自采样（指纹含 `sample`），宿主端口 10316 归属校验为真。
   上一轮把 `cap:70` 当作权威上限是错的：那是遗留 `--demo` 实例的合成池；真实账号月度池就是 10。
   另记一条判据教训：`.venv\Scripts\pythonw.exe` 是 PEP 733 重定向壳，一次启动必然产生
   壳 + `C:\Python\Python313\python.exe` 两个 PID，**按命令行数进程会把 1 个实例误读成 2 个**，
   要按 `ExecutablePath` 区分。
7. 曾经开着的口子：命中率胶囊在真机上未出现。同页直连测得 `sessionStats` 返回
   `{success:true, stats:{branches,contextUsage,messages,tokens{input,output,cacheRead,cacheWrite}}}`
   且 `session=known`，所以通道没问题；两种候选解释是当前会话的 token 计数为 0（分母为 0 ⇒ 按 D-2 正确地不出胶囊）
   或 `stats` 拉取时机。**未定性之前不得声称命中率已跑通**，下一步用 `probe.statsAt` 区分这两者。
   ~~`gen`/`rate` 两个胶囊按 D-7 属意保持"仅本地服务供给"~~ —— **已被 D-12 取代**：`gen` 删除，
   `rate` 仍属服务供给，但服务的窗口里没有 usage_records 时它按 D-2 正确缺席（实测 `basis:"none"`）。
4. 任何"注入成功"的结论必须先有真机 `__goatProbe()`（`widget.js:1004`）回读 `mounted` 为真。
5. **默认验证路径是免重启的同源夹具** `app/web/widget-preview.html`：复刻宿主 composer 的 DOM 与主题变量、
   mock `electronAPI`，加载**同一份** `app/web/widget.js`。绝不为验证而重启用户正在使用的 Command Code；
   需要真机时把重启那一步交给用户点的脚本。

**缺陷已修复并现场回归通过（2026-10-11 深夜）**：`/api/quit` 曾答应退出却不退出。
`GaugeService.request_stop()` 只调用 `self._stop_callback()`，而 `run_foreground` **从未安装该回调**
⇒ 整条 quit 路径是空操作。我先前把它归因于等待循环（`b5a5f97` 改成检查 `stop_event`）——必要但
**不充分**，绿测仍失败；也猜过 `request_stop` 不存在——错（`server.py:1070` 就在）。正确修法：
在 `run_foreground` 里把回调接上（让它设置 `_stop_event`，注入线程与等待循环随之退出、锁随之释放），
并加一条真机回归"quit 之后 entry.py 进程数必须为 0"。修法：`request_stop()` 自己置 `_stop_event`（回调只作为宿主的额外拆卸步骤）。
   现场回归：quit 后 `entry.py` 进程数 **0**、`inject.lock` **已释放**（说明 `finally` 真的跑完，
   不是被杀）、`--status` 报 `gauge.reachable=False`。这条现在可作为唯一停止手段。

**重影已量化归零（2026-10-12）**：`tools/check_widget.py` 在夹具页加载生产 `widget.js`、
用 MutationObserver 数空闲窗口的 DOM 变更。结果 `count=0`（起点 19），`shapeChanges` 停在
两条启动过渡（`waiting` → 真实胶囊）。**测量教训**：空闲窗口必须从"首帧数据落地、板面稳定"之后
才开始计数，否则会把一次性的 `waiting` 替换算成周期性重影——我第一版就是这么误判的。
工装因此要求：观察期内 `shapeChanges` 不得增长，且数值变化必须就地打补丁（节点全部存活）
同时数字与条宽确实更新。

**重影归零第二回合：降级改为层级（2026-10-12，`31e9d5f`）**
- **根因**：真机 12 秒静置窗口测得 9 次变更 = `child:3` + `attr:6`，`attr:class@SPAN.bar`/`SPAN.cd`
  全部出自 `fit()`：它每轮先清空 `shed`、再按"未降级宽度"重新测量、再把该加的 class 加回去。
  于是只变数字的轮询也要付一次完整的降级循环。
- **修复**：`fit()` 改持一个层级（见 P-9）。轮询只读式测量、按证据向下走；向上升级只在真实 resize
  (`refit()`) 或整行重建 (`paint()` 全量写分支置 `state.climb`) 时才做。`setLevel()` 自带**无上限计数**
  `levelChanges` 与有界环形日志 `shedChanges`，"层级没动"因此可在环形缓冲区写满之后仍然被证明。
  协议版本 11 → 12（`WIDGET_VERSION` 与 `widget.js` 的 `VERSION` 必须同笔提交，两侧要一致）。
- **效果验证**：
  - 真浏览器夹具 `tools/check_widget.py` 从 6/6 扩到 **9/9**（`e5d698f`）：新增窄列降级、
    **降级板面 8 秒零变更 + `levelChanges` 不增长**、放宽后装饰回归且指标集不变。
    窄列实测 `wide{level:0,shed:0} → {layout:tall,level:6,shed:6,metrics 不变}`，只记 **1** 次层级迁移。
  - 真机（未重启宿主，只重启本仓库服务；`entry.py --watch` 路径）v12 回读
    `state=live layout=full level=-1 metrics=7`，12 秒静置窗口 **count=0（child/attr/text/style 全 0）**，
    `levelChanges` 增量 0、`shapeChanges` 停在启动的 2 条；状态点仍是 `rgb(120,222,143)`。
    测量脚本只读：直连既有 DevTools target，**不另起注入器**（避免已记录的多注入器互相覆盖陷阱）。
  - Python 套件 134 项：仅 `test_credentials.test_saved_session_survives_restart` 失败，
    属本机 CC Switch 存在真实 key 的既有基线，非回归。
- **仍未做**：上一轮那个 `child:3`（指标在场与否随数据抖动导致整行全量重写）**本轮窗口内未复现，但没有被针对性修改**，
  不能记为已消除；真机窄列降级也**未实测**（要动用户正在用的 composer 宽度），该形态只由夹具证明。
  待办进度：① 成员与数据可得性解耦 **已完成**（`43ae9b2` 会话内只增不减 + `—` 占位）；
  ② `tok/s` 换成上下文占用 **已完成**（D-12，`ctx` 胶囊，真机工装 11/11 断言其来自 `sessionStats`）；
  ③ 面板对齐 仍未开始。

**登录自启动此前从未生效（2026-10-12，`d515db5`）**
`install.ps1` 写进 HKCU Run 键的是 `entry.py --watch`，而 `entry.py` 把这个名字**原样转发**给守护进程；
守护进程自己的循环叫 `--follow`，argparse 直接拒收 ⇒ 开机自启动在第一次轮询之前就死掉，
且因 `pythonw` 无控制台而**零可见症状**。其它模式标志（`--status/--launch/--probe`）都做了名称翻译，
所以手工跑起来一切正常，坏的恰好是没人手工跑的那条。
- **修复**：`entry.py` 把 `--watch` 映射为 `--follow`；公开名保持不变，Run 键与 `uninstall.ps1` 的匹配串无需改动。
- **效果验证**：新增 `EntryCLITests` 把 `entry.py` 转发的标志交给**真实的** `watchdog.main` 解析——
  两个文件各自持有 argparse，过去没有任何东西检查它们是否一致。
  红/绿对照：旧转发串 `--watch` ⇒ `SystemExit 2 / unrecognized arguments`；新串 ⇒ `follow()` 被进入、rc=0。
  现场以 `Start-Process pythonw entry.py --watch` 复跑，`--status` 的 `watchdog_pid` 由 **0 变为 44876**——
  这是本仓库第一次观察到自启动路径真的把守护循环跑起来。

**高度抖动定因与被否证的第一假设（2026-10-12，`120fe17` `9af969d` `43ae9b2` `75598ba` `aab5a9b`）**
- **我先错了一次**：class 写入归零后用户仍报抖动，我假设"整行重建会置 climb ⇒ 层级在临界宽度反复翻转"是主因。
  用几何工装做同一实验直接否证：**`level` 全程冻结在 0，行高仍 14 次跳变（28↔49）**。
  教训：只数 DOM 写入的工装答不了几何问题，故新增 `tools/trace_height.py`（采样高度/行数/档位/层级/成员序列）。
- **真因**：派生指标（`gen/burn/cache/rate`）按"数据够不够"整颗进出，一颗 60–90px，跨过换行点整块高度翻倍；
  它恰好发生在用户打字时（采样窗口滑过阈值）。编辑态里同一次重排会把 取消/保存/添加 挤出所在行。
  ⇒ 治理口径见 D-2 补充（会话内只增不减 + `—`）与 P-9（层级只由宽度重判）。
- **效果验证（同一实验红绿对照）**：
  - 数据断档时：v13 掉了 `gen+burn+rate` 三颗胶囊；v14 十一颗全在、`idle=3` 占位、行高恒定（`check_widget.py` 第 10 条断言）。
  - 临界宽度 + 每秒断档：v13 行高 `51↔54` 共 9 次；v14 **恒定 54，0 次**。
  - 夹具 `tools/check_widget.py` **10/10**；Python 136 项仅 `test_credentials` 既有基线失败。
  - 顺带卫生修复：`patchNode` 不再为"生成串里没有 style"的节点写空 `style=""`（每次重建少 12 次属性写入）。
- **待做（不记为已跑通）**：① 真机复测需用户重开 Command Code，之后**只读** `__goatProbe().boardLog`
  与 `.editor`，不许为验证动宿主；② 编辑态按钮不可见的**宿主侧成因仍未定性**——夹具里动作行
  `owner=ours`、祖先裁剪链为空，说明差异在宿主自己的 DOM，必须拿真机那一次读数来判。

**退出编辑模式后板面坍缩为一行（2026-10-13，`bbf5b3a`）**
- **根因**：上一轮我把"整行重建 ⇒ 重判层级"这个触发条件删掉时，没有审计它顺带承担的另一件事——
  它同时也是 `data-layout` 从 `editing` 回到档位的唯一路径。于是退出编辑后属性停在 `editing`，
  不匹配任何 `:host([data-layout=…])` 规则，行退回 `nowrap + overflow:hidden`，超出容器的胶囊被裁掉。
  同一类失效还潜伏在 `blocked` 上：遮挡守卫一旦置位，没有重判就再也回不来（那会表现为"看板整条消失"）。
- **修复**：`fit()` 开头检查属性是否还在档位集合内，不在就重判（这是**结构变化**，不是轮询，
  不会重新打开上一轮关掉的抖动路径）。
- **验证**：红/绿在 `git archive` 基线副本上做过——基线 `layout:"editing"`、`outside:409px`；
  修复后 `layout:"wrapped"`、`outside:-6px`（真浏览器夹具新增第 12 条断言 + 垫片 2 条）。
  真机 v17 回读：`layout=wrapped height=50 outside=-5 metrics=10+ctx idle=1`。
- **教训（对应"删功能要审计它的守卫"）**：移除一个触发条件前，先问它还在顺手掩盖什么。
  这次它掩盖的是一个属性生命周期问题，代价是用户屏幕上 409px 的不可见内容。

**编辑模式拖动：一次只挪一格、无反馈、退出后指标整颗消失（2026-10-13，`a25d782` `291184b`）**
- **根因分析（两组缺陷叠加，不是一条）**：
  1. **两个绘制者写同一行**。重入守卫只问"这个 id 的节点在不在"，于是宿主 React 摘掉节点之后的一次再注入
     会**再起一个闭包**，而上一任的心跳/轮询/`visibilitychange` 还活着，两边都按 `getElementById` 找节点画。
     真机留证（只读一次 `__goatProbe()`）：内存里 `layout.order` 干净、`hidden=[avg,requests]`，
     屏上却是 `week` 两颗、`requests` 在板、`cache` 消失、状态点被指标覆盖，
     且 `boardLog` **同一秒记到两种不同成员**（`t=1791865064` 两条）。
     这正是 `blocked↔wrapped` 每秒翻转（"退出编辑后整条看不见，几秒后自己回来"）的成因：
     谁后画谁定档位。
  2. **拖动一跨格就整行重建**。`moveMetric → render → paint` 走 `row.innerHTML` 全写，
     `drag.node` 变游离节点，`onDragMove` 下一拍 `parentElement` 为空直接 return ⇒ **一次拖动最多跨一格**；
     `dragging` 抬起态随旧节点一起消失 ⇒ "没有视觉反馈"；`pointerup` 只听 `row` ⇒
     板外松手后拖动**悬空**，此后不按键光划过板面仍会重排（夹具实测复现）。
  3. **脏 DOM 会被固化成用户布局**：拖动结束用 `commitDomOrder()` 从 DOM 反读顺序，既不去重也不校验集合，
     于是 (1) 造成的重复 `week` 被写进 `layout.order` 并挤掉 `cache`；`loadLayout()` 读存储同样不去重 ⇒ 永久。
- **修复计划（对应条款 N-4 / L-5 / P-6 / P-11）**：闭包只画自己 `build()` 的节点（`state.node` 身份判定，
  `render/refit/tick/mount` 全部按所有权拒绝领养）；接管一律先 `__goatDispose()`，dispose 补齐
  `visibilitychange` 的撤销；纯重排改为 `insertBefore` 移动现有元素（`paint` 的 `movable` 分支），
  `patchNode` 把 `dragging` 与 `shed` 同样列为不可撤销；邻位用 WAAPI 做 FLIP 让位、被拖者抬起描边+阴影，
  `prefers-reduced-motion` 不播；`pointermove/up/cancel` 挂 `window` + `setPointerCapture`，
  `buttons===0` 即收尾；跨格判定每帧合流且"顺序没变就不 render"；拖动跨过后 500ms 内的 `click` 作废
  （`×` 外探 5px 压在邻颗上，否则拖完顺手隐藏一个指标）；提交与读存储都按可见集合校验/去重。
  版本 17 → 18（`widget.js` 与 `inject.WIDGET_VERSION` 同步）。
- **效果验证**：
  - 真浏览器夹具 `tools/check_widget.py` **16/16**（新增 4 条：一次跨多格且元素身份保持、
    抬起态+邻位动画、板外松手后光划过不再重排、再注入后跨轮询成员恒等于 layout）。
    **红/绿对照**：同一工装跑 `35f08fb` 的 widget.js ⇒ 这 4 条**全部变红**（`same:false`、`animates:0`、
    `held:null` 残留、`drift` 非空），不是空断言。
  - 垫片 `tests/test_widget.py` **96 项全绿**（新增 4 条：存储去重 ×2、提交校验 ×2；基线上 4 条全红）。
    顺带修正夹具自身：垫片把"二次注入"建成新 context，等于模拟了一次页面重载而非同页再注入，
    改为复用同一 window 才走到接管分支。
  - Python 全量 161 项：仅 `test_credentials.test_saved_session_survives_restart` 这一条**既有基线失败**
    （本机存在真实 CC Switch key，与本轮无关）。
  - 夹具截图：`goat-edit-mid-drag.png` 中 `月 0/7.20` 呈强调色实线描边 + 阴影，位于 `5h` 与 `周` 之间。
  - **待做（不记为已跑通）**：真机生效需要**重启本机 gauge 服务**让新 `WIDGET_VERSION` 进内存——
    现在跑着的进程仍带 17 的常量，而 `widget_source()` 每次从磁盘读 18 的源码，
    这个组合会导致每轮 attach 都 dispose+重建（看板反复重挂）且 `__goatUpdate` 因版本不符而丢弃推送。
    重启后按 N-4 只读回读 `__goatProbe()`：期望 `version=18`、`ownsNode=true`、
    DOM 成员序列 === `metrics.order` 去掉 `metrics.hidden`、`.chip.dot` 恰好 1 颗、`boardLog` 不再出现
    同一秒两种成员。**这一步需要用户点头或自行执行，绝不为验证重启 Command Code。**
- **本轮总结**：用户报的三件事（拖动丢数据 / 退出后整颗不可见 / 无反馈不让位）里，前两件是
  **同一个根因**（多绘制者 + 脏 DOM 固化），第三件是拖动自身的三个缺陷。修完最要紧的一条不变式：
  **"全页一份"必须由"谁拥有这个节点"来判定，而不是"有没有这个 id"**——历史上同类假症状
  （`gen 复活 / cache 重复 / rate 出现两次`）也是这一类，判据顺序仍是先问"谁还在写这个 DOM"。
- **commit**：`a25d782`（产品码）、`291184b`（两套工装）、本条文档随附。回滚点 `35f08fb`。

**拖动把板面涂成选区色（2026-10-13 下午，`55e17d8` `570c765`）**
- **根因**：按下-横移这一手势在浏览器看来既可能是拖动也可能是**选择文本**，而 `#row` 里全是文本框；
  读数条此前从未声明 `user-select`/`cursor`，于是编辑模式每次跨格拖动都顺带产生原生选区
  （用户截图：`Month/Burn/Cache/Ctx` 整片选区色）。
- **修复**：`:host{cursor:default}`（I-beam 只在 `cursor:auto` 的文本框上出现，归一化放在继承根上，
  一处覆盖全部胶囊与数字）；`:host([data-edit="on"]) #row{user-select:none;-webkit-user-select:none}`。
  **正常模式不加限制**——退出编辑后仍可选中复制数字，只是光标是箭头。版本 18 → 19。
- **效果验证（关键：换了工装才有证据）**：第一版断言用合成 `PointerEvent` 派发拖动，夹具里 `selected`
  恒为空串 ⇒ **在 v18 上也"通过"**，是空断言；原生选择只认浏览器自己的输入管线。
  改用 CDP `Input.dispatchMouseEvent`（mousePressed/mouseMoved/mouseReleased）后：
  v18 **红**（实测选中 `消耗\n—\n×\n命中\n46.6%\n×\n吞吐\n—\n×\n`，与用户截图同形），
  v19 **绿**（`select:none` + `selected:""`）；另一条 `reading: outside the editor…arrow caret`
  同样 v18 红（`caret:auto`）/ v19 绿（`caret:default`，且整条仍可框选）。
  夹具 **18/18**、垫片 **96/96**、Python 161 项仅 `test_credentials` 既有基线失败。
  截图两张：编辑态拖动中（抬起描边+阴影、零选区）、正常模式刻意框选（整条被选中、箭头光标）。
  顺带修掉夹具自身一个遮蔽性缺陷：`Harness.evaluate` 从 `result.exceptionDetails` 读异常，
  而 CDP 把它放在 `result` 的**同级** ⇒ 页面抛错被静默成 `None`，本次就是它把一个语法错误
  （CSS 模板串里写了反引号）伪装成"看板没挂载"。
- **本轮总结**：手势歧义这类问题，**替身事件表达不了机制就必须换成真输入**；
  一条断言若在修复前的构建上也能通过，它就不是断言。
- **commit**：`55e17d8`（产品码 + 夹具异常读取修正）、`570c765`（真鼠标输入断言）。回滚点 `0b080b5`。

**验证工装泄漏 headless 浏览器，把机器吃干（2026-10-13 夜，`17e6f18`）**
- **根因分析（四件事叠成一条死亡螺旋，第 1 条才是主因）**：
  1. **句柄被丢弃，收尾语句是死代码**。`tools/check_widget.py` 的 `launch_browser()` 只返回端口号，
     `Popen` 对象在函数里就地蒸发；而 `main()` 写的是 `process = None` + `finally: if process is not None:
     process.terminate()` ⇒ **那行永远不会执行**。`tools/trace_height.py` 复用同一函数，`finally` 里
     连 terminate 都没有，只关静态服务器。**每跑一次 = 泄漏一整个浏览器**。
  2. **只杀根进程也不够**。477 个 Edge 进程 / 61 个浏览器 ≈ 7.8，正是"根进程 + 渲染器 + utility +
     crashpad"的规模；`terminate()` 即便生效也只收根进程，其余被重新收养后继续占内存。
  3. **持久 profile 会让泄漏自乘**。`git log -S'restore-last-session' --all` 与 `-S'"--restart"' --all`
     **均无命中** ⇒ 用户看到的这两个标志不是本仓库任何版本写过的，而是 **Chromium 自身的崩溃后自重启**
     机制加上去的：内存压力 ⇒ 浏览器崩 ⇒ 带旧 profile 自重启 ⇒ 更缺内存 ⇒ 重启更勤。
  4. **profile 从不删**：58 个残留目录共 **25GB**，单个最大 490MB（Edge 把缓存写进临时 profile）。
- **归因修正（这条链原先被记在开机自启头上，实测不成立）**：`install.ps1` 确实**无条件**写
  `HKCU\...\Run\GOATGaugeWatchdog`（`--serve --inject`，`--auto-launch` 才是要开关），但它拉起的运行时路径
  **创建不了 headless Edge**：`tools/` 从不被 `app/`、`entry.py` 导入；`app/watchdog.py` 里浏览器引用 0 处
  （它只负责带调试端口拉起 Command Code 宿主）；重启登录那一刻（10-12 22:4x）**新建 `goat-*` 目录 = 0 个**。
  浏览器 profile 的创建批次落在 10-13 02:01/02:03 与 12:51–14:16，对应的是**开发会话里反复跑这两个工装**。
  即：自启与本次内存事件无因果关系，泄漏源是工装自身。（现场该 Run 值已不存在，即已被 `uninstall.ps1`
  或手动删除——这就是"止血"实际止掉的东西，而工装漏点当时仍在。）
- **修复计划**：`launch_browser()` 改返回 `(端口, Popen)`，并新增 `shutdown_browser(连接, 进程, profile)`：
  先发 CDP（Chrome DevTools Protocol，浏览器远程调试协议）`Browser.close` 让浏览器自己收整棵树，
  `wait(8s)`，超时才 `taskkill /T /F /PID` 兜底，然后带重试删 profile（Windows 上句柄释放有延迟），
  删不掉就打 `WARN` 到 stderr 而不是静默；`launch_browser()` 自己的失败路径（等不到 `DevToolsActivePort`）
  也先收尾再抛——否则一次失败启动就是一个孤儿。启动参数补
  `--disable-session-crashed-bubble` + `--hide-crash-restore-bubble` 断掉第 3 条。`trace_height.py` 同步。
- **效果验证（红绿对照，数字可复现）**：
  - **红**：`git archive HEAD` 导出修复前副本跑一次 ⇒ profile 目录 1396→1397、
    `harness_processes` **0→7**、带 `DevToolsActivePort` 的 profile 61→62。与用户看到的 61/477 同形。
  - **绿**：工作树跑 `check_widget.py` ⇒ 18/18 断言全绿，目录 47→47、`harness_processes` 恒为 0；
    `trace_height.py --seconds 4 --width 700` ⇒ 同样 +0/+0。
  - **异常路径**：`check_widget.py tools`（缺 `widget-preview.html`，中途 `TypeError` 抛出）⇒
    收尾照样跑完，`harness_processes=0`、目录 1395→1395。
  - Python 全量 **161 项**：仅 `test_credentials.test_saved_session_survives_restart` 这一条既有基线失败
    （本机存在真实 CC Switch key，与本轮无关）。实验留下的孤儿与基线副本已按同一 PID 判据清回 1395/61/0。
- **本轮总结**：**"有 finally"不等于"资源被回收"**——`finally` 里引用的变量从未被赋值，静态读代码时最容易
  滑过去；判据是"跑一次之后计数是否 +1"，不是"代码里有没有 terminate"。其次，**症状的起始时间不等于成因**：
  登录时刻与内存事件重合，但 profile 的创建时间戳才是因果链上的证据，而 `--restore-last-session` 这类
  浏览器自己加的 flag 不能反推成"旧版本代码的实例"。
- **遗留（未做，需用户点头）**：① `tests/test_watchdog.py` 9 处 `tempfile.mkdtemp(prefix="goat-*")`
  只 `addCleanup(environment.stop)`、从不删目录 ⇒ Temp 里约 **1327 个空目录**（只占磁盘 368K，不占内存）；
  ② 已漏的 **25GB / 58 个浏览器 profile 仍在** `%TEMP%`，未清扫；
  ③ 开机自启按用户决定改为"只跟随 Command Code 启停、不再设计自启"，但 `install.ps1` 目前仍无条件写 Run 键，
  完全去掉自启意味着登录后读数条不会自己回来——这一条需要单独定夺。
- **commit**：`17e6f18`。回滚点 `0031774`。


## 10. 安装与卸载的边界

- `install.ps1`：探测解释器 → 建项目内 `.venv` → 装 `websocket-client`（官方 → 清华 → 阿里逐个回退，
  **绝不关闭 TLS 校验**）→ 写 HKCU Run 键（**`entry.py --serve --inject`**，一个进程同时干服务、注入、
  可选的一次性带端口拉起宿主）→ 打印状态。**幂等**，重复执行只补齐。
- `uninstall.ps1`：删 Run 键 → 停服务与守护 → `__goatDispose()` → 删 `.venv`。`%LOCALAPPDATA%\GOATGauge\`
  默认保留，`-Purge` 才删。不碰宿主安装目录、不碰宿主 CSP。
- **L-7 自启动必须注册"完整形态"**：只跑 `--watch` 时服务缺席，后果是**静默降级**而非报错——
  月份分母从套餐上限 `mplan`(10) 退成"本周期实测最大余量" `mest`(7.16)（屏上 `0/7.16`），
  且 `gen`/`rate` 这类按 D-7 只由服务供给的指标永远不出现。2026-10-12 真机实测踩中：
  `fp` 尾部 `src=-+host+sample+mest` ⇒ 无推送。`--watch` 保留为"只要 D2 纯宿主自给、不开 HTTP 端口"的
  可选降级模式，但不再是默认注册项。`--port` 默认值同步改成 18927（`--status` 探测的就是它，
  默认 0 会让自检永远报 `reachable: false`）。
- 两者都**不**创建/改写 Command Code 的**安装目录**，也不新建开始菜单项；带端口启动是一条独立命令，
  是否使用由用户决定。唯一的例外是下面 S1：`-WithShortcut` 会改**已存在的启动快捷方式**（含机器级那一个），
  且默认关闭、可逐字还原。
- **S1（可选，默认关）：`install.ps1 -WithShortcut` 允许改"当前用户自己账户下"的那一个 `.lnk`。**
  这是上游文档给所有用户的同款做法（`README.zh-CN.md:63`「将参数加到快捷方式目标末尾」）。硬约束：
  ① 只写 `Arguments` 一个字段，且**只追加缺失的开关**（幂等，重复执行不叠加）；
  ② 首次改动前把原值写进 `%LOCALAPPDATA%\GOATGauge\launch-shortcut.json`，并**再存一份 `.lnk` 字节副本**
  （`launch-shortcut.lnk.bak`）——WScript 的 `Save()` 会重写整个链接文件，只靠 JSON 不足以兜底；
  ③ `uninstall.ps1` 逐字还原并回读校验，还原成功才删备份；`-Purge` 之前必须先还原；
  ④ `--status` 报 `launch_shortcut.state = ready|partially-ready|missing-args|absent|unreadable`，
  让"双击有没有端口"可查；
  ⑤ 必须遍历**所有存在的**同名快捷方式（用户级 + 机器级 `C:\ProgramData`）。真机踩过：只改了用户级那个，
  用户点的却是机器级那个（参数为空）⇒ 改动静默无效，看板照样不出现。机器级写入要提权，
  install 与 uninstall **对称地各请求一次**（只在真的被拒时才提权）。
  失败模式必须写清：**已有无端口实例在跑时，带参数的启动只会聚焦旧窗口**（宿主 `requestSingleInstanceLock`），
  这条与上游 `ide/README.md:45` 的说明一致。
- **S2（可选，默认关）：`install.ps1 -AutoLaunch` 让开机那一次判定里，若宿主完全没运行就带端口拉起。**
  语义是**只判定一次**：第一眼看到宿主在跑（哪怕没端口）就永久放弃拉起，否则用户主动退出 Command Code 后
  会被后台重新打开——那不再叫"跟随宿主"。该逻辑抽成 `watchdog.follow_startup_launch()`，由默认注册项
  `entry.py --serve --inject --auto-launch` 与降级模式 `--watch --auto-launch` **共用**，判定与措辞只有一处。
- **仍然禁止**：IFEO（Image File Execution Options）一类启动劫持。实测宿主的 GPU/网络/渲染子进程
  都是**同一个 exe** 带 `--type=…` 派生（`tasklist` 实据），而 IFEO 在 CreateProcess 层生效 ⇒ 它会拦下
  每一个子进程，写错就是宿主起不来；且它不随宿主更新消失，卸载器也不知道它存在 ⇒ 启动器一旦丢失，
  应用直接打不开。收益（覆盖绕过快捷方式的启动）与失败代价不成比例。
- **Run 键里那条命令必须被证明"能解析"，而不是"写着对"**：`pythonw` 无控制台，argparse 报错等于静默死亡。
  公开标志名（`--watch`）与守护进程内部标志名（`--follow`）之间的翻译是一张跨文件契约，
  只能靠一条把两侧串起来的测试守住（`tests/test_watchdog.py::EntryCLITests`）。
  验收判据是 `--status` 里 `inject_pid != 0` 且 `gauge.reachable = true`（只看到 `watchdog_pid` 不足以证明
  服务在跑——那正是 L-7 的残缺形态）；看不到进程就当没启动。

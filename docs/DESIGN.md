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
| 包名 / 版本 | `@commandcode/desktop` 0.1.51，Electron 43.3.0 / Chrome 150 | `resources/app/package.json` \[实测\] |
| 安装位置 | `D:\Program Files\Command Code\`，`resources/app` 为**未打包目录** | \[实测\] |
| 目录权限 | `out/renderer/index.html` 的 ACL 为 `BUILTIN\Users:(I)(RX)`；当前会话不在管理员组 | `icacls` \[实测\] |
| 单实例 | `app.on("second-instance", …)`（`out/main/index.js:32271`）⇒ 旧实例不退，带参数再启动只会聚焦旧窗口 | \[实测\] |
| 调试端口 | 主进程无 `app.commandLine.appendSwitch`（全文零命中）⇒ 宿主既不自开也不禁用 CDP | \[实测\] |
| 页面 CSP | `default-src 'self'; script-src 'self' 'wasm-unsafe-eval' 'sha256-zrvkeKAHvjk8g+o0GV59i098Rz4hc1p5vZJqJBHNJBU='; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-src 'none'; worker-src 'none'; object-src 'none'; base-uri 'none'` | `out/renderer/index.html` \[实测\] |
| Trusted Types | CSP **不含** `require-trusted-types-for` ⇒ 宿主未强制 Trusted Types | \[实测\] |
| 官方扩展面 | `~/.commandcode/mcp.json`（MCP，即 Model Context Protocol 模型上下文协议）与 CLI `config.json`（实测顶层键仅 `featureModels/reasoningEffort/starredModels`）；**无** `contributes`、无 `out/cli.js`、`statusline` 命中全是 agent 输出解析与 Discord 文案 | \[实测\] |
| 数据桥 | `window.electronAPI`：`accountOverview(options)` / `sessionStats(sessionId)` / `sessionContextBreakdown(sessionId)`，通道名 `account-overview`/`session-stats`/`session-context-breakdown` | `out/preload/index.js`、`out/main/index.js` \[静态\] |
| 界面锚点 | `.cc-composer-surface` > `.cc-composer` > `.cc-composer-toolbar` > 其内 `textarea[rows="1"]`；路径上无 `data-testid` | 构建产物 \[静态\]，线上 DOM \[未验证\] |
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
- **D-8 诊断自述按账号隔离且不猜测**：`probe.json` 一类自述写入
  `%LOCALAPPDATA%\GOATGauge\<accountHash>\`，哈希 = `sha256(userId)` 前 12 位。哈希隔离
  **不等于匿名化**（同哈希可跨文件关联，不许在 README 里写成隐私声明）；确认不了当前账号时**不猜、不写、
  不读别的账号目录**，离线启动也无法确认就整段跳过（上游同一立场：`docs/ROBUSTNESS.md:125,127`）。

## 5. 生命周期（守护进程）

- **L-1 一个常驻守护，登录后由 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` 拉起**（`pythonw`，无窗口）。
  宿主没有 Antigravity 那样的官方插件容器（`sidecar` + `restart_policy`）可以外包生命周期，所以只能自持。
- **L-2 状态机跟随宿主**：`等待宿主` → `等待端口文件` → `已 attach` → `宿主退出/端口消失 → detach 回等待`。
  宿主缺席时禁止做任何 CDP 请求。无端口时**静默等待**，不重启宿主、不弹通知；带端口启动是一条显式的
  `gauge-launch.cmd`（照抄上游 `ide/manage.py:37-47`：detached spawn + `CREATE_NO_WINDOW`）。
- **L-3 单实例**：用户数据目录持 `watchdog.lock`（PID + 端口 + 启动时间）。抢不到锁的新实例**立刻退出**，
  绝不杀无关进程。两个守护并存会各自注册一次预加载脚本，表现为读数条重复或互相覆盖。
- **L-4 判活禁止依赖 HTTP 端点**：面板废弃后 `/api/state`、`/api/quit` 不存在，改由锁文件 + 进程名判定。
- **L-5 幂等**：同一份源码重复注入不得产生第二个节点；源码指纹变化时才清理并替换（`WIDGET_VERSION`
  `inject.py:24` 与 `widget.js` 内部版本必须同步，不一致的 payload 直接丢弃）。
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
- **P-6 布局编辑**存 `localStorage` 的 `goat-gauge-layout-v2`；提交时只重写**当前真的有数据、真的在板上渲染出来**
  的槽位，暂时无数据的指标保持原位，否则一次偶发空窗会永久打乱用户布局。所有事件在 Shadow DOM 内委托并
  `stopPropagation`，不漏给宿主输入框。
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

## 7. 故障 × 防护 × 验证

| 情况 | 防护 / 恢复 | 验证方式 |
| --- | --- | --- |
| 宿主正常启动（无端口） | 守护静默等待，不重启宿主 | `watchdog` 状态自述 + 锁文件 |
| 端口文件消失 / 换端口 | 每轮重新读首行，端口变则重连 | `test_inject.py` 端口变化用例 |
| 脚本早于 `.cc-composer` 执行 | 心跳重试挂载 | 预览页 `?late=1` 夹具 |
| 宿主重渲染拆掉节点 | MutationObserver + 1 秒心跳重新锚定，保证全页一份 | `test_widget.py` |
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

## 8. 与上游 Antigrative Dashboard 的规范差异（不照抄清单）

| 上游规范 | 我们是否照抄 | 原因 |
| --- | --- | --- |
| `setMarkup()` 手写 createElement 模板编译器，拒 `on*`/`src`/`href`/`srcdoc` | **不照抄机制，照抄约束** | 上游宿主强制 Trusted Types；Command Code CSP 无 `require-trusted-types-for`（§1）。我们用 `innerHTML` 拼串，但必须守住 D-3 的值类型白名单并加回归测试 |
| 只认稳定 ID + 语义控件识别输入区 | **照抄** | 同一理由：宿主双语、类名比文案稳（A-1） |
| 生命周期外包给宿主插件容器 / 扩展宿主 | **不能** | Command Code 无 `contributes`、无 `out/cli.js`、无 sidecar 托管 ⇒ 只能 L-1 自持守护 |
| aux pane 原生面板 | **不能** | 宿主无原生面板容器；也正因如此面板被废弃 |
| 免端口 `statusLine` 通道（其 CLI 路线） | **不能** | Command Code 主进程无该扩展点（§1 否证） |
| 宿主内 RPC 需凭据 + 端口归属 + PID 谱系校验 | **不需要** | 我们走 preload 桥 `window.electronAPI`，读数条就在 renderer 内 |
| 硬锁 `SUPPORTED_VERSIONS={'2.19.1'}` 的补丁适配器 | **不采用** | 见 N-2；改为"识别不了就不挂载" |
| 白名单数值持久化、不存正文/邮箱/凭据 | **照抄并收紧** | D-5 |
| 面板与读数条共用一个 collector | **不采用** | 面板废弃；单一数据源减少一处失败面 |

## 9. 验证边界（不许据此报"跑通"）

1. Electron 43.3.0 是否接受该应用传 `--remote-debugging-port`（即真能生成 `DevToolsActivePort`）——**\[未验证\]**。
2. 线上 DOM 是否真有 `.cc-composer` / `.cc-composer-surface`——**\[未验证\]**（结论来自静态产物）。
3. `accountOverview()` / `sessionStats()` / `sessionContextBreakdown()` 的真实返回结构——**\[未验证\]**。
4. 任何"注入成功"的结论必须先有真机 `__goatProbe()`（`widget.js:1004`）回读 `mounted` 为真。
5. **默认验证路径是免重启的同源夹具** `app/web/widget-preview.html`：复刻宿主 composer 的 DOM 与主题变量、
   mock `electronAPI`，加载**同一份** `app/web/widget.js`。绝不为验证而重启用户正在使用的 Command Code；
   需要真机时把重启那一步交给用户点的脚本。

## 10. 安装与卸载的边界

- `install.ps1`：探测解释器 → 建项目内 `.venv` → 装 `websocket-client`（官方 → 清华 → 阿里逐个回退，
  **绝不关闭 TLS 校验**）→ 写 HKCU Run 键 → 注册读数条 → 打印状态。**幂等**，重复执行只补齐。
- `uninstall.ps1`：删 Run 键 → 停守护 → `__goatDispose()` → 删 `.venv`。`%LOCALAPPDATA%\GOATGauge\`
  默认保留，`-Purge` 才删。不碰宿主安装目录、不碰宿主 CSP、不碰用户快捷方式。
- 两者都**不**创建/改写 Command Code 的快捷方式或开始菜单项；带端口启动是一条独立命令，是否使用由用户决定。

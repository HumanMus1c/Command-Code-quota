# GOAT Gauge

GOAT Gauge is a local-first usage dashboard for Command Code GOAT.

## Screenshots

| Dashboard (light) | Dashboard (dark) |
|:---:|:---:|
| ![Dashboard light](docs/screenshots/dashboard-light.png) | ![Dashboard dark](docs/screenshots/dashboard-dark.png) |

| Model usage by cost | Usage records |
|:---:|:---:|
| ![Model usage](docs/screenshots/models-cost.png) | ![Usage records](docs/screenshots/records.png) |

---

It reads the same account and billing endpoints used by the Command Code CLI:

- `GET /alpha/whoami`
- `GET /alpha/billing/credits`
- `GET /alpha/billing/subscriptions`
- `GET /alpha/usage/summary`

The dashboard shows the rolling 5-hour, weekly, and monthly quota, reset
countdowns, credit balances, billing-period statistics, and a locally recorded
usage trend. It runs on `127.0.0.1`, stores history in SQLite, and stores a
saved API key with Windows DPAPI encryption.

It mirrors the GoGauge layout: quota rings, a time-range switch
(today / 7 days / 30 days / all), overview KPIs, a model-usage donut, a daily
trend chart, a paged usage-record table with per-request cost, plus cache hit
rate, cache read/write volume, and per-model hit rate.

## Run

Double-click `start_chrome.bat`, or run:

```powershell
python entry.py --chrome
```

The first run opens a dedicated Chrome profile at the Command Code sign-in
page. Sign in once in that window. GOAT Gauge then captures the authenticated
browser session through Chrome DevTools, saves it encrypted on disk, and starts
refreshing the dashboard automatically. The dashboard opens in the same Chrome
profile.

If you prefer not to use browser sign-in, the onboarding screen also accepts a
Command Code API key.

To run only the local server:

```powershell
python -m pip install -r requirements.txt
python entry.py --serve --port 18927
```

Then open `http://127.0.0.1:18927`.

Use `--demo` to preview the UI without an API key:

```powershell
python entry.py --chrome --demo
```

## Desktop shortcut

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\make-shortcut.ps1
# 可选: 同时注册「登录后静默启动本地服务」
powershell -ExecutionPolicy Bypass -File .\tools\make-shortcut.ps1 -Startup
```

This creates a **GOAT Gauge (Chrome)** shortcut on the desktop together with
`assets/goat-gauge.ico`.

The shortcut targets a hidden `powershell.exe` running `launch.ps1` rather than
`cmd.exe`, which is what makes it behave well on the taskbar: no black console
flash on launch, and the shortcut keeps its own icon when pinned.

`launch.ps1` starts or reuses the local server, waits for it to become ready,
and then opens the installed PWA through its Chrome app identity. If the PWA is
not installed yet, it falls back to a regular Chrome app window. This ordering
prevents the taskbar shortcut from opening the dashboard before the local
server is available.

In `-ServerOnly` mode, the same launcher starts only the local server and never
opens a browser window. This is the mode used by the optional login startup
entry.

The normal launcher is also self-healing. It opens the PWA first so the
dedicated Chrome profile exposes its authenticated session through DevTools,
then waits briefly for the dashboard session to recover. If the running server
is still stuck unauthenticated, the launcher exits that server cleanly, starts
a fresh one, and retries once.

To pin it: right-click the desktop shortcut, then **Pin to taskbar**
(on Windows 11 choose *Show more options* first).

### Why the Chrome window pins as "Google Chrome"

Chrome's `--app=` windows inherit Chrome's own taskbar identity, so pinning a
running window produces a **Google Chrome** entry. GOAT Gauge therefore ships a
web app manifest and service worker, which lets Chrome install it as a real
standalone app with its own name and icon.

In the dashboard, open **设置 → 安装为桌面应用**. Once installed, GOAT Gauge
appears in the Start menu and can be pinned to the taskbar with the purple icon
and its own window identity.

Because the installed app talks to `http://127.0.0.1:18927`, register the server
to start at login (`-Startup` above) if you want the pinned app to work right
after a reboot. The pinned app itself needs the local server running.

## Credential lookup

GOAT Gauge uses either a signed-in Chrome session or an API key. For a browser
session, the key is never handled manually. For API-key mode, it checks:

1. `COMMAND_CODE_API_KEY`
2. `COMMANDCODE_API_KEY`
3. The encrypted key saved by GOAT Gauge
4. Known values in `%USERPROFILE%\.commandcode\auth.json` or
   `%USERPROFILE%\.commandcode\config.json`
5. The Command Code provider stored by
   [CC Switch](https://github.com/farion1231/cc-switch) in
   `%USERPROFILE%\.cc-switch\cc-switch.db`

Step 5 only reads providers that actually point at Command Code, so keys that
belong to other vendors in the same database are never used.

If no key or browser session is available, the interface asks for an API key.
It is validated before being saved. The key is never returned to the browser.

### Staying signed in

Auto-login used to depend on Chrome handing over a live cookie on every launch,
so an expired or renamed cookie silently logged the dashboard out. GOAT Gauge
now:

- reuses a Command Code API key from CC Switch when one exists, which never
  expires the way a browser session does;
- stores the captured browser session in `browser-session.bin`, encrypted with
  Windows DPAPI, and restores it at startup without needing Chrome;
- captures every `commandcode.ai` cookie instead of a fixed list of cookie
  names, so a renamed sign-in cookie no longer breaks detection;
- keeps session cookies across Chrome restarts by enabling `Continue where you
  left off` in the dedicated profile;
- clears a rejected session and reopens the Command Code sign-in page when the
  upstream API answers `401`/`403`.

Run the settings drawer's **自动检测** button to re-scan for a key.

### API key plus browser session

Command Code only exposes per-request usage records to a signed-in browser, so
the two credentials cover different things:

- The **API key** drives quota, credits, plan, and summary numbers. It does not
  expire the way a browser session does, so the dashboard always has live data.
- The **browser session** powers per-request records, per-model breakdown,
  cache hit rate, and the wider local ranges.

When a key is available it stays the primary credential and the captured
browser session is attached alongside it for the detail endpoints. If the
browser session is missing, the dashboard still shows live quota and adds a
note with a **登录 Chrome** button.

## Data

Runtime data defaults to `%LOCALAPPDATA%\GOATGauge`.

Set `GOATGAUGE_DATA` to use a portable data directory.

### Currency

The settings drawer can show costs in `USD` (default) or `CNY`. Costs are
always fetched and stored in USD, so switching currency only changes how they
are displayed. In CNY mode an editable rate field controls the conversion
(default `1 USD = 7.2 CNY`), which keeps the app working offline instead of
depending on a live exchange-rate API.

Currency conversion applies to real cost fields only (`cost_total`,
`cost_input`, `cost_output`, `cost_cache`, and average cost). Quota and credit
figures such as `monthly_remaining` or window usage are Command Code credits,
not dollars, and are never converted.

### Proxy support

Upstream requests follow the `*_proxy` environment variables and the Windows
system proxy. The opener is rebuilt on every request, so a proxy client such as
Clash or mihomo may start, stop, or change ports while GOAT Gauge keeps
running; the dashboard picks the change up without a restart. When a proxy is
configured but unreachable, the request is retried once over a direct
connection, and loopback traffic to the DevTools port always bypasses the
proxy.

This matters on machines where DNS resolves upstream hosts to a proxy-only
fake IP range such as `198.18.0.0/15`: direct connections there time out, and a
dashboard that cached the "no proxy" decision at start-up would freeze until it
was restarted.

### Upstream limits

Command Code's usage endpoint only exposes the newest bounded window
(currently 1 day / 100 entries) and ignores range query parameters. GOAT Gauge
therefore keeps every record it has seen in local SQLite, and the
7-day / 30-day / all views are assembled from that local history. Those wider
ranges fill in as the app runs.

The same endpoint returns no session identifier, so per-session cost is not
available and is intentionally omitted rather than guessed.

Per-request records and cache buckets come from the `/internal/*` endpoints,
which only answer for a signed-in browser session. When that session is missing
the overview falls back to the billing-period aggregate that the API key can
still read (`/alpha/usage/summary`) and labels those cards "本计费周期上游汇总"
so period totals are never mistaken for the selected range.

### Cache metrics

Cache tokens come from `/internal/usage/charts`, which accepts an explicit
`from`/`to` window (roughly one day) and reports `cacheReadInputTokens` /
`cacheCreationInputTokens` per model and time bucket. GOAT Gauge stores those
buckets locally and computes:

- hit rate = cache read tokens / input tokens
- miss tokens = input tokens - cache read tokens

Aggregate KPIs prefer these upstream buckets because they cover the full
upstream window; the record table is a narrower sample (the newest ~100
entries), and the dashboard labels the difference.

GOAT Gauge uses Chrome DevTools port `9333` by default, so it can run
alongside other local tools that use port `9222`. Override it with
`GOATGAUGE_CHROME_CDP_PORT` if needed.

### 注入到 Command Code 的读数条

除了本地面板，GOAT Gauge 还能把一行紧凑读数注入 Command Code 桌面应用，贴在对话
输入框上方：5 小时 / 本周 / 本月的余量（`已用/上限` + 简单进度条 + 重置或账期倒计时）、
生成速度（tok/s）、credits 燃烧率（cr/h）、请求吞吐、缓存命中率，以及本期请求数、
TOKEN、费用与均价。末端的
状态点只显示颜色，数据年龄改为鼠标悬停时经 `title` 呈现，避免文本增减推动布局。

宽度只影响**呈现**，永远不改变**显示哪些指标**——否则读数条和布局编辑器会给出两份不一样
的信息（曾经就是如此：窄处被裁掉的指标，进编辑模式又冒出来）。阶梯是：单行 → 换行（限高三
行）→ 依次牺牲额度表内的倒计时与进度条、只留数字 → 连高预算也放弃（`tall`）。`blocked`
（整体 `visibility:hidden` 并置 `inert`）只由几何冲突触发：与输入框重叠、或窄到连一个胶囊都
放不下。测量前必须先回到不换行状态再读 `scrollWidth`，否则已经换行的行会被读成「不溢出」而
错误退回单行。

### 读数条的布局编辑

行尾的齿轮按钮进入编辑模式，此时读数条永不裁剪（`data-layout="editing"`），每个胶囊
变成虚线框并在右下角出现一个 `×` 小按钮，行尾换成三个只在编辑模式出现的按钮：

- **添加**：下拉列出所有已隐藏的指标，点一下即放回板上；
- **取消**：丢弃本次草稿（进入编辑时快照的 `order`/`hidden`），不写存储；
- **保存**：写入 `localStorage` 的 `goat-gauge-layout-v2` 并退出，按新布局重排。

鼠标左键按住胶囊左右拖动即可排序；落点由"指针在哪个胶囊中线左侧"决定，提交时只重写
**当前有数据、真的在板上渲染出来的**那些指标所占的槽位，暂时没数据的指标保持原位——
否则一次偶发的空窗就会把用户的布局永久打乱。所有交互都在 Shadow DOM（影子 DOM）内
做事件委托并 `stopPropagation`，不会把点击漏给宿主输入框。

配色一律从宿主根元素的 CSS 变量解析（`--primary`、`--card`、`--background`、
`--foreground`、`--ink-subtle`、`--border`、`--success`、`--warning`、`--destructive`），
解析不到才按当前明暗态退回内置默认值；宿主切换 `data-theme` 后由 1 秒心跳感知并重新解析，
不需要重新注入。只有两套主题：简洁浅色与简洁深色。

预览页的主题按以下优先级决定，页面上直接标出当前来源：`?theme=light|dark` > 手动点过
的按钮（存 `localStorage`）> **Command Code 自己的主题** > 简洁浅色。宿主主题不是
从它的 leveldb 里抠出来的（那是 snappy 分片，改版即碎），而是注入器通过 DevTools 协议向
活动渲染进程问一次 `documentElement` 的明暗态，回写进 `/api/live` 的 `host.theme`；这一项
只在宿主带调试端口在跑时存在，取不到就回到简洁浅色——**不跟随操作系统配色**，因为演示页
要对标的是 Command Code，而 Command Code 的主题是它自己的设置项，与系统明暗无关。
每一档都按当前状态重新解析，所以「宿主停止上报主题」这类来源消失会把页面带回下一档，
而不是钉死在上一次的值上。手动选过之后宿主再切主题不会覆盖它，点「跟随宿主」可重新交还。

读数条文案跟随宿主语言：宿主在启动与切换语言时改写 `<html lang>`，`zh-*` 用中文，
其余任何语言（含 `en`、`fr`）一律回落英文；语言切换由 1 秒心跳感知并就地重绘，不需要
重新注入。单位符号（`cr/h`、`tok/s`、`h`/`m`/`d`）不进语言表，两种语言下保持一致。
没有图表，没有多色调，数字一律等宽对齐。

Command Code 是 Electron 应用，页面 CSP 为 `script-src 'self' …`，注入的 `<script>`
会被拒绝，因此读数条通过 DevTools 协议求值进页面，数据也不再回拉 HTTP：

- 额度与用量走宿主自己的 IPC 桥 `window.electronAPI.accountOverview()`，即应用正在
  显示的那份数据，不额外产生上游请求；
- 生成速度、吞吐、燃烧率与命中率由本机服务算好后经 `window.__goatUpdate(payload)`
  推入；生成速度按请求实际流式时长（`Σ duration_ms`）归一，而不是按首末记录
  的墙钟跨度——后者会把空闲间隙算进分母，把速度低估一个量级；
- 读数条挂在 `.cc-composer` 的同一 flex 列里、位于其前一个兄弟位置，是真实布局节点
  而不是浮层；空间不足时先丢弃右侧可选指标，再不行就整体 `visibility:hidden` 并置
  `inert`，绝不覆盖宿主按钮；
- 宿主重渲染后由 MutationObserver 与 1 秒心跳重新锚定，且保证全页只有一份；
- 页面隐藏时轮询自动降频，回到前台立即补一帧。

想先确认读数条的位置、配色与降级行为而不重启宿主，可以打开内置预览页：它复刻
Command Code 输入框的 DOM 结构与主题变量，跑的就是同一份 `app/web/widget.js`。

```powershell
python entry.py --serve --port 18927 --demo
# 然后打开 http://127.0.0.1:18927/widget-preview.html
# 加 ?late=1 可复现真实注入顺序：脚本先于宿主 composer 执行，读数条靠心跳重试挂载
```

预览页里的读数来自模拟的宿主接口与真实计算出的 `/api/live` 派生指标；它只用于看效果，
不是注入通道。

启用与卸载：

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\start-widget.ps1
# 宿主已在调试端口上时只注入: 加 -AttachOnly；用演示数据验证: 加 -Demo
powershell -ExecutionPolicy Bypass -File .\tools\start-widget.ps1 -Detach
```

Command Code 启用了单实例锁，且默认不监听任何端口，所以要带
`--remote-debugging-port` 重启它才能注入；脚本会先征求确认再关闭宿主。调试端口只监听
`127.0.0.1`，但本机任意进程都能借此脚本化该窗口，不用时请用 `-Detach` 或正常重启。
读数条锚定在 `.cc-composer` 这类语义化类名上——宿主带中英文两套语言包，
`aria-label` 与占位文案会随语言变化，不能作为锚点。

## Tests

```powershell
python -m unittest discover -s tests -v
```

This is an unofficial community project and is not affiliated with Command
Code or its operators.

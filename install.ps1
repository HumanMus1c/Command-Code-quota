<#
GOAT Gauge 安装器（一键）

做四件事，全部幂等，重复执行只补齐：
  1. 找一个可用的 Python 3.10+（参数 -> PATH -> py -3 启动器 -> 常见安装目录）
  2. 在项目内建 .venv 并装依赖（官方源失败后依次回退清华、阿里；绝不关闭 TLS 校验）
  3. 注册登录后静默启动的守护进程（HKCU Run 键，指向本仓库的 entry.py --watch）
  4. 立即拉起守护并打印诊断

用法（普通 PowerShell，无需管理员）:
  powershell -ExecutionPolicy Bypass -File .\install.ps1
  powershell -ExecutionPolicy Bypass -File .\install.ps1 -CheckOnly      # 只体检，不写任何东西
  powershell -ExecutionPolicy Bypass -File .\install.ps1 -NoAutostart    # 不注册开机自启
  powershell -ExecutionPolicy Bypass -File .\install.ps1 -IndexUrl <url> # 指定镜像源
  powershell -ExecutionPolicy Bypass -File .\install.ps1 -WithShortcut   # 给你自己的快捷方式加调试端口参数（可选）
  powershell -ExecutionPolicy Bypass -File .\install.ps1 -AutoLaunch     # 开机时若宿主完全没运行，带端口拉起一次（可选）

边界：本脚本**永不**碰 Command Code 的安装目录，也不碰机器级的 ProgramData 快捷方式（docs/DESIGN.md N-2/§10）。
-WithShortcut 是唯一例外，且默认关闭：它只改**你自己账户下** %APPDATA% 里那个 .lnk 的 Arguments 字段，
写前把原值与 .lnk 字节副本存进 %LOCALAPPDATA%\GOATGauge\，uninstall.ps1 逐字还原。
#>
[CmdletBinding()]
param(
    [string]$PythonPath,
    [string]$IndexUrl,
    [switch]$NoAutostart,
    [switch]$CheckOnly,
    [switch]$NoStart,
    [switch]$WithShortcut,
    [switch]$AutoLaunch
)

$ErrorActionPreference = 'Stop'

$Repo     = $PSScriptRoot
$Venv     = Join-Path $Repo '.venv'
$VenvPy   = Join-Path $Venv 'Scripts\python.exe'
$VenvPyw  = Join-Path $Venv 'Scripts\pythonw.exe'
$EntryPy  = Join-Path $Repo 'entry.py'
$RunKey   = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$RunValue = 'GOATGaugeWatchdog'

# 官方源在前，国内镜像在后；trusted-host / --no-index 这类降级一律不做。
$Mirrors = @(
    'https://pypi.org/simple',
    'https://pypi.tuna.tsinghua.edu.cn/simple',
    'https://mirrors.aliyun.com/pypi/simple/'
)

function Write-Step([string]$Message) { Write-Host "==> $Message" -ForegroundColor Cyan }
function Write-Done([string]$Message) { Write-Host "    $Message" -ForegroundColor DarkGray }
function Write-Ok([string]$Message)   { Write-Host "    [OK] $Message" -ForegroundColor Green }
function Write-Bad([string]$Message)  { Write-Host "    [×] $Message" -ForegroundColor Yellow }

function Resolve-BasePython {
    $ErrorActionPreference = 'Continue'
    $candidates = @()
    if ($PythonPath) { $candidates += $PythonPath }

    $onPath = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
    if ($onPath) { $candidates += $onPath }

    $launcher = (Get-Command py.exe -ErrorAction SilentlyContinue).Source
    if ($launcher) {
        try {
            $fromLauncher = (& $launcher -3 -c 'import sys; print(sys.executable)' 2>$null)
            if ($fromLauncher) { $candidates += ("$fromLauncher").Trim() }
        } catch { }
    }

    $candidates += @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe'),
        'C:\Python\Python313\python.exe',
        'C:\Python\Python312\python.exe',
        'C:\Python313\python.exe',
        'C:\Python312\python.exe'
    )

    foreach ($candidate in $candidates) {
        if (-not $candidate) { continue }
        $candidate = ("$candidate").Trim().Trim('"')
        # 存在不等于可用：3.8 的 python.exe 也在 PATH 上，会一路走到 venv 才炸。
        if ((Test-Path -LiteralPath $candidate) -and (Test-PythonUsable $candidate)) { return $candidate }
    }
    return $null
}

function Get-PythonVersionCode([string]$Executable) {
    $ErrorActionPreference = 'Continue'
    # PowerShell 5.1 mangles embedded double quotes when it builds a native
    # command line, so the probe is written with no quote characters at all:
    # 3.13 -> 313, 3.10 -> 310.
    try {
        $reported = (& $Executable -c 'import sys;print(sys.version_info[0]*100+sys.version_info[1])' 2>$null)
    } catch {
        return 0
    }
    $text = ("$reported").Trim()
    $code = 0
    if ([int]::TryParse($text, [ref]$code)) { return $code }
    return 0
}

function Test-PythonUsable([string]$Executable) {
    $code = Get-PythonVersionCode $Executable
    return ($code -ge 310 -and $code -lt 400)
}

function Install-Dependencies {
    $ErrorActionPreference = 'Continue'
    $sources = @()
    if ($IndexUrl) { $sources += $IndexUrl }
    $sources += $Mirrors

    foreach ($source in $sources) {
        Write-Step "安装依赖（源: $source）"
        & $VenvPy -m pip install --disable-pip-version-check --no-input `
            --index-url $source -r (Join-Path $Repo 'requirements.txt') 2>&1 |
            ForEach-Object { Write-Done "$_" }
        if ($LASTEXITCODE -eq 0) {
            & $VenvPy -c 'import websocket' 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) { return $true }
        }
        Write-Bad "该源安装失败，尝试下一个。"
    }
    return $false
}

function Test-Dependencies {
    $ErrorActionPreference = 'Continue'
    & $VenvPy -c 'import websocket' 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
}

function Get-AutostartTarget {
    # 一个进程同时干三件事：本地服务、注入读数条、（可选）开机时带端口拉起宿主。
    # 注册成 --watch 会让服务缺席：月份分母退成估算值、gen/rate 这类只由服务供给的
    # 指标永远不出现 —— 这是 2026-10-12 真机上实际踩到的残缺形态。
    $line = ('"{0}" "{1}" --serve --inject' -f $VenvPyw, $EntryPy)
    if ($AutoLaunch) { $line += ' --auto-launch' }
    return $line
}

function Get-ServiceArguments {
    $arguments = @("`"$EntryPy`"", '--serve', '--inject')
    if ($AutoLaunch) { $arguments += '--auto-launch' }
    return $arguments
}

function Set-LaunchShortcut {
    $ErrorActionPreference = 'Continue'
    $report = & $VenvPy $EntryPy --install-shortcut 2>&1 | Out-String
    Write-Done ($report.Trim())
    if ($LASTEXITCODE -eq 0) {
        Write-Ok "所有启动快捷方式都已带上调试端口参数（原值已备份，uninstall 可还原）。"
        return
    }
    if ($report -notmatch 'denied-or-error') { Write-Bad "快捷方式未改动。"; return }
    # 机器级（ProgramData）那个 .lnk 普通用户写不动；开始菜单搜索常常解析到它，
    # 只改用户级等于没改。请求一次提权（本机 UAC 已关弹窗，静默通过）。
    Write-Bad "机器级快捷方式需要管理员权限，正在请求一次提权。"
    Start-Process -FilePath $VenvPy -ArgumentList @("`"$EntryPy`"", '--install-shortcut') -Verb RunAs -WindowStyle Hidden -Wait
    Write-Done ((& $VenvPy $EntryPy --install-shortcut 2>&1 | Out-String).Trim())
}

function Register-Autostart {
    $target = Get-AutostartTarget
    $current = (Get-ItemProperty -Path $RunKey -Name $RunValue -ErrorAction SilentlyContinue).$RunValue
    if ($current -eq $target) {
        Write-Ok "开机自启已注册（未改动）。"
        return
    }
    if ($current) { Write-Bad "已有旧值，将覆盖: $current" }
    New-ItemProperty -Path $RunKey -Name $RunValue -PropertyType String -Value $target -Force | Out-Null
    Write-Ok "开机自启已注册: $RunValue"
}

function Start-Watchdog {
    $ErrorActionPreference = 'Continue'
    $diagnosis = & $VenvPy $EntryPy --status 2>$null | Out-String
    if ($diagnosis -match '"inject_pid":\s*([1-9]\d*)') {
        Write-Ok "本地服务与注入已在运行（PID $($Matches[1])）。"
        return
    }
    Start-Process -FilePath $VenvPyw -ArgumentList (Get-ServiceArguments) -WorkingDirectory $Repo -WindowStyle Hidden
    Write-Ok "本地服务与注入已静默启动（无窗口）。"
}

Write-Step "定位 Python 3.10+"
$base = Resolve-BasePython
if (-not $base) {
    Write-Host "找不到可用的 Python 3.10+。请先到 python.org 或 Microsoft Store 安装，再重跑本脚本；已安装但不在 PATH 上的可用 --PythonPath 直接指定。" -ForegroundColor Red
    exit 1
}
$detected = Get-PythonVersionCode $base
Write-Ok "使用 $base（Python $([math]::Floor($detected/100)).$($detected%100)）"

if ($CheckOnly) {
    Write-Step "体检模式：不创建、不安装、不写注册表"
    Write-Done (".venv 是否存在: " + (Test-Path -LiteralPath $VenvPy))
    if (Test-Path -LiteralPath $VenvPy) { Write-Done ("依赖是否就绪: " + (Test-Dependencies)) }
    Write-Done ("开机自启当前值: " + (Get-ItemProperty -Path $RunKey -Name $RunValue -ErrorAction SilentlyContinue).$RunValue)
    & $base (Join-Path $Repo 'entry.py') --status 2>$null | Out-String | Write-Done
    exit 0
}

if (-not (Test-Path -LiteralPath $VenvPy)) {
    Write-Step "创建虚拟环境 .venv"
    & $base -m venv $Venv
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $VenvPy)) {
        Write-Host "创建 .venv 失败。请检查杀毒软件是否拦截了 python -m venv。" -ForegroundColor Red
        exit 1
    }
    Write-Ok "已创建 $Venv"
} else {
    Write-Ok ".venv 已存在，跳过创建。"
}

if (Test-Dependencies) {
    Write-Ok "依赖已就绪（websocket-client）。"
} elseif (-not (Install-Dependencies)) {
    Write-Host "依赖安装失败。可手动重试: & `"$VenvPy`" -m pip install -r requirements.txt --index-url <可用镜像>" -ForegroundColor Red
    exit 1
} else {
    Write-Ok "依赖已安装。"
}

if (-not $NoAutostart) { Register-Autostart } else { Write-Done "按 -NoAutostart 跳过开机自启。" }

if ($WithShortcut) {
    Write-Step "给你的启动快捷方式加上调试端口（可选）"
    Set-LaunchShortcut
}

if (-not $NoStart) { Start-Watchdog } else { Write-Done "按 -NoStart 跳过立即启动。" }

Write-Step "诊断"
$ErrorActionPreference = 'Continue'
& $VenvPy $EntryPy --status 2>&1 | Out-String | Write-Host

Write-Host ""
Write-Host "读数条只在宿主带环回调试端口运行时才能挂载。启动宿主请用:" -ForegroundColor Yellow
Write-Host "    .\gauge-launch.cmd"
Write-Host "想让双击原来的图标就自带端口（上游文档给用户的同款做法，只改你自己 %APPDATA% 下的 .lnk）:"
Write-Host "    powershell -ExecutionPolicy Bypass -File .\install.ps1 -WithShortcut" -ForegroundColor Gray
Write-Host "已在运行的 Command Code 不会被本脚本重启（守护进程也不会替你关闭正在使用的会话）。"
Write-Host "带端口启动后，回读宿主接口结构: .\gauge-launch.cmd 之后运行"
Write-Host "    & `"$VenvPy`" `"$EntryPy`" --probe" -ForegroundColor Gray

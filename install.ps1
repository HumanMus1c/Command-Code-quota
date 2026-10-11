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

本脚本不碰 Command Code 的安装目录，也不碰它的快捷方式与开始菜单项（见 docs/DESIGN.md N-2/§10）。
#>
[CmdletBinding()]
param(
    [string]$PythonPath,
    [string]$IndexUrl,
    [switch]$NoAutostart,
    [switch]$CheckOnly,
    [switch]$NoStart
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
    return ('"{0}" "{1}" --watch' -f $VenvPyw, $EntryPy)
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
    if ($diagnosis -match '"watchdog_pid":\s*([1-9]\d*)') {
        Write-Ok "守护进程已在运行（PID $($Matches[1])）。"
        return
    }
    Start-Process -FilePath $VenvPyw -ArgumentList @("`"$EntryPy`"", '--watch') -WorkingDirectory $Repo -WindowStyle Hidden
    Write-Ok "守护进程已静默启动（无窗口）。"
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

if (-not $NoStart) { Start-Watchdog } else { Write-Done "按 -NoStart 跳过立即启动。" }

Write-Step "诊断"
$ErrorActionPreference = 'Continue'
& $VenvPy $EntryPy --status 2>&1 | Out-String | Write-Host

Write-Host ""
Write-Host "读数条只在宿主带环回调试端口运行时才能挂载。启动宿主请用:" -ForegroundColor Yellow
Write-Host "    .\gauge-launch.cmd"
Write-Host "已在运行的 Command Code 不会被本脚本重启（守护进程也不会替你关闭正在使用的会话）。"
Write-Host "带端口启动后，回读宿主接口结构: .\gauge-launch.cmd 之后运行"
Write-Host "    & `"$VenvPy`" `"$EntryPy`" --probe" -ForegroundColor Gray

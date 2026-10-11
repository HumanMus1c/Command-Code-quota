<#
GOAT Gauge 读数条启动器 (注入 Command Code 桌面应用)

读数条是注入到 Command Code 渲染进程里的一行紧凑读数, 贴在对话输入框上方。
Command Code 是 Electron 应用且启用了单实例锁, 页面 CSP 又禁止注入 <script>,
所以必须:
  1. 让 Command Code 带 --remote-debugging-port 重新启动 (会在 %APPDATA%\Command Code
     下生成 DevToolsActivePort)
  2. 由本仓库的 python 服务通过 DevTools 协议把读数条源码求值进页面并持续推送派生指标

本脚本做: 关闭正在运行的 Command Code -> 带调试端口重启 -> 等 DevToolsActivePort ->
拉起本地服务并开始注入。

用法:
  powershell -ExecutionPolicy Bypass -File .\tools\start-widget.ps1            # 会先征求确认再重启宿主
  powershell -ExecutionPolicy Bypass -File .\tools\start-widget.ps1 -Force     # 不询问, 直接重启宿主
  powershell -ExecutionPolicy Bypass -File .\tools\start-widget.ps1 -Demo      # 用演示数据验证读数条
  powershell -ExecutionPolicy Bypass -File .\tools\start-widget.ps1 -AttachOnly # 宿主已在调试端口上, 只注入
  powershell -ExecutionPolicy Bypass -File .\tools\start-widget.ps1 -Detach    # 移除读数条, 不起服务

安全说明: --remote-debugging-port 只监听 127.0.0.1, 但本机任意进程都能借此脚本化该窗口。
不需要时请用 -Detach 或直接正常重启 Command Code。
#>
[CmdletBinding()]
param(
    [string]$ExePath = 'D:\Program Files\Command Code\Command Code.exe',
    [int]$DebugPort = 9334,
    [int]$ServerPort = 18927,
    [switch]$Force,
    [switch]$Demo,
    [switch]$AttachOnly,
    [switch]$Detach,
    [switch]$NoServer
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Profile = Join-Path $env:APPDATA 'Command Code'
$PortFile = Join-Path $Profile 'DevToolsActivePort'

function Get-HostProcess {
    Get-Process -Name 'Command Code' -ErrorAction SilentlyContinue
}

if ($Detach) {
    Push-Location $RepoRoot
    try { python entry.py --detach } finally { Pop-Location }
    return
}

# ---- 1. 让宿主带上调试端口 ----
if (-not $AttachOnly) {
    if (-not (Test-Path $ExePath)) {
        throw "找不到 Command Code: $ExePath  (用 -ExePath 指定)"
    }
    $running = @(Get-HostProcess)
    if ($running.Count -gt 0) {
        Write-Host "Command Code 正在运行 ($($running.Count) 个进程)。单实例锁决定了必须先退出它才能开调试端口。" -ForegroundColor Yellow
        if (-not $Force) {
            $answer = Read-Host '优雅关闭并带调试端口重启? [y/N]'
            if ($answer -notmatch '^[yY]') {
                Write-Host '已取消。读数条未注入, 宿主未受影响。' -ForegroundColor Green
                return
            }
        }
        # 先走正常关闭窗口流程, 给应用落盘会话的机会
        foreach ($proc in $running) {
            try { [void]$proc.MainWindowHandle; if ($proc.MainWindowHandle -ne 0) { [void]$proc.CloseMainWindow() } } catch { }
        }
        $deadline = (Get-Date).AddSeconds(20)
        while (@(Get-HostProcess).Count -gt 0 -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 400 }
        $leftover = @(Get-HostProcess)
        if ($leftover.Count -gt 0) {
            Write-Host "仍有 $($leftover.Count) 个进程未退出, 强制结束。" -ForegroundColor Yellow
            $leftover | Stop-Process -Force
            Start-Sleep -Milliseconds 600
        }
    }

    if (Test-Path $PortFile) {
        try { Remove-Item $PortFile -Force -ErrorAction Stop } catch { Write-Host "旧 DevToolsActivePort 未删除: $_" -ForegroundColor DarkGray }
    }

    Write-Host "启动 Command Code (调试端口 $DebugPort) ..."
    Start-Process -FilePath $ExePath -ArgumentList @("--remote-debugging-port=$DebugPort") | Out-Null
}

# ---- 2. 等 DevToolsActivePort 出现 ----
$deadline = (Get-Date).AddSeconds(30)
$observed = $null
while ((Get-Date) -lt $deadline) {
    if (Test-Path $PortFile) {
        try {
            $first = ((Get-Content $PortFile -TotalCount 1) -as [int])
            if ($first -gt 0) { $observed = $first; break }
        } catch { }
    }
    Start-Sleep -Milliseconds 300
}
if (-not $observed) {
    Write-Host '超时: 没有生成 DevToolsActivePort。' -ForegroundColor Red
    Write-Host '  可能原因: 该 Command Code 版本拒绝了调试端口开关, 或它已被另一个不带端口的实例占用。' -ForegroundColor Red
    Write-Host '  先用任务管理器确认 "Command Code" 进程已全部退出, 再重试; 或加 -DebugPort 换端口。' -ForegroundColor Red
    return
}
if ($observed -ne $DebugPort) {
    Write-Host "注意: 实际调试端口是 $observed (请求 $DebugPort)。注入按文件里的端口走。" -ForegroundColor Yellow
}
Write-Host "调试通道就绪: 127.0.0.1:$observed" -ForegroundColor Green

# ---- 3. 起服务 + 注入 ----
Push-Location $RepoRoot
try {
    $args = @('entry.py', '--serve', '--port', "$ServerPort", '--inject')
    if ($Demo) { $args += '--demo' }
    if ($NoServer) {
        Write-Host "本地服务请自行启动: python entry.py --serve --port $ServerPort --inject" -ForegroundColor DarkGray
    } else {
        Write-Host "本地服务: http://127.0.0.1:$ServerPort/  (Ctrl+C 退出即停止注入)" -ForegroundColor Green
        & python @args
    }
} finally {
    Pop-Location
}

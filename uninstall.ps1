<#
GOAT Gauge 卸载器（一键）

顺序: 从活页面移除读数条 -> 停守护进程 -> 清除旧版本留下的两处登录自启注册 -> 删 .venv。
默认**保留**运行时数据目录（历史库与加密凭据），要一起删才加 -Purge。
本脚本从不触碰 Command Code 的安装目录、快捷方式或开始菜单项；
它清的只有本工具自己留下的条目（HKCU Run 键值 + 「启动」文件夹里那个指向本仓库的 .lnk），
且都先核对指向——同名但属于别处的值/链接一律原样保留。

用法:
  powershell -ExecutionPolicy Bypass -File .\uninstall.ps1
  powershell -ExecutionPolicy Bypass -File .\uninstall.ps1 -KeepVenv   # 只停服务，留 .venv
  powershell -ExecutionPolicy Bypass -File .\uninstall.ps1 -Purge      # 连数据目录一起删
#>
[CmdletBinding()]
param(
    [switch]$KeepVenv,
    [switch]$Purge
)

$ErrorActionPreference = 'Stop'

$Repo     = $PSScriptRoot
$Venv     = Join-Path $Repo '.venv'
$VenvPy   = Join-Path $Venv 'Scripts\python.exe'
$EntryPy  = Join-Path $Repo 'entry.py'
$RunKey   = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$RunValue = 'GOATGaugeWatchdog'

function Write-Step([string]$Message) { Write-Host "==> $Message" -ForegroundColor Cyan }
function Write-Ok([string]$Message)   { Write-Host "    [OK] $Message" -ForegroundColor Green }
function Write-Done([string]$Message) { Write-Host "    $Message" -ForegroundColor DarkGray }
function Write-Skip([string]$Message) { Write-Host "    [跳过] $Message" -ForegroundColor Yellow }

function Get-RuntimeDir {
    $configured = "$($env:GOATGAUGE_DATA)".Trim()
    if ($configured) { return $configured }
    $root = if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { Join-Path $env:USERPROFILE 'AppData\Local' }
    return (Join-Path $root 'GOATGauge')
}

$hasVenv = Test-Path -LiteralPath $VenvPy

Write-Step "从正在运行的宿主页面移除读数条"
if ($hasVenv) {
    try {
        $detached = (& $VenvPy $EntryPy --detach 2>&1 | Out-String)
        Write-Done $detached.Trim()
    } catch {
        Write-Skip "移除失败（宿主多半没在调试端口上跑）: $_"
    }
} else {
    Write-Skip "没有 .venv，跳过；若宿主已不在运行，读数条会随宿主退出自然消失。"
}

Write-Step "停止守护进程"
$stopped = 0
try {
    $procs = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" -ErrorAction SilentlyContinue
    foreach ($proc in $procs) {
        $line = "$($proc.CommandLine)"
        if ($line -notmatch 'entry\.py.*(--watch|--serve)|app\.watchdog') { continue }
        $exePath = "$($proc.ExecutablePath)"
        # 只结束由本仓库 .venv 拉起的进程；命令行匹配但路径不符的一律不动。
        if (-not $exePath -or -not $exePath.StartsWith($Venv, [StringComparison]::OrdinalIgnoreCase)) {
            Write-Skip "PID $($proc.ProcessId) 命令行像守护进程但可执行文件不在 .venv 内，未结束它。"
            continue
        }
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
        $stopped += 1
    }
} catch {
    Write-Skip "查询进程失败: $_"
}
if ($stopped) { Write-Ok "已结束 $stopped 个守护进程。" } else { Write-Done "没有在运行的守护进程。" }

Write-Step "还原启动快捷方式（若安装时改过）"
if ($hasVenv) {
    $restored = (& $VenvPy $EntryPy --uninstall-shortcut 2>&1 | Out-String)
    if ($LASTEXITCODE -ne 0 -and $restored -match 'differs') {
        # 机器级那个要管理员权限才写得动，和安装时对称地请求一次。
        Write-Bad "机器级快捷方式需要管理员权限还原，正在请求一次提权。"
        Start-Process -FilePath $VenvPy -ArgumentList @("`"$EntryPy`"", '--uninstall-shortcut') -Verb RunAs -WindowStyle Hidden -Wait
        $restored = (& $VenvPy $EntryPy --uninstall-shortcut 2>&1 | Out-String)
    }
    if ($LASTEXITCODE -eq 0) {
        if ($restored -match 'nothing-to-restore') { Write-Done "没改过快捷方式，无需还原。" }
        else { Write-Ok "已把每个快捷方式的 Arguments 还原成安装前的原值。" }
    } else {
        Write-Skip "还原失败，请手动检查；原始 .lnk 副本留在数据目录里。$($restored.Trim())"
    }
    Write-Done ($restored.Trim())
} else {
    Write-Skip "没有 .venv，无法自动还原；备份仍在数据目录（launch-shortcut.json / launch-shortcut-*.lnk.bak）。"
}

Write-Step "清除旧版本注册的开机自启 (Run 键)"
$current = (Get-ItemProperty -Path $RunKey -Name $RunValue -ErrorAction SilentlyContinue).$RunValue
if (-not $current) {
    Write-Done "未注册过，无需处理。"
} elseif ($current -like "*$Venv*") {
    Remove-ItemProperty -Path $RunKey -Name $RunValue -ErrorAction SilentlyContinue
    Write-Ok "已删除 $RunValue。"
} else {
    Write-Skip "保留指向别处的同名值: $current"
}

# 旧版 tools/make-shortcut.ps1 -Startup 往「启动」文件夹放过一个 .lnk，安装器从不认识它，
# 只有这里能把它带走 —— 删功能必须同时给它留一条清除路径。
Write-Step "清除旧版本放在「启动」文件夹里的条目"
$startupLink = Join-Path ([Environment]::GetFolderPath('Startup')) 'GOAT Gauge Server.lnk'
if (-not (Test-Path -LiteralPath $startupLink)) {
    Write-Done "没有该条目，无需处理。"
} else {
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut($startupLink)
    $pointsHere = ($link.WorkingDirectory -eq $Repo) -or ($link.Arguments -like "*$Repo*")
    if ($pointsHere) {
        Remove-Item -LiteralPath $startupLink -Force
        Write-Ok "已删除 $startupLink。"
    } else {
        Write-Skip "保留指向别处的同名链接: $($link.Arguments)"
    }
}

Write-Step "删除虚拟环境"
if ($KeepVenv) {
    Write-Done "按 -KeepVenv 保留 $Venv。"
} elseif (Test-Path -LiteralPath $Venv) {
    Remove-Item -LiteralPath $Venv -Recurse -Force -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $Venv) { Write-Skip "删除不完整（可能有文件被占用），请关闭相关进程后重试。" }
    else { Write-Ok "已删除 .venv。" }
} else {
    Write-Done "没有 .venv。"
}

Write-Step "运行时数据目录"
$data = Get-RuntimeDir
if (Test-Path -LiteralPath $data) {
    if ($Purge) {
        # 只允许删本插件的目录名，或 GOATGAUGE_DATA 明确指定的那一个。
        $leaf = (Split-Path -Leaf $data)
        if ($leaf -ne 'GOATGauge' -and -not $env:GOATGAUGE_DATA) {
            Write-Skip "路径 $data 不像本插件的数据目录，拒绝删除。"
        } else {
            Remove-Item -LiteralPath $data -Recurse -Force -ErrorAction SilentlyContinue
            if (Test-Path -LiteralPath $data) { Write-Skip "$data 未能完全删除。" }
            else { Write-Ok "已删除 $data（含历史库与加密凭据）。" }
        }
    } else {
        Write-Done "保留 $data（SQLite 历史与 DPAPI 加密凭据）。要一起删请加 -Purge。"
    }
} else {
    Write-Done "没有数据目录，无需处理。"
}

Write-Host ""
Write-Host "卸载完成。Command Code 的安装目录与机器级快捷方式从未被改动；你自己账户下的启动快捷方式"
Write-Host "已按上面的还原步骤处理（没改过就是原样）。若它当前带调试端口在跑，正常退出再重新打开"
Write-Host "即可回到默认（无端口）状态。" -ForegroundColor Yellow

# 清除 DSH 渲染层本地缓存（界面里存的"上次打开的会话"就在这里）
# 用法：先完全关闭 DSH Desktop，再运行本脚本
$ErrorActionPreference = "Stop"

Write-Host "=== 清除 DSH 本地缓存 ===" -ForegroundColor Cyan

if (Get-Process "DSH Desktop" -ErrorAction SilentlyContinue) {
    Write-Host "[中止] DSH Desktop 还在运行。请先完全退出（含托盘图标），确认任务管理器里没有残留进程。" -ForegroundColor Red
    exit 1
}

$base = Join-Path $env:APPDATA "DSH Desktop\Partitions\dsh-desktop-renderer"
$ts   = Get-Date -Format "yyyyMMdd_HHmmss"

$ls = Join-Path $base "Local Storage"
if (Test-Path $ls) {
    $dst = "$ls.bak_$ts"
    if (Test-Path $dst) { Remove-Item $dst -Recurse -Force }
    Move-Item $ls $dst -Force
    Write-Host "[1/2] Local Storage 已移到：$dst（要回退就改回原名）"
} else {
    Write-Host "[1/2] 没找到 Local Storage，跳过"
}

$ss = Join-Path $base "Session Storage"
if (Test-Path $ss) {
    Remove-Item $ss -Recurse -Force
    Write-Host "[2/2] Session Storage 已清（非持久数据，直接删）"
} else {
    Write-Host "[2/2] 没找到 Session Storage，跳过"
}

Write-Host ""
Write-Host "完成。现在打开 DSH Desktop。" -ForegroundColor Green
Write-Host "  打开后若自动进入旧会话，点左侧「新会话」即可用新 id 开对话。"


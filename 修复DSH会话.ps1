# 修复 DSH 桌面的坏会话（先完全关闭 DSH Desktop 再运行）
$ErrorActionPreference = "Stop"
$id  = "session-ad80e08a-acf8-4e53-91b9-f14e6ed680b9"
$w   = Join-Path $env:USERPROFILE ".dsh"
$wsd = Join-Path $w "sessions\--D-~79D8~4E66~667A~80FD~4F53--"
$sess = Join-Path $wsd $id
$jf  = Join-Path $w "storages\workspace.json"

Write-Host "=== 修复 DSH 坏会话 ===" -ForegroundColor Cyan

if (Get-Process "DSH Desktop" -ErrorAction SilentlyContinue) {
    Write-Host "[中止] DSH Desktop 还在运行，请先完全退出（任务管理器确认没有残留进程）。" -ForegroundColor Red
    exit 1
}

if (Test-Path $sess) {
    $bak = Join-Path $wsd ("_broken_" + $id)
    if (Test-Path $bak) { Remove-Item $bak -Recurse -Force }
    Move-Item $sess $bak -Force
    Write-Host "[1/3] 已把坏会话移到 _broken_$id（可回退）"
} else {
    Write-Host "[1/3] 坏会话目录不存在，跳过"
}

Copy-Item $jf "$jf.bak" -Force
Write-Host "[2/3] 已备份 workspace.json -> workspace.json.bak"

$text = Get-Content $jf -Raw
$new  = $text -replace ('\s*"' + [regex]::Escape($id) + '",'), ""

try {
    $null = $new | ConvertFrom-Json
} catch {
    Write-Host "[中止] 改完 JSON 不合法，已保持原文件不动。" -ForegroundColor Red
    exit 1
}
Set-Content -Path $jf -Value $new -Encoding UTF8
Write-Host "[3/3] workspace.json 已更新并通过 JSON 校验"

$obj = $new | ConvertFrom-Json
$ws  = $obj.tables.workspaces."b3c43f54-988e-459f-b254-d63e24b84f1f"
Write-Host ""
Write-Host ("现在的会话列表：" + ($ws.sessionIds -join ", "))
Write-Host ""
Write-Host "完成。现在可以打开 DSH Desktop，在 D:\秘书智能体 里新建会话了。" -ForegroundColor Green
Write-Host "要回退：把 sessions\_broken_$id 改回原名，并用 workspace.json.bak 覆盖 workspace.json。"

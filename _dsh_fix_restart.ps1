# 由 Windows 任务计划在 DSH 之外执行：关 DSH → 清渲染层缓存 → 重新打开 DSH
$log = Join-Path $env:USERPROFILE ".dsh\dsh-fix.log"
function L($m) { ("{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $m) | Out-File $log -Append -Encoding UTF8 }

L "=== 开始 ==="
Start-Sleep -Seconds 25

try {
    $procs = Get-Process "DSH Desktop" -ErrorAction SilentlyContinue
    L ("关闭前进程数: " + ($procs | Measure-Object).Count)
    $procs | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 4
    L ("关闭后剩余: " + ((Get-Process "DSH Desktop" -ErrorAction SilentlyContinue) | Measure-Object).Count)
} catch { L ("关进程出错: " + $_.Exception.Message) }

$base = Join-Path $env:APPDATA "DSH Desktop\Partitions\dsh-desktop-renderer"
$ts   = Get-Date -Format "yyyyMMdd_HHmmss"
$ls = Join-Path $base "Local Storage"
if (Test-Path $ls) {
    $dst = "$ls.bak_cache_$ts"
    try { Move-Item $ls $dst -Force; L "Local Storage 已移到 $dst" } catch { L ("移 Local Storage 失败: " + $_.Exception.Message) }
} else { L "没有 Local Storage" }
$ss = Join-Path $base "Session Storage"
if (Test-Path $ss) {
    try { Remove-Item $ss -Recurse -Force; L "Session Storage 已清" } catch { L ("清 Session Storage 失败: " + $_.Exception.Message) }
} else { L "没有 Session Storage" }

Start-Sleep -Seconds 2
try {
    Start-Process "D:\新建文件夹\DSH Desktop\DSH Desktop.exe"
    L "已重新启动 DSH Desktop"
} catch { L ("重启失败: " + $_.Exception.Message) }
L "=== 结束 ==="

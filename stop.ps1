# stop.ps1 —— Kill Syncore backend (8000) and frontend (5173) by port
# 用法:
#   .\stop.ps1                 kill both 8000 and 5173
#   .\stop.ps1 -Port 8000      kill backend only
#   .\stop.ps1 -Port 5173      kill frontend only
#   .\stop.ps1 -Deps           连 Docker 依赖一起停（Milvus/etcd/Redis/ES/Neo4j）
#   .\stop.ps1 -Force          端口被非 python/node 的进程占用时不询问、直接杀
#
# ⚠️ 这是硬杀兜底：taskkill /F 会跳过后端 lifespan 的优雅关闭（停 scheduler、
#    drain 后台任务、关 MCP/ES 连接），在途子任务要等下次启动的对账来收尸。
#    优先直接**关闭** start.ps1 开的两个 PowerShell 窗口 —— 那会发
#    CTRL_CLOSE_EVENT，uvicorn 有几秒时间走完优雅关闭。
#
# ⚠️ 必须 taskkill /T /F 杀整棵树：start.ps1 用 --reload 起 uvicorn，监听
#    socket 归 reloader 父进程，真正跑应用的 worker 子进程持有继承的 socket
#    句柄 —— 只杀监听 PID 会留下继续占着 8000 端口服务旧代码的孤儿
#    （与 desktop/main.js killBackendTree 的注释是同一个坑）。

[CmdletBinding()]
param(
    [int[]]$Port = @(8000, 5173),
    [switch]$Deps,
    [switch]$Force
)

$ErrorActionPreference = "SilentlyContinue"
$Root = $PSScriptRoot

function Get-PidsByPort {
    param([int]$Port)
    $pids = @()
    # Primary: netstat (works without admin on most Windows)
    try {
        $lines = netstat -ano 2>$null | Select-String ":$Port\s+.*LISTENING"
        foreach ($line in $lines) {
            $parts = $line.ToString().Trim() -split '\s+'
            $procId = $parts[-1]
            if ($procId -match '^\d+$') { $pids += [int]$procId }
        }
    } catch {}
    # Fallback: Get-NetTCPConnection
    if (-not $pids) {
        try {
            $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
            foreach ($c in $conns) { if ($c.OwningProcess) { $pids += $c.OwningProcess } }
        } catch {}
    }
    return $pids | Select-Object -Unique
}

foreach ($p in $Port) {
    Write-Host ">>> Port $p" -ForegroundColor Cyan
    $pids = Get-PidsByPort -Port $p
    if (-not $pids) {
        Write-Host "    Port $p is free" -ForegroundColor Gray
        continue
    }
    foreach ($pid_ in $pids) {
        $proc = Get-Process -Id $pid_ -ErrorAction SilentlyContinue
        if (-not $proc) {
            Write-Host "    PID $pid_ no longer exists" -ForegroundColor Gray
            continue
        }
        # 身份校验：本项目的服务只可能是 python(uvicorn) / node(vite)。
        # 别的进程占着端口说明是用户的其它程序，无差别杀掉代价太高。
        if ($proc.ProcessName -notmatch '^(python|pythonw|node)$') {
            if ($Force) {
                Write-Host "    !!  PID $pid_ ($($proc.ProcessName)) is not python/node — killing anyway (-Force)" -ForegroundColor Yellow
            } else {
                $ans = Read-Host "    PID $pid_ ($($proc.ProcessName)) is not python/node. Kill it anyway? (y/N)"
                if ($ans -ne "y" -and $ans -ne "Y") {
                    Write-Host "    Skipped PID $pid_" -ForegroundColor Yellow
                    continue
                }
            }
        }
        taskkill /PID $pid_ /T /F | Out-Null
        Write-Host "    OK  Killed process tree of PID $pid_ ($($proc.ProcessName))" -ForegroundColor Green
    }
}

# 收尾：关掉 start.ps1 开的两个 -NoExit PowerShell 窗口（里面的服务已死，只剩空壳）。
# 按命令行匹配「本项目路径 + uvicorn / npm run dev」，只命中 start.ps1 用
# Start-Process 拉起的包装窗口，不会误杀用户自己开的终端。
$backendPath = [regex]::Escape((Join-Path $Root "backend"))
$frontendPath = [regex]::Escape((Join-Path $Root "frontend"))
$wrappers = Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" | Where-Object {
    $_.CommandLine -and
    (($_.CommandLine -match $backendPath -and $_.CommandLine -match 'uvicorn') -or
     ($_.CommandLine -match $frontendPath -and $_.CommandLine -match 'npm run dev'))
}
foreach ($w in $wrappers) {
    Stop-Process -Id $w.ProcessId -Force -ErrorAction SilentlyContinue
    Write-Host "    OK  Closed leftover window PID $($w.ProcessId)" -ForegroundColor Green
}

# ---------- Docker 依赖（-Deps 时才动） ----------
# 依赖端口 (19530/6379/9200/7687) 的监听者是 com.docker.backend —— Docker
# Desktop 的端口转发进程，不是容器本体。按端口杀进程停不掉容器（杀 8868 只会
# 把整个 Docker Desktop 打死），唯一正确方式是 docker stop。
# 容器按「发布端口」匹配，不硬编码容器名（docker-compose 起的别名也命中）；
# milvus-etcd 不发布任何端口（2379 仅容器内），只能按已知名字识别。
if ($Deps) {
    Write-Host ">>> Docker dependencies" -ForegroundColor Cyan
    $depPorts = @(19530, 6379, 9200, 7687)
    $knownNames = @("milvus-etcd")
    $toStop = [ordered]@{}
    foreach ($line in (docker ps --format "{{.ID}}|{{.Names}}|{{.Ports}}" 2>$null)) {
        $f = "$line" -split "\|"
        if ($f.Count -lt 3) { continue }
        $hit = ($knownNames -contains $f[1])
        if (-not $hit) {
            foreach ($dp in $depPorts) {
                if ($f[2] -match ":$dp->") { $hit = $true; break }
            }
        }
        if ($hit) { $toStop[$f[0]] = $f[1] }
    }
    if ($toStop.Count -eq 0) {
        Write-Host "    no matching containers running" -ForegroundColor Gray
    } else {
        foreach ($id in $toStop.Keys) {
            docker stop $id | Out-Null
            Write-Host "    OK  stopped container $($toStop[$id])" -ForegroundColor Green
        }
    }
}

Write-Host ""; Write-Host "Done." -ForegroundColor Green

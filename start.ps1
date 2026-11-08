# start.ps1 —— 一键启动 Syncore
# 用法:
#   .\start.ps1                      正常启动（后端 8000 + 前端 5173）
#
# 长驻进程各自开一个 PowerShell 窗口，关闭窗口即停止。
# 停服务用 .\stop.ps1（按端口杀进程）。
#
# ---- 关于 Docker 容器名 ----
# 本开发机的五台容器是手工 docker run 起的（milvus-etcd / my_milvus / my-redis /
# my-Elasticsearch / my_neo4j），端口分别是 2379(仅容器内) / 19530 / 6379 / 9200 / 7687。
# 如果你在新机器上用 docker-compose 起，容器名会变（xjms-redis / xjms-es 等），
# 但对外端口不变。所以本脚本**先按端口查容器**，名字只做辅助显示。
#
# ⚠️ Milvus 是「etcd + Milvus 核心」两个容器：19530 通不代表集合元数据可用 ——
#    下面的预检只探 19530，如果启动日志报 Milvus 未就绪，先看 milvus-etcd 是不是没起来。

[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

# ---------- 路径 ----------
$Root      = $PSScriptRoot
$Backend   = Join-Path $Root "backend"
$Frontend  = Join-Path $Root "frontend"
$VenvPython= Join-Path $Backend ".venv\Scripts\python.exe"
$EnvFile   = Join-Path $Backend ".env"
$VitePort  = 5173
$ApiPort   = 8000

# ---------- 工具函数 ----------
function Write-Step { param([string]$Text) Write-Host ""; Write-Host ">>> $Text" -ForegroundColor Cyan }
function Write-Ok   { param([string]$Text) Write-Host "    OK  $Text" -ForegroundColor Green }
function Write-Warn { param([string]$Text) Write-Host "    !!  $Text" -ForegroundColor Yellow }
function Write-Err  { param([string]$Text) Write-Host "    XX  $Text" -ForegroundColor Red }

function Test-ContainerUp {
    param(
        [string]$Name,
        [int]$Port = 0
    )
    # Strategy 1: by container name (works if you used the same names)
    try {
        $status = docker ps --filter "name=$Name" --format "{{.Status}}" 2>$null
        if ($status -match "^Up") { return $true }
    } catch {}
    # Strategy 2: by port (works regardless of container name — more portable)
    if ($Port -gt 0) {
        try {
            $portListening = netstat -ano 2>$null | Select-String ":$Port\s+.*LISTENING"
            if ($portListening) { return $true }
        } catch {}
    }
    return $false
}

function Get-PythonCmd {
    if (Test-Path $VenvPython) { return $VenvPython }
    # 回退：本机后端依赖装在 Anaconda（backend 无 .venv 时用它）。
    # 不要回落到「全局 python」—— 全局那个是托管解释器，没装 fastapi/langgraph，
    # 用它起后端口会直接 ImportError。
    $anacondaPy = "B:\Anconda3\python.exe"
    if (Test-Path $anacondaPy) {
        Write-Warn ".venv 不存在，使用 Anaconda python（本机后端依赖所在）。"
        return $anacondaPy
    }
    $globalPyCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($globalPyCmd -and $globalPyCmd.Source) {
        Write-Warn ".venv 与 Anaconda 均不存在，回退全局 python（可能缺依赖）。"
        return "python"
    }
    Write-Err "找不到 python。请先安装或激活虚拟环境。"
    exit 1
}

# ==============================================================
# ① 预检
# ==============================================================
Write-Host "================================================" -ForegroundColor Cyan
Write-Host "  Syncore · One-Click Start" -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan

Write-Step "Docker infrastructure"
# Each entry: known container name + default port. Check tries name first,
# then falls back to port (so docker-compose names work too).
$containers = @(
    @{ Name = "my_milvus";           Port = 19530; Desc = "Milvus" },
    @{ Name = "my-redis";            Port = 6379;  Desc = "Redis" },
    @{ Name = "my-Elasticsearch";    Port = 9200;  Desc = "Elasticsearch" },
    @{ Name = "my_neo4j";            Port = 7687;  Desc = "Neo4j" }
)
$allUp = $true
foreach ($c in $containers) {
    $up = Test-ContainerUp -Name $c.Name -Port $c.Port
    if ($up) {
        Write-Ok "$($c.Desc) port $($c.Port)"
    } else {
        Write-Err "$($c.Desc) port $($c.Port) not reachable"
        $allUp = $false
    }
}
if (-not $allUp) {
    Write-Warn "Some containers down. App will run in degraded mode."
    $continue = Read-Host "Continue anyway? (y/N)"
    if ($continue -ne "y" -and $continue -ne "Y") { Write-Host "Cancelled."; exit 1 }
}

Write-Step "Backend environment"
$pythonCmd = Get-PythonCmd
if (-not (Test-Path $EnvFile)) {
    Write-Err "backend\.env missing. Copy .env.example and fill in API keys:"
    Write-Host "    cd backend; copy .env.example .env" -ForegroundColor Yellow
    exit 1
} else { Write-Ok "backend\.env exists" }

Write-Step "Frontend"
$nodeModules = Join-Path $Frontend "node_modules"
if (Test-Path $nodeModules) { Write-Ok "node_modules present" }
else {
    Write-Err "node_modules missing. Run: cd frontend && npm install"
    exit 1
}

# ==============================================================
# ② Port conflict check
# ==============================================================
Write-Step "Port check"
$portBlocked = $false
foreach ($port in @($ApiPort, $VitePort)) {
    $used = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($used) {
        $portBlocked = $true
        Write-Warn "Port $port is in use (PID=$($used[0].OwningProcess))."
    } else { Write-Ok "Port $port free" }
}
if ($portBlocked) {
    Write-Warn "端口被占用，继续启动会报 WinError 10013（端口已被监听）。"
    $go = Read-Host "先运行 .\stop.ps1 再重试。仍要强制继续吗? (y/N)"
    if ($go -ne "y" -and $go -ne "Y") { Write-Host "Cancelled."; exit 1 }
}

# ==============================================================
# ④ Launch backend → wait ready → launch frontend
# ==============================================================
Write-Step "Launching backend (new window)"
# 用确定的 python 跑 uvicorn，不依赖 PATH 里恰好有个对的 uvicorn。
# 显式 --host：uvicorn CLI 不读 .env 的 API_HOST，不给就只绑 IPv4 的 127.0.0.1。
$backendCmd = "Set-Location '$Backend'; "
if (Test-Path $VenvPython) {
    $venvActivate = Join-Path $Backend ".venv\Scripts\Activate.ps1"
    if (Test-Path $venvActivate) { $backendCmd += "& '$venvActivate'; " }
}
$backendCmd += "Write-Host '[BACKEND] $pythonCmd -m uvicorn app.main:app --reload --port $ApiPort' -ForegroundColor Cyan; & '$pythonCmd' -m uvicorn app.main:app --reload --host 127.0.0.1 --port $ApiPort"
Start-Process powershell -ArgumentList "-NoExit", "-NoProfile", "-Command", $backendCmd -WorkingDirectory $Backend

# 等后端就绪再起前端 —— 后端冷启动约 28.5s、前端 vite 约 1.2s，
# 同时起会导致「打开页面一片 ECONNREFUSED」的启动竞态。
# 就绪判据用 /api/info（只读配置、秒回）；不能用 /api/health —— 它逐个探活
# Milvus/ES/Redis/Neo4j，Docker 没起时单次要 30+ 秒，会把「已就绪但依赖降级」
# 的后端误判成没起来（desktop/main.js 头注同款结论）。
$readyTimeout = 90
Write-Step "Waiting for backend /api/info (cold start ~30s)"
$ready = $false
for ($i = 1; $i -le $readyTimeout; $i++) {
    $code = & curl.exe --noproxy '*' -s -o NUL -w '%{http_code}' "http://127.0.0.1:$ApiPort/api/info" 2>$null
    if ($code -eq '200') { $ready = $true; break }
    Write-Host "    ... ${i}s" -ForegroundColor DarkGray
    Start-Sleep -Seconds 1
}
if ($ready) { Write-Ok "backend ready after ~${i}s" }
else { Write-Warn "backend not ready after ${readyTimeout}s — starting frontend anyway (may briefly show ECONNREFUSED)" }

Write-Step "Launching frontend (new window)"
$frontendCmd = "Set-Location '$Frontend'; Write-Host '[FRONTEND] npm run dev' -ForegroundColor Cyan; npm run dev"
Start-Process powershell -ArgumentList "-NoExit", "-NoProfile", "-Command", $frontendCmd -WorkingDirectory $Frontend

# ==============================================================
# ⑤ Tip
# ==============================================================
Write-Host ""
Write-Host "================================================" -ForegroundColor Green
Write-Host "  Starting... available in ~5-15 seconds" -ForegroundColor Green
Write-Host "================================================" -ForegroundColor Green
Write-Host "  API:      http://localhost:$ApiPort" -ForegroundColor Gray
Write-Host "  Frontend: http://localhost:$VitePort" -ForegroundColor Gray
Write-Host "  Health:   http://localhost:$ApiPort/api/health" -ForegroundColor Gray
Write-Host ""
Write-Host "  Stop: .\stop.ps1   (add -Deps to also stop Docker containers;" -ForegroundColor Gray
Write-Host "        or close those two PowerShell windows)" -ForegroundColor Gray
Write-Host "================================================" -ForegroundColor Green

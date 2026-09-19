# ============================================
# PicSystem 一键部署脚本（Windows PowerShell）
#   右键使用 PowerShell 运行，或：
#   powershell -ExecutionPolicy Bypass -File deploy.ps1
# 可选参数：
#   -Port 9090            对外 HTTP 端口
#   -Builtin / -External  内置或外部上游 chatgpt2api
#   -AdminUser admin      管理员用户名
#   -AdminPass <密码>      管理员密码（缺省自动生成）
#   -Yes                  全部使用默认值，非交互
# ============================================
[CmdletBinding()]
param(
    [int]$Port = 0,
    [switch]$Builtin,
    [switch]$External,
    [string]$AdminUser = "admin",
    [string]$AdminPass = "",
    [switch]$Yes
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $MyInvocation.MyCommand.Path)

function Info($msg) { Write-Host "[*] $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "[✓] $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "[!] $msg" -ForegroundColor Yellow }
function Die($msg)  { Write-Host "[x] $msg" -ForegroundColor Red; exit 1 }

function Ask($prompt, $default) {
    if ($Yes) { return $default }
    $reply = Read-Host "$prompt [$default]"
    if ([string]::IsNullOrWhiteSpace($reply)) { return $default }
    return $reply
}

function Rand-Hex([int]$bytes) {
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $buf = New-Object byte[] $bytes
    $rng.GetBytes($buf)
    ($buf | ForEach-Object { $_.ToString("x2") }) -join ""
}

# ---- 1. 环境检查 ----
try { docker version | Out-Null } catch { Die "未找到可用的 Docker，请先安装 Docker Desktop" }
try { docker compose version | Out-Null } catch { Die "未找到 docker compose，请升级 Docker Desktop" }
Ok "docker 环境检查通过"

# ---- 2. 已有配置则复用 ----
if (Test-Path ".env") {
    $reuse = if ($Yes) { "y" } else { Read-Host "检测到已有 .env 配置，直接复用并启动？[Y/n]" }
    if ([string]::IsNullOrWhiteSpace($reuse)) { $reuse = "y" }
    if ($reuse.ToLower() -eq "y") {
        Info "复用现有 .env，开始构建启动…"
        docker compose up -d --build
        Ok "已启动。查看初始管理员账号密码：Get-Content .env | Select-String ADMIN_"
        exit 0
    }
    $stamp = Get-Date -Format "yyyyMMddHHmmss"
    Move-Item ".env" ".env.bak.$stamp"
    Info "旧配置已备份"
}

# ---- 3. 交互配置 ----
if ($Port -le 0) {
    $raw = Ask "对外 HTTP 端口" "9090"
    $parsed = 0
    if (-not [int]::TryParse($raw, [ref]$parsed) -or $parsed -lt 1 -or $parsed -gt 65535) {
        Die "端口无效：$raw（需为 1-65535 的数字）"
    }
    $Port = $parsed
}
$mode = ""
if ($Builtin) { $mode = "builtin" }
elseif ($External) { $mode = "external" }
else {
    $reply = if ($Yes) { "y" } else { Read-Host "是否内置部署上游 chatgpt2api？[Y/n]" }
    if ([string]::IsNullOrWhiteSpace($reply)) { $reply = "y" }
    $mode = if ($reply.ToLower() -eq "y") { "builtin" } else { "external" }
}
$AdminUser = Ask "管理员用户名" $AdminUser
if ([string]::IsNullOrWhiteSpace($AdminPass) -and -not $Yes) {
    $secure = Read-Host "管理员密码（留空自动生成）" -AsSecureString
    $AdminPass = [System.Net.NetworkCredential]::new("", $secure).Password
}
# 初始管理员密码：24 位十六进制（96 位熵），.NET 加密安全随机源
if ([string]::IsNullOrWhiteSpace($AdminPass)) { $AdminPass = Rand-Hex 12 }

$upstreamBase = "http://chatgpt2api"
$upstreamKey = ""
if ($mode -eq "external") {
    $upstreamBase = Ask "外部 chatgpt2api 地址（如 https://api.example.com）" ""
    if ([string]::IsNullOrWhiteSpace($upstreamBase)) { Die "外部模式必须提供上游地址" }
    $upstreamKey = Ask "外部 chatgpt2api 密钥（CHATGPT2API_AUTH_KEY）" ""
    if ([string]::IsNullOrWhiteSpace($upstreamKey)) { Die "外部模式必须提供上游密钥" }
}
else {
    $upstreamKey = Rand-Hex 24
}

$jwtSecret = Rand-Hex 32
$pgPassword = Rand-Hex 24

# ---- 4. 写入 .env ----
$lines = @(
    "HTTP_PORT=$Port",
    "TZ=Asia/Shanghai",
    "ADMIN_USERNAME=$AdminUser",
    "ADMIN_PASSWORD=$AdminPass",
    "JWT_SECRET=$jwtSecret",
    "UPSTREAM_API_KEY=$upstreamKey",
    "UPSTREAM_BASE_URL=$upstreamBase",
    "POSTGRES_DB=picsystem",
    "POSTGRES_USER=picsystem",
    "POSTGRES_PASSWORD=$pgPassword",
    "CHATGPT2API_IMAGE=ghcr.io/yukkcat/chatgpt2api:latest",
    "CHATGPT2API_CONSOLE_PORT=3000",
    "COOKIE_SECURE=false"
)
if ($mode -eq "builtin") { $lines += "COMPOSE_PROFILES=builtin-upstream" }
$lines -join "`n" | Out-File -FilePath ".env" -Encoding ascii -NoNewline
Ok ".env 已生成"

# ---- 5. 内置上游的初始化文件 ----
$consoleAccess = "n"
if ($mode -eq "builtin") {
    if (-not $Yes) {
        $consoleAccess = Read-Host "是否把上游控制台绑定到本机 127.0.0.1:3000（用于添加 ChatGPT 账号，公网不可达）？[y/N]"
        if ([string]::IsNullOrWhiteSpace($consoleAccess)) { $consoleAccess = "n" }
    }
    if ($consoleAccess.ToLower() -eq "y") {
        # Windows 的 COMPOSE_FILE 分隔符为分号（Linux/macOS 为冒号）
        Add-Content -Path ".env" -Value "`nCOMPOSE_FILE=docker-compose.yml;docker-compose.console.yml" -Encoding ascii
        Ok "上游控制台将绑定到 127.0.0.1:3000"
    }
    New-Item -ItemType Directory -Force -Path "data/chatgpt2api" | Out-Null
    if (-not (Test-Path "data/chatgpt2api-config.json")) {
        "{}" | Out-File -FilePath "data/chatgpt2api-config.json" -Encoding ascii
    }
    Info "正在拉取 chatgpt2api 镜像（ghcr.io，如网络不佳可稍后重试）…"
    docker compose pull chatgpt2api
    if ($LASTEXITCODE -ne 0) { Warn "镜像拉取失败，稍后可手动 docker compose pull chatgpt2api" }
}

# ---- 6. 构建启动 ----
Info "构建并启动全部服务…"
docker compose up -d --build
if ($LASTEXITCODE -ne 0) { Die "docker compose 启动失败，请查看上方输出" }

# ---- 7. 等待健康 ----
Info "等待服务就绪…"
$healthy = $false
foreach ($i in 1..60) {
    try {
        Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 3 | Out-Null
        $healthy = $true
        break
    }
    catch { Start-Sleep -Seconds 2 }
}
if ($healthy) { Ok "PicSystem 已启动！" }
else { Warn "健康检查暂未通过，请用 docker compose logs -f 查看日志" }

Write-Host ""
Write-Host "============================================"
Write-Host "  站点地址      ：http://<服务器IP>:$Port"
Write-Host "  管理员账号    ：$AdminUser"
Write-Host "  管理员密码    ：$AdminPass"
if ($mode -eq "builtin") {
    if ($consoleAccess.ToLower() -eq "y") {
        Write-Host "  2api 控制台   ：http://127.0.0.1:3000 （仅服务器本机访问，密钥见 .env 的 UPSTREAM_API_KEY）"
    }
    else {
        Write-Host "  2api 控制台   ：默认零端口暴露。如需访问：在 .env 加 COMPOSE_FILE=docker-compose.yml;docker-compose.console.yml（Windows 分隔符为分号）后 docker compose up -d，然后访问 http://127.0.0.1:3000"
    }
    Write-Host "  注意：请先在 2api 控制台添加 ChatGPT 账号，系统才能正常出图/对话"
}
Write-Host "  配置文件      ：$(Get-Location)\.env"
Write-Host "  忘记账号密码  ：Get-Content .env | Select-String ADMIN_"
Write-Host "  常用命令      ：docker compose logs -f / docker compose restart / docker compose down"
Write-Host "============================================"
Warn "请妥善保存以上账号信息；首次登录后建议立即在「个人中心」修改管理员密码"
Warn "内置 2api 的原面板密钥已随机生成并仅用于系统内部对接，日常运营无需使用"

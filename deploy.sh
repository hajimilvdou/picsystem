#!/usr/bin/env bash
# ============================================
# PicSystem 一键部署脚本（Linux / macOS）
#   curl / git clone 后：bash deploy.sh
# 可选参数：
#   --port 9090            对外 HTTP 端口
#   --builtin / --external 内置或外部上游 chatgpt2api
#   --admin-user admin     管理员用户名
#   --admin-pass <密码>     管理员密码（缺省自动生成）
#   --yes                  全部使用默认值，非交互
# ============================================
set -euo pipefail

cd "$(dirname "$0")"

HTTP_PORT=""
MODE=""
ADMIN_USER="admin"
ADMIN_PASS=""
ASSUME_YES=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) HTTP_PORT="$2"; shift 2 ;;
    --builtin) MODE="builtin"; shift ;;
    --external) MODE="external"; shift ;;
    --admin-user) ADMIN_USER="$2"; shift 2 ;;
    --admin-pass) ADMIN_PASS="$2"; shift 2 ;;
    --yes|-y) ASSUME_YES=true; shift ;;
    *) echo "未知参数：$1"; exit 1 ;;
  esac
done

info() { printf '\033[36m[*]\033[0m %s\n' "$1"; }
ok()   { printf '\033[32m[✓]\033[0m %s\n' "$1"; }
warn() { printf '\033[33m[!]\033[0m %s\n' "$1"; }
die()  { printf '\033[31m[x]\033[0m %s\n' "$1" >&2; exit 1; }

ask() { # ask <变量名> <提示> <默认值>
  local var="$1" prompt="$2" default="$3"
  if [[ "$ASSUME_YES" == true ]]; then
    printf -v "$var" '%s' "$default"
  else
    read -r -p "$prompt [$default]: " reply
    printf -v "$var" '%s' "${reply:-$default}"
  fi
}

rand_hex() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex "$1"
  else
    head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'
  fi
}

# ---- 1. 环境检查 ----
command -v docker >/dev/null 2>&1 || die "未找到 docker，请先安装 Docker：https://docs.docker.com/get-docker/"
docker compose version >/dev/null 2>&1 || die "未找到 docker compose 插件，请升级 Docker 到 20.10+"
ok "docker 环境检查通过"

# ---- 2. 已有配置则复用 ----
if [[ -f .env ]]; then
  if [[ "$ASSUME_YES" == true ]]; then
    REUSE="y"
  else
    read -r -p "检测到已有 .env 配置，直接复用并启动？[Y/n]: " REUSE
    REUSE="${REUSE:-y}"
  fi
  if [[ "${REUSE,,}" == "y" ]]; then
    info "复用现有 .env，开始构建启动…"
    docker compose up -d --build
    ok "已启动。查看初始管理员账号密码：grep ADMIN_ .env"
    exit 0
  fi
  mv .env ".env.bak.$(date +%Y%m%d%H%M%S)"
  info "旧配置已备份"
fi

# ---- 3. 交互配置 ----
[[ -z "$HTTP_PORT" ]] && ask HTTP_PORT "对外 HTTP 端口" "9090"
if ! [[ "$HTTP_PORT" =~ ^[0-9]+$ ]] || (( HTTP_PORT < 1 || HTTP_PORT > 65535 )); then
  die "端口无效：$HTTP_PORT（需为 1-65535 的数字）"
fi
if [[ -z "$MODE" ]]; then
  if [[ "$ASSUME_YES" == true ]]; then
    MODE="builtin"
  else
    read -r -p "是否内置部署上游 chatgpt2api？[Y/n]: " reply
    reply="${reply:-y}"
    [[ "${reply,,}" == "y" ]] && MODE="builtin" || MODE="external"
  fi
fi
ask ADMIN_USER "管理员用户名" "$ADMIN_USER"
if [[ -z "$ADMIN_PASS" && "$ASSUME_YES" != true ]]; then
  read -r -s -p "管理员密码（留空自动生成）: " ADMIN_PASS
  echo
fi
# 初始管理员密码：24 位十六进制（96 位熵），openssl / /dev/urandom 安全随机源
[[ -z "$ADMIN_PASS" ]] && ADMIN_PASS="$(rand_hex 12)"

UPSTREAM_BASE_URL="http://chatgpt2api"
if [[ "$MODE" == "external" ]]; then
  UPSTREAM_BASE_URL=""
  UPSTREAM_API_KEY=""
  ask UPSTREAM_BASE_URL "外部 chatgpt2api 地址（如 https://api.example.com）" ""
  [[ -z "$UPSTREAM_BASE_URL" ]] && die "外部模式必须提供上游地址"
  ask UPSTREAM_API_KEY "外部 chatgpt2api 密钥（CHATGPT2API_AUTH_KEY）" ""
  [[ -z "$UPSTREAM_API_KEY" ]] && die "外部模式必须提供上游密钥"
fi

JWT_SECRET="$(rand_hex 32)"
POSTGRES_PASSWORD="$(rand_hex 24 | tr -d ' ')"
[[ "$MODE" == "builtin" ]] && UPSTREAM_API_KEY="$(rand_hex 24)"

# ---- 4. 写入 .env ----
umask 077
cat > .env <<EOF
HTTP_PORT=$HTTP_PORT
TZ=Asia/Shanghai
ADMIN_USERNAME=$ADMIN_USER
ADMIN_PASSWORD=$ADMIN_PASS
JWT_SECRET=$JWT_SECRET
UPSTREAM_API_KEY=$UPSTREAM_API_KEY
UPSTREAM_BASE_URL=$UPSTREAM_BASE_URL
POSTGRES_DB=picsystem
POSTGRES_USER=picsystem
POSTGRES_PASSWORD=$POSTGRES_PASSWORD
CHATGPT2API_IMAGE=ghcr.io/yukkcat/chatgpt2api:latest
CHATGPT2API_CONSOLE_PORT=3000
COOKIE_SECURE=false
EOF
if [[ "$MODE" == "builtin" ]]; then
  echo "COMPOSE_PROFILES=builtin-upstream" >> .env
fi
ok ".env 已生成（权限 600）"

# ---- 5. 内置上游的初始化文件 ----
CONSOLE_ACCESS="n"
if [[ "$MODE" == "builtin" ]]; then
  if [[ "$ASSUME_YES" != true ]]; then
    read -r -p "是否把上游控制台绑定到本机 127.0.0.1:3000（用于添加 ChatGPT 账号，公网不可达）？[y/N]: " CONSOLE_ACCESS
    CONSOLE_ACCESS="${CONSOLE_ACCESS:-n}"
  fi
  if [[ "${CONSOLE_ACCESS,,}" == "y" ]]; then
    echo "COMPOSE_FILE=docker-compose.yml:docker-compose.console.yml" >> .env
    ok "上游控制台将绑定到 127.0.0.1:3000"
  fi
  mkdir -p data/chatgpt2api
  if [[ ! -f data/chatgpt2api-config.json ]]; then
    printf '{}\n' > data/chatgpt2api-config.json
  fi
  info "正在拉取 chatgpt2api 镜像（ghcr.io，如网络不佳可稍后重试）…"
  docker compose pull chatgpt2api || warn "镜像拉取失败，稍后可手动 docker compose pull chatgpt2api"
fi

# ---- 6. 构建启动 ----
info "构建并启动全部服务…"
docker compose up -d --build

# ---- 7. 等待健康 ----
info "等待服务就绪…"
for i in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:${HTTP_PORT}/api/health" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

if curl -fsS "http://127.0.0.1:${HTTP_PORT}/api/health" >/dev/null 2>&1; then
  ok "PicSystem 已启动！"
  # 数据库迁移在 api 启动时自动完成，这里做结果确认：升级是否成功一目了然
  info "数据库迁移状态："
  if ! docker compose exec -T api python -m app.migrate status; then
    warn "未能读取迁移状态，可用 docker compose logs api 查看「[迁移]」日志"
  fi
else
  warn "健康检查暂未通过，请用 docker compose logs -f 查看日志"
  warn "若日志中出现「迁移」报错，说明数据库升级失败——服务会拒绝启动以免带半截结构对外服务"
fi

echo
echo "============================================"
echo "  站点地址      ：http://<服务器IP>:${HTTP_PORT}"
echo "  管理员账号    ：${ADMIN_USER}"
echo "  管理员密码    ：${ADMIN_PASS}"
if [[ "$MODE" == "builtin" ]]; then
  if [[ "${CONSOLE_ACCESS,,}" == "y" ]]; then
echo "  2api 控制台   ：http://127.0.0.1:3000 （仅服务器本机访问，密钥见 .env 的 UPSTREAM_API_KEY）"
  else
echo "  2api 控制台   ：默认零端口暴露。如需访问：在 .env 加 COMPOSE_FILE=docker-compose.yml:docker-compose.console.yml 后 docker compose up -d，然后访问 http://127.0.0.1:3000"
  fi
echo "  注意：请先在 2api 控制台添加 ChatGPT 账号，系统才能正常出图/对话"
fi
echo "  配置文件      ：$(pwd)/.env"
echo "  忘记账号密码  ：grep ADMIN_ .env"
echo "  常用命令      ：docker compose logs -f / docker compose restart / docker compose down"
echo "============================================"
warn "请妥善保存以上账号信息；首次登录后建议立即在「个人中心」修改管理员密码"
warn "内置 2api 的原面板密钥已随机生成并仅用于系统内部对接，日常运营无需使用"

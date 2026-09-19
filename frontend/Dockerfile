# ---- 构建前端 ----
FROM node:22-alpine AS build
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm run build

# ---- 运行镜像 ----
FROM nginx:1.27-alpine
COPY --from=build /build/dist /usr/share/nginx/html
# 作为 envsubst 模板：容器启动时把 ${XFF_MODE} 替换为环境变量值（默认 $remote_addr）
COPY nginx.conf /etc/nginx/templates/default.conf.template
EXPOSE 80
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1/ || exit 1

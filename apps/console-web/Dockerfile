# 控制台镜像。多阶段：构建产物是静态文件，运行时不需要 node。
#
# nginx 用 unprivileged 变体：容器内非 root 运行（M0 §9 的 Sandbox 分级要求
# 动作型工具非 root；控制台不是工具，但没有理由给它 root）。

FROM node:22-alpine AS build
WORKDIR /app
# 先只拷依赖清单，让依赖层可被缓存——改一行 tsx 不该重装 node_modules。
COPY package.json package-lock.json ./
RUN npm ci
COPY tsconfig.json vite.config.ts index.html ./
COPY src ./src
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.27-alpine
# 8081 而不是 80：unprivileged 镜像默认监听 8080，而 8080 已被控制面占用。
EXPOSE 8081
USER root
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
# API 地址在容器启动时注入 index.html，不在构建期烧进 bundle：
# 同一份镜像要能指向不同的控制面。
COPY docker-entrypoint.sh /docker-entrypoint.d/40-inject-api-base.sh
# 注入脚本要用 sed -i 改写 index.html，而 sed -i 是「写临时文件再改名」，
# 因此需要**目录**可写而不只是文件可写。默认 COPY 进来的目录属 root，
# 非 root 用户会以 "can't create temp file" 失败。
RUN chown -R 101:101 /usr/share/nginx/html \
    && chmod +x /docker-entrypoint.d/40-inject-api-base.sh
USER 101

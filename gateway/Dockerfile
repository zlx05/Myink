# Myink Go 网关镜像（阶段 5 多阶段）：前端 dist + 网关二进制 + 精简运行层。
# 最终镜像包含 web/dist（网关静态托管，单端口 8080 同源出页面 + API + SSE）。
#
# 基础镜像源可用 build arg 覆盖（国内 docker.io 直连可能超时；compose 当前环境传
# NODE_IMAGE/GOLANG_IMAGE/DEBIAN_IMAGE=docker.m.daocloud.io/library/...，见 docker-compose.yml）。
#
# 阶段 1：构建前端静态产物（vite build，base=/，产物含 index.html/assets/favicon）
ARG NODE_IMAGE=node:20-alpine
ARG GOLANG_IMAGE=golang:1.26.1-alpine
ARG DEBIAN_IMAGE=debian:bookworm-slim
FROM ${NODE_IMAGE} AS web
# npm 源可覆盖（国内 registry.npmjs.org 可能不通，compose 传 npmmirror）
ARG NPM_REGISTRY=https://registry.npmjs.org
WORKDIR /app/web
COPY web/package.json web/package-lock.json ./
RUN npm ci --registry=${NPM_REGISTRY}
COPY web/ ./
RUN npm run build

# 阶段 2：构建网关二进制（pure Go 静态编译，alpine 可跑）
FROM ${GOLANG_IMAGE} AS build
# go 模块代理可覆盖（国内 proxy.golang.org 不通，compose 传 goproxy.cn）
ARG GOPROXY=https://proxy.golang.org,direct
ENV GOPROXY=${GOPROXY}
WORKDIR /app/gateway
COPY gateway/go.mod gateway/go.sum ./
RUN go mod download
COPY gateway/ ./
RUN CGO_ENABLED=0 go build -o /out/gateway ./cmd/gateway

# 阶段 3：精简运行层（debian slim，含 dist + 二进制）
FROM ${DEBIAN_IMAGE}
WORKDIR /app
COPY --from=build /out/gateway /usr/local/bin/gateway
COPY --from=web /app/web/dist /app/dist
EXPOSE 8080
# compose 通过环境变量注入 REDIS_ADDR/PYTHON_API_BASE/WEB_DIST_DIR（无需 .env 文件，main.go loadDotEnv 找不到即忽略）
ENTRYPOINT ["gateway"]

# Myink Python 镜像：myink-api（uvicorn）/ myink-worker（队列消费）共用。
# 只 COPY Python 侧（pyproject + src/），web/gateway/测试不进镜像。
# 依赖全量安装（含 sentence-transformers/bge-m3，重但按 EMBED_ENABLED 开关加载，见 docker-compose.yml）。
#
# 基础镜像源可用 build arg 覆盖（国内 docker.io 直连可能超时；compose 当前环境传
# PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.12-slim，见 docker-compose.yml）。
ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}
WORKDIR /app

# pip 源可覆盖（国内 pypi.org 直连慢/不通，compose 传清华源，见 docker-compose.yml）
ARG PIP_INDEX_URL=https://pypi.org/simple

COPY pyproject.toml ./
COPY src/ ./src/
# 装核心依赖（不含 ml extras——sentence-transformers/torch ~2GB 且国内源下载易抖，
# embedder 延迟导入 + EMBED_ENABLED=0 默认不加载，纯关系链路可用；需要时 pip install -e .[ml]）。
# cache mount：pip wheel 缓存跨 build 复用（网络抖动中断后重跑不重下已拉取部分）；
# --timeout/--retries 应对国内大依赖读超时抖动。
RUN --mount=type=cache,target=/root/.cache/pip pip install . --index-url ${PIP_INDEX_URL} --timeout 120 --retries 5

# 镜像自洽：核心依赖不含 ml extras（torch/sentence-transformers），默认关闭向量
#（config.py 默认 enable=1 会去 import 未装的 torch 报错而非降级）；compose 可按需覆盖
ENV EMBED_ENABLED=0

EXPOSE 8100
# 默认入口；compose 中 myink-api 覆盖为 `myink init && myink-api`，worker 覆盖为 `myink-worker`
CMD ["myink-api"]

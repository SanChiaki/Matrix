# Matrix（agentnet）：Python 3.14 + uv，加上 DSH 运行时（Node 24 + @deepseek-ai/dsh）。
# 只在 compose 网络里对 Caddy 开放 8765，不对宿主机发布端口；对外访问一律经 Caddy（HTTPS + basic auth）。
FROM python:3.14-slim-bookworm

ARG DSH_VERSION=0.2.0-rc.2
ARG NODE_MAJOR=24
ARG UV_VERSION=0.12.1

# bubblewrap：dsh 在 Linux 上的沙箱 runner（bwrap → Landlock）。容器需要 seccomp=unconfined 才能建命名空间，见 compose。
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg bubblewrap git \
 && curl -fsSL https://deb.nodesource.com/setup_${NODE_MAJOR}.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && npm install -g @deepseek-ai/dsh@${DSH_VERSION} \
 && pip install --no-cache-dir uv==${UV_VERSION} \
 && apt-get purge -y gnupg && apt-get autoremove -y && rm -rf /var/lib/apt/lists/* /root/.npm

# 非 root 运行：dsh 的会话、凭据都在这个用户的 ~/.dsh（挂卷持久化）
RUN useradd --create-home --uid 1000 matrix \
 && mkdir -p /app /data /home/matrix/.dsh && chown matrix:matrix /app /data /home/matrix/.dsh
WORKDIR /app
COPY --chown=matrix:matrix pyproject.toml uv.lock ./
COPY --chown=matrix:matrix agentnet ./agentnet
USER matrix
RUN uv sync --frozen --no-dev

ENV AGENTNET_HOST=0.0.0.0 \
    AGENTNET_DATA=/data \
    PATH=/app/.venv/bin:$PATH
EXPOSE 8765
CMD ["agentnet", "--runtime", "dsh", "--port", "8765"]

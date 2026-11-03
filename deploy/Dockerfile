# Matrix：Python 3.14 + uv，加上 DSH 运行时（Node 24 + @deepseek-ai/dsh）。
# 只在 compose 网络里对 Caddy 开放 8765，不对宿主机发布端口；对外访问一律经 Caddy（HTTPS + basic auth）。
FROM python:3.14-slim-bookworm

ARG DSH_VERSION=0.2.1-rc.5
ARG NODE_MAJOR=24
ARG UV_VERSION=0.12.1
# 无公网构建（README“内网 / 无公网构建”）：都空着 = 官方源，公网构建不受影响
ARG APT_MIRROR=""
ARG NPM_REGISTRY=""
ARG PYPI_INDEX=""
ARG NODE_TARBALL=""

# 内网根 CA：deploy/ca-certificates/*.crt 进系统证书库，在 apt 之前（基础镜像自带 ca-certificates），
# 构建里的下载（含经 HTTPS 拦截代理的）与运行时都用它。
# Node（dsh）和 Rust（uv）默认都只信自己带的 CA 包，不读 /etc/ssl/certs，也不读 SSL_CERT_FILE：
# NODE_USE_SYSTEM_CA / UV_SYSTEM_CERTS 让它们改用系统证书库（含公共根证书，对公网没有影响），内网 CA 进了系统库才信。
COPY deploy/ca-certificates/ /usr/local/share/ca-certificates/matrix/
ENV NODE_USE_SYSTEM_CA=1 \
    UV_SYSTEM_CERTS=1

# bubblewrap：dsh 在 Linux 上的沙箱 runner（bwrap → Landlock）。容器需要 seccomp=unconfined 才能建命名空间，见 compose。
# APT_MIRROR：镜像根地址（其下有 debian、debian-security），替换 deb.debian.org。
# NODE_TARBALL：Node 官方二进制包（node-v24.x-linux-x64.tar.xz）的地址，内网只镜像了 nodejs.org 时用它代替 NodeSource。
# 最后确认 dsh 在 PATH 上：装在别的前缀时补一个链接，找不到就让构建失败，不留到第一轮才 FileNotFoundError。
RUN set -eux; \
    rm -f /usr/local/share/ca-certificates/matrix/README /usr/local/share/ca-certificates/matrix/.gitignore; \
    update-ca-certificates; \
    if [ -n "$APT_MIRROR" ]; then \
      sed -i -e "s|https\?://deb.debian.org|${APT_MIRROR%/}|g" -e "s|https\?://security.debian.org|${APT_MIRROR%/}|g" \
        /etc/apt/sources.list.d/debian.sources; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl gnupg xz-utils bubblewrap git; \
    if [ -n "$NODE_TARBALL" ]; then \
      mkdir -p /opt/node; \
      curl -fsSL "$NODE_TARBALL" | tar -xJ -C /opt/node --strip-components=1; \
      ln -sf /opt/node/bin/node /opt/node/bin/npm /opt/node/bin/npx /usr/local/bin/; \
    else \
      curl -fsSL https://deb.nodesource.com/setup_${NODE_MAJOR}.x | bash -; \
      apt-get install -y --no-install-recommends nodejs; \
    fi; \
    npm install -g ${NPM_REGISTRY:+--registry="$NPM_REGISTRY"} @deepseek-ai/dsh@${DSH_VERSION}; \
    command -v dsh || ln -sf "$(npm prefix -g)/bin/dsh" /usr/local/bin/dsh; \
    command -v dsh; \
    # PYPI_INDEX 是 http:// 时：pip 要 --trusted-host，不然静默跳过这个源，报“找不到 uv”，像是源里没有；
    # 后面的 uv 也设上 UV_INSECURE_HOST。只给 http 源加：这两个开关对 https 源会关掉证书校验。
    PYPI_HTTP_HOST=$(printf '%s' "$PYPI_INDEX" | sed -nE 's#^http://([^/@]*@)?([^/]*).*#\2#p'); \
    pip install --no-cache-dir ${PYPI_INDEX:+--index-url "$PYPI_INDEX"} ${PYPI_HTTP_HOST:+--trusted-host "$PYPI_HTTP_HOST"} \
      uv==${UV_VERSION}; \
    apt-get purge -y gnupg; apt-get autoremove -y; rm -rf /var/lib/apt/lists/* /root/.npm

# 非 root 运行：dsh 的会话、凭据都在这个用户的 ~/.dsh（挂卷持久化）
RUN useradd --create-home --uid 1000 matrix \
 && mkdir -p /app /data /home/matrix/.dsh && chown matrix:matrix /app /data /home/matrix/.dsh
WORKDIR /app
COPY --chown=matrix:matrix pyproject.toml uv.lock ./
COPY --chown=matrix:matrix matrix ./matrix
USER matrix
# 换了 PyPI 源就不能 --frozen：frozen 直接用 uv.lock 里写死的 files.pythonhosted.org 地址，不看 index。
# 不 frozen 时从配置的源重新解析，已锁定的版本优先。
RUN set -eu; export UV_INSECURE_HOST=$(printf '%s' "$PYPI_INDEX" | sed -nE 's#^http://([^/@]*@)?([^/]*).*#\2#p'); \
    [ -n "$UV_INSECURE_HOST" ] || unset UV_INSECURE_HOST; \
    if [ -n "$PYPI_INDEX" ]; then UV_DEFAULT_INDEX="$PYPI_INDEX" uv sync --no-dev; else uv sync --frozen --no-dev; fi
# 企业内部 IM 的驱动包（docs/im-driver-authoring.md 第五节）：pip 安装参数，多个用空格隔开；空着不装
ARG IM_DRIVERS=""
RUN set -eu; export UV_INSECURE_HOST=$(printf '%s' "$PYPI_INDEX" | sed -nE 's#^http://([^/@]*@)?([^/]*).*#\2#p'); \
    [ -n "$UV_INSECURE_HOST" ] || unset UV_INSECURE_HOST; \
    if [ -n "$PYPI_INDEX" ]; then export UV_DEFAULT_INDEX="$PYPI_INDEX"; fi; \
    if [ -n "$IM_DRIVERS" ]; then uv pip install --no-cache $IM_DRIVERS; fi

ENV MATRIX_HOST=0.0.0.0 \
    MATRIX_DATA=/data \
    PATH=/app/.venv/bin:$PATH
EXPOSE 8765
CMD ["matrix", "--runtime", "dsh", "--port", "8765"]

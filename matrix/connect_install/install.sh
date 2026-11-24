#!/bin/sh
# matrix-connect 安装脚本（macOS / Linux）：下载自带 Python 的独立程序，不需要预先装 uv、git、Python。
# Matrix 服务器在 /connect/install.sh 提供它，设备页给的命令是：
#
#   curl -fsSL https://<服务器>/connect/install.sh | sh -s -- https://<服务器>
#
# 做的事：按本机系统与架构下载 matrix-connect-<系统>-<架构>.tar.gz（从这台服务器的 /connect/dist/，
# 服务器上没放就由它转到 GitHub Release），核对 sha256，解压到 ~/.local/share/matrix-connect/app，
# 在 ~/.local/bin 放一个链接；然后运行 `matrix-connect setup <服务器>`（配对 + 装成后台服务）。
# 已经配对过的电脑只升级程序并重装后台服务（`matrix-connect install`），不重新配对。
#
# 环境变量：MATRIX_CONNECT_HOME（安装目录）、MATRIX_CONNECT_BIN（链接放哪）、
# MATRIX_CONNECT_DOWNLOAD（下载地址前缀，默认 <服务器>/connect/dist）、MATRIX_CONNECT_NO_SETUP=1（只装不配对）、
# MATRIX_CONNECT_NO_MODIFY_PATH=1（不改 shell 配置）。之后的参数原样交给 setup，如 --name 笔记本。
set -eu

say() { printf '%s\n' "$*"; }
fail() { printf 'matrix-connect 安装失败：%s\n' "$*" >&2; exit 1; }

SERVER="${1:-${MATRIX_SERVER:-}}"
[ -n "$SERVER" ] || fail "要给服务器地址：curl -fsSL https://<服务器>/connect/install.sh | sh -s -- https://<服务器>"
[ $# -gt 0 ] && shift
SERVER="${SERVER%/}"

case "$(uname -s)" in
  Darwin) os=macos ;;
  Linux) os=linux ;;
  *) fail "不支持 $(uname -s)；Windows 请在 PowerShell 里用设备页给的命令" ;;
esac
case "$(uname -m)" in
  x86_64|amd64) arch=x64 ;;
  arm64|aarch64) arch=arm64 ;;
  *) fail "不支持的架构 $(uname -m)" ;;
esac
# Rosetta 下的终端报 x86_64：Apple 芯片上装原生版
if [ "$os" = macos ] && [ "$arch" = x64 ] && [ "$(sysctl -n sysctl.proc_translated 2>/dev/null || echo 0)" = 1 ]; then
  arch=arm64
fi

ASSET="matrix-connect-$os-$arch.tar.gz"
BASE="${MATRIX_CONNECT_DOWNLOAD:-$SERVER/connect/dist}"
HOME_DIR="${MATRIX_CONNECT_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/matrix-connect}"
BIN_DIR="${MATRIX_CONNECT_BIN:-$HOME/.local/bin}"

if command -v curl >/dev/null 2>&1; then
  fetch() { curl -fsSL --retry 3 -o "$2" "$1"; }
elif command -v wget >/dev/null 2>&1; then
  fetch() { wget -q -O "$2" "$1"; }
else
  fail "需要 curl 或 wget"
fi
sha256() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | cut -d' ' -f1
  else echo ""; fi
}

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT INT TERM

say "下载 $ASSET …"
fetch "$BASE/$ASSET" "$TMP/$ASSET" || fail "下载 $BASE/$ASSET 失败"
if fetch "$BASE/$ASSET.sha256" "$TMP/sum" 2>/dev/null; then
  want="$(cut -d' ' -f1 < "$TMP/sum")"
  got="$(sha256 "$TMP/$ASSET")"
  [ -z "$got" ] || [ "$got" = "$want" ] || fail "$ASSET 的 sha256 对不上（下载不完整或被篡改）"
fi

mkdir -p "$TMP/x" "$HOME_DIR" "$BIN_DIR"
tar -xzf "$TMP/$ASSET" -C "$TMP/x" || fail "解压失败"
[ -x "$TMP/x/matrix-connect/matrix-connect" ] || fail "安装包里没有 matrix-connect"

# 换上新的一份：正在跑的后台服务仍用着旧文件，挪开不删，重装服务后再清掉
rm -rf "$HOME_DIR/app.old"
[ -d "$HOME_DIR/app" ] && mv "$HOME_DIR/app" "$HOME_DIR/app.old"
mv "$TMP/x/matrix-connect" "$HOME_DIR/app"
ln -sf "$HOME_DIR/app/matrix-connect" "$BIN_DIR/matrix-connect"
EXE="$HOME_DIR/app/matrix-connect"
say "已安装：$BIN_DIR/matrix-connect → $EXE"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    if [ "${MATRIX_CONNECT_NO_MODIFY_PATH:-}" != 1 ]; then
      line="export PATH=\"$BIN_DIR:\$PATH\"  # matrix-connect"
      added=""
      for rc in "$HOME/.profile" "$HOME/.zshrc" "$HOME/.bashrc"; do
        case "$rc" in
          */.zshrc) [ -f "$rc" ] || [ "$(basename "${SHELL:-}")" = zsh ] || continue ;;
          */.bashrc) [ -f "$rc" ] || continue ;;
        esac
        grep -qs "# matrix-connect" "$rc" && continue
        printf '\n%s\n' "$line" >> "$rc"
        added="$added $rc"
      done
      [ -z "$added" ] || say "已把 $BIN_DIR 加进 PATH（写在$added，新开的终端生效）"
    else
      say "提示：$BIN_DIR 不在 PATH 里"
    fi ;;
esac

if [ "${MATRIX_CONNECT_NO_SETUP:-}" = 1 ]; then
  say "下一步：matrix-connect setup $SERVER"
elif "$EXE" status >/dev/null 2>&1; then
  say "这台电脑已经配对过：重装后台服务（不重新配对）"
  "$EXE" install
else
  "$EXE" setup "$SERVER" "$@"
fi
rm -rf "$HOME_DIR/app.old"

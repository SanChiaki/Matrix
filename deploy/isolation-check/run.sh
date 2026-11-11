#!/usr/bin/env bash
# 云侧执行面隔离验证（docs/cloud-runtime.md 第 11 节）。用法见同目录 README.md：
#   ./run.sh all        依次跑 info build t1 t2 t3 t4，最后打包结果
#   ./run.sh <步骤>...  只跑某几步：info build t1 t2 t3 t4 pack
# 环境变量：DEEPSEEK_API_KEY（有就跑真模型的检查）、DOCKER_NETWORK（如 host）、BUILD_ARGS（额外的 docker build 参数）、
#           IMAGE（镜像名）、OUT（结果目录）。
set -uo pipefail
KIT=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$KIT/../.." && pwd)
OUT=${OUT:-$KIT/results}
IMAGE=${IMAGE:-matrix:isolation-check}
NET=${DOCKER_NETWORK:-}
LIVE=""; [ -n "${DEEPSEEK_API_KEY:-}" ] && LIVE="--live"
mkdir -p "$OUT"
SUM=$OUT/summary.txt

# 安全选项组合。compose：现在 deploy/compose.yml 的写法；default：生产想要的（不放开 seccomp 与 /proc 遮挡）
SEC_COMPOSE="--security-opt seccomp=unconfined --security-opt systempaths=unconfined"
SEC_SECCOMP_ONLY="--security-opt seccomp=unconfined"
SEC_SYSPATHS_ONLY="--security-opt systempaths=unconfined"
SEC_DEFAULT="--security-opt no-new-privileges"

say() { echo "$*" | tee -a "$SUM"; }
have_runsc() { docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q '"runsc"'; }
runtimes() { echo runc; have_runsc && echo runsc; }

# 挂进容器的脚本：拷到一个谁都能读的临时目录（容器里是 uid 1000，可能读不了克隆下来的目录）
MNT=$(mktemp -d)
cp "$KIT"/probe.py "$KIT"/memsample.py "$KIT"/heavy.py "$KIT"/incontainer.sh "$KIT"/outer_dsh "$MNT"/
chmod 755 "$MNT"; chmod 644 "$MNT"/*; chmod 755 "$MNT"/incontainer.sh "$MNT"/outer_dsh
trap 'rm -rf "$MNT"' EXIT

# docker run 的公共参数：非 root、去掉全部 capabilities，代理与模型密钥只按名字透传（值不写进任何日志）
common() {  # common <runtime> <安全选项>
  local -a a=(--runtime="$1" --user matrix --cap-drop ALL -v "$MNT":/kit:ro)
  [ -n "$NET" ] && a+=(--network "$NET")
  for v in DEEPSEEK_API_KEY HTTPS_PROXY HTTP_PROXY NO_PROXY https_proxy http_proxy no_proxy; do
    [ -n "${!v:-}" ] && a+=(-e "$v")
  done
  # shellcheck disable=SC2206
  a+=($2)
  printf '%s\n' "${a[@]}"
}

drun() {  # drun <runtime> <安全选项> <命令...>：前台跑完
  local rt=$1 sec=$2; shift 2
  mapfile -t args < <(common "$rt" "$sec")
  docker run --rm "${args[@]}" "$IMAGE" "$@"
}

collect() {  # collect <日志> <前缀>：把关键行收进 summary
  grep -E '^(A_|B_)?(✓|✗|!|ok=|[A-Z_]+=|HEAVY|MEM_|全部通过|有检查没过)' "$1" | sed "s/^/$2 /" >> "$SUM" || true
}

to_mib() { awk -v s="$1" 'BEGIN { n = s + 0; if (s ~ /GiB/) n *= 1024; else if (s ~ /KiB/) n /= 1024; else if (s ~ /[0-9]B$/) n /= 1048576; printf "%d", n }'; }

measured() {  # measured <名字> <runtime> <安全选项> <命令...>：后台跑，每秒从宿主看一次容器内存（含 gVisor 自己的开销）
  local name=$1 rt=$2 sec=$3; shift 3
  mapfile -t args < <(common "$rt" "$sec")
  local cid peak=0 m mib t0=$SECONDS
  cid=$(docker run -d "${args[@]}" "$IMAGE" "$@") || { say "$name 起不来"; return; }
  while [ "$(docker inspect -f '{{.State.Running}}' "$cid" 2>/dev/null)" = true ]; do
    m=$(docker stats --no-stream --format '{{.MemUsage}}' "$cid" 2>/dev/null | awk '{print $1}')
    mib=$(to_mib "${m:-0B}"); [ "$mib" -gt "$peak" ] && peak=$mib
    sleep 1
  done
  docker logs "$cid" > "$OUT/$name.log" 2>&1
  docker rm "$cid" > /dev/null
  collect "$OUT/$name.log" "[$name]"
  say "[$name] HOST_PEAK_MEM=${peak}MiB WALL=$((SECONDS - t0))s"
}

info() {
  {
    echo "## info $(date -Is)"
    uname -a
    grep -E '^(PRETTY_NAME|VERSION_ID)=' /etc/os-release
    echo "cpus=$(nproc) mem=$(free -m | awk '/Mem:/ {print $2}')MiB kvm=$([ -e /dev/kvm ] && echo yes || echo no)"
    docker version --format 'docker server={{.Server.Version}}'
    docker info --format 'cgroup=v{{.CgroupVersion}} driver={{.CgroupDriver}} storage={{.Driver}} default_runtime={{.DefaultRuntime}} security={{json .SecurityOptions}} runtimes={{json .Runtimes}}'
    for k in kernel.apparmor_restrict_unprivileged_userns kernel.unprivileged_userns_clone user.max_user_namespaces; do
      echo "$k=$(sysctl -n $k 2>/dev/null || echo n/a)"
    done
    echo "runsc=$(runsc --version 2>/dev/null | head -1 || echo none)"
    echo "live=$([ -n "$LIVE" ] && echo yes || echo 'no（没设 DEEPSEEK_API_KEY，只跑不调真模型的检查）')"
  } 2>&1 | tee -a "$SUM"
}

build() {
  say "## build"
  # shellcheck disable=SC2086
  if docker build -f "$REPO/deploy/Dockerfile" -t "$IMAGE" ${BUILD_ARGS:-} "$REPO" > "$OUT/build.log" 2>&1; then
    say "build=ok"
  else
    say "build=FAIL（见 build.log 末尾）"; tail -20 "$OUT/build.log"; exit 1
  fi
}

t1() {  # 第 1 项：gVisor 里能否运行 dsh 的 bwrap
  say "## T1 gVisor 里能否运行 dsh 的 bwrap"
  have_runsc || { say "T1=SKIPPED（docker 没有注册 runsc，见 README 第 2 步）"; return; }
  for v in "default:$SEC_DEFAULT" "compose:$SEC_COMPOSE"; do
    local n=${v%%:*} sec=${v#*:}
    drun runsc "$sec" bash /kit/incontainer.sh > "$OUT/t1-$n-probe.log" 2>&1
    collect "$OUT/t1-$n-probe.log" "[t1-$n-probe]"
    drun runsc "$sec" timeout 1800 matrix-dsh-check $LIVE > "$OUT/t1-$n-dshcheck.log" 2>&1
    collect "$OUT/t1-$n-dshcheck.log" "[t1-$n-dshcheck]"
  done
}

t2() {  # 有 seccomp / AppArmor 的宿主上，普通容器（runc）里各种安全选项组合下 bwrap 能否运行
  say "## T2 runc 下各安全选项组合"
  for v in "compose:$SEC_COMPOSE" "seccomp-only:$SEC_SECCOMP_ONLY" "syspaths-only:$SEC_SYSPATHS_ONLY" "default:$SEC_DEFAULT"; do
    local n=${v%%:*} sec=${v#*:}
    drun runc "$sec" bash /kit/incontainer.sh > "$OUT/t2-$n.log" 2>&1
    collect "$OUT/t2-$n.log" "[t2-$n]"
  done
  for v in "compose:$SEC_COMPOSE" "default:$SEC_DEFAULT"; do
    local n=${v%%:*} sec=${v#*:}
    drun runc "$sec" timeout 1800 matrix-dsh-check $LIVE > "$OUT/t2-$n-dshcheck.log" 2>&1
    collect "$OUT/t2-$n-dshcheck.log" "[t2-$n-dshcheck]"
  done
}

t3() {  # 第 5 项：重活的兼容性、耗时与内存，runc 对 runsc
  say "## T3 重活（runc 对 runsc）"
  for rt in $(runtimes); do
    measured "t3-$rt-heavy" "$rt" "$SEC_COMPOSE" python /kit/heavy.py
  done
}

t4() {  # 第 6 项：一轮运行的内存、冷启动、空闲沙箱的开销
  say "## T4 内存与启动"
  for rt in $(runtimes); do
    measured "t4-$rt-round" "$rt" "$SEC_COMPOSE" python /kit/memsample.py -- \
      matrix-dsh-check ${LIVE:+--live --only reply,skill,delegate}
    local ms=() t
    for _ in 1 2 3 4 5; do
      t=$(date +%s%N); docker run --rm --runtime="$rt" "$IMAGE" true > /dev/null 2>&1; ms+=($(( ($(date +%s%N) - t) / 1000000 )))
    done
    say "[t4-$rt] COLD_START_MS=${ms[*]}"
    local before after ids=()
    before=$(free -m | awk '/Mem:/ {print $3}')
    for i in $(seq 1 10); do ids+=("$(docker run -d --runtime="$rt" "$IMAGE" sleep 600)"); done
    sleep 10
    after=$(free -m | awk '/Mem:/ {print $3}')
    local sum=0 m
    for id in "${ids[@]}"; do m=$(docker stats --no-stream --format '{{.MemUsage}}' "$id" | awk '{print $1}'); sum=$((sum + $(to_mib "$m"))); done
    docker rm -f "${ids[@]}" > /dev/null
    say "[t4-$rt] IDLE_10_SANDBOXES host_used_delta=$((after - before))MiB docker_stats_sum=${sum}MiB"
  done
}

pack() {
  local f="$KIT/isolation-results-$(hostname -s)-$(date +%Y%m%d-%H%M).tgz"
  tar czf "$f" -C "$(dirname "$OUT")" "$(basename "$OUT")"
  say "结果已打包：$f"
}

steps=("$@"); [ ${#steps[@]} -eq 0 ] && steps=(all)
[ "${steps[0]}" = all ] && steps=(info build t1 t2 t3 t4 pack)
for s in "${steps[@]}"; do
  case $s in
    info|build|t1|t2|t3|t4|pack) $s ;;
    *) echo "不认识的步骤：$s（可用：all info build t1 t2 t3 t4 pack）"; exit 2 ;;
  esac
done

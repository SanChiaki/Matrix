#!/bin/bash
# 在部署镜像里以 matrix 用户运行（run.sh 调用）：
#   0. 直接起 bwrap（dsh 内层沙箱的原样参数、带用户命名空间、外层套内层），分清失败出在哪一层；
#   A. 现状：裸 dsh，Agent 的 bash 去读别人的东西；
#   B. 阶段 0：外层 bwrap 包住 dsh，同一组探针。
# 输出里 KEY=值 的行会被 run.sh 收进 summary.txt。
set -u
D=/data
mkdir -p $D/conversations/c1/a1 $D/conversations/c2/b1 $D/agents/other
echo VAULT-SECRET > $D/vault.key
echo OTHER-PRIVATE > $D/agents/other/notes.md
echo OTHER-SEAT-WORK > $D/conversations/c2/b1/report.md
echo '{}' > $D/state.json
mkdir -p /home/matrix/.dsh && echo DSH-SHARED > /home/matrix/.dsh/marker
cat > $D/conversations/c1/a1/p.sh <<'EOF'
r() { cat "$1" >/dev/null 2>&1 && echo "$2=READABLE" || echo "$2=blocked"; }
r /data/vault.key VAULT
r /data/agents/other/notes.md OTHER_HOME
r /data/conversations/c2/b1/report.md OTHER_SEAT
r /home/matrix/.dsh/marker SHARED_DSH_HOME
r /data/state.json STATE
echo "PROCS=$(ls /proc | grep -c '^[0-9]')"
# 内层沙箱是哪一种：bwrap 时 1 号进程是 bwrap；否则 dsh 退到了别的方式（如 Landlock，只管写）
p1=$(tr '\0' ' ' < /proc/1/cmdline 2>/dev/null | cut -c1-60)
case $p1 in bwrap*) echo "INNER=bwrap" ;; *) echo "INNER=not-bwrap（1 号进程：$p1）" ;; esac
echo "SEE_PARENT_ENV=$(cat /proc/[0-9]*/environ 2>/dev/null | tr '\0' '\n' | grep -c 'sk-probe-secre[t]')"
touch inside.txt 2>/dev/null && echo WRITE_IN=yes || echo WRITE_IN=no
touch /data/escape.txt 2>/dev/null && echo WRITE_OUT=yes || echo WRITE_OUT=no
echo "LS_ROOT=$(ls / | tr '\n' ' ')"
EOF
cd /app

echo "##### 0. 直接起 bwrap"
b() {  # b <名字> <命令...>
  local name=$1; shift
  if err=$("$@" 2>&1); then echo "$name=ok"; else echo "$name=FAIL $(echo "$err" | tail -1 | cut -c1-200)"; fi
}
DSH_ARGS=(--ro-bind / / --dev /dev --unshare-pid --proc /proc --die-with-parent --tmpfs /tmp)
b BWRAP_DSH_ARGS bwrap "${DSH_ARGS[@]}" -- true
b BWRAP_USERNS bwrap --unshare-user "${DSH_ARGS[@]}" -- true
b BWRAP_NESTED python -c '
import os, sys
from pathlib import Path
from matrix import sandbox
inner = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--unshare-pid", "--proc", "/proc", "--die-with-parent",
         "--tmpfs", "/tmp", "--", "true"]
cmd = sandbox.wrap(inner, ro=sandbox.python_dirs(), rw=[Path("/tmp/nested-probe")], network=True)
os.execvp(cmd[0], cmd)'

echo "##### A. 现状：裸 dsh"
python /kit/probe.py dsh $D/conversations/c1/a1 "bash p.sh" file.write shell.exec 2>&1 | sed 's/^/A_/'
echo "##### B. 阶段 0：外层 bwrap 包住 dsh"
MATRIX_UNIT_STATE=$D/units/c1-a1/dsh python /kit/probe.py /kit/outer_dsh $D/conversations/c1/a1 "bash p.sh" \
  file.write shell.exec 2>&1 | sed 's/^/B_/'

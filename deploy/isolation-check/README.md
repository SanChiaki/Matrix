# 云侧执行面隔离验证

回答 [docs/cloud-runtime.md](../../docs/cloud-runtime.md) 第 11 节还没答上的问题。最关键的是第一个：
**dsh 自己的沙箱（bubblewrap）能不能在 gVisor 里运行**，它决定阶段 1 用 gVisor 还是 Kata。

一条命令跑完，结果打成一个压缩包，发回来即可。全程大约 40–60 分钟，下载 2–3 GB。

## 1. 测什么

| 步骤 | 回答的问题 | 要 gVisor | 要模型密钥 |
| --- | --- | --- | --- |
| `info` | 记下机器环境（内核、Docker、seccomp / AppArmor、cgroup、gVisor 版本） | 否 | 否 |
| `build` | 按 `deploy/Dockerfile` 构建部署镜像 | 否 | 否 |
| `t1` | **gVisor 里 dsh 的 bwrap 能否运行**；在默认安全选项、和 compose 现在放开的两项下各测一遍；能运行的话完整跑一遍 `matrix-dsh-check` | 是 | 可选 |
| `t2` | 普通容器（runc）里：seccomp、`/proc` 遮挡各种组合下 bwrap 能否运行；阶段 0 的外层套内层能否运行 | 否 | 可选 |
| `t3` | 重活（2 万个小文件、512 MB 大文件、git clone、pip、npm、uv sync、pytest）在 runc 与 gVisor 下能否跑通、各要多久、占多少内存 | 对比用 | 否 |
| `t4` | 一轮 Agent 运行的内存、容器冷启动时间、10 个空闲沙箱的内存开销，runc 与 gVisor 对比 | 对比用 | 可选 |
| `pack` | 把结果打成 `isolation-results-<主机>-<时间>.tgz` | 否 | 否 |

没有模型密钥时，不调真模型的部分照跑（Agent 的 bash 由一个本地假模型服务驱动），真模型那几项（回复、续接、委托、审批、提问等）跳过。
有密钥结论更完整，审批那一项尤其需要。

## 2. 机器要求

- **不要用生产机**：要装 gVisor、改 Docker 配置、重启 Docker。
- Linux x86_64，发行版尽量和以后部署的一样。首选 **Ubuntu 22.04 / 24.04 或 Debian 12**，并保持默认的 seccomp 与 AppArmor 开着，
  这些正是要测的；不要在 Docker Desktop、WSL 或已经关掉 seccomp 的机器上测。
- Docker Engine 24 或以上，最好是 cgroup v2。
- 4 核、8 GB 内存以上，磁盘空闲 20 GB 以上。不需要 KVM（gVisor 默认不用它）。
- 能访问：Docker Hub、`deb.debian.org`、`deb.nodesource.com`、`registry.npmjs.org`、`pypi.org` 与 `files.pythonhosted.org`、`github.com`、
  装 gVisor 用的 `gvisor.dev` 与 `storage.googleapis.com`；有密钥时还要 `api.deepseek.com`。
  只有内网镜像的机器见第 6 节。
- 跑脚本的用户能用 `docker`（在 docker 组里，或用 `sudo -E`）。

## 3. 准备

拿到这个分支：

```bash
git clone -b claude/beautiful-hamilton-akc84f https://github.com/SanChiaki/Matrix.git
```

装 gVisor（Debian / Ubuntu，官方 apt 源；其他发行版见 <https://gvisor.dev/docs/user_guide/install/>）：

```bash
curl -fsSL https://gvisor.dev/archive.key | sudo gpg --dearmor -o /usr/share/keyrings/gvisor-archive-keyring.gpg
```

```bash
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main" | sudo tee /etc/apt/sources.list.d/gvisor.list
```

```bash
sudo apt-get update && sudo apt-get install -y runsc
```

注册为 Docker 运行时（写 `/etc/docker/daemon.json`）并重启 Docker：

```bash
sudo runsc install && sudo systemctl restart docker
```

确认装好了，应该看到 `runsc` 和 gVisor 的启动信息：

```bash
docker info --format '{{json .Runtimes}}' | grep -o runsc && docker run --rm --runtime=runsc alpine dmesg | head -3
```

## 4. 跑

```bash
cd Matrix/deploy/isolation-check
```

有 DeepSeek 密钥就先设上（只按名字传进容器，不会写进任何日志或结果）：

```bash
export DEEPSEEK_API_KEY=sk-...
```

```bash
./run.sh all
```

屏幕上和 `results/summary.txt` 里会逐步出现结果，每步的完整日志在 `results/` 下。中途断了可以只跑剩下的步骤，例如 `./run.sh t3 t4 pack`。

## 5. 发回什么

最后一行会打出压缩包的路径（`isolation-results-<主机>-<时间>.tgz`），把它发回来即可。里面是 `summary.txt` 和各步日志：
没有密钥，没有你机器上的文件，只有 `info` 一步记下的发行版、内核、Docker 版本与配置。发之前可以先看一眼 `summary.txt`。

## 6. 出问题时

| 现象 | 处理 |
| --- | --- |
| `build=FAIL`，日志里有 `429 Too Many Requests` | Docker Hub 限流，等几分钟再 `./run.sh build t1 t2 t3 t4 pack` |
| 只有内网镜像、或要经代理出网 | 构建参数放进 `BUILD_ARGS`，写法见 [deploy/README.md](../README.md)「内网 / 无公网构建」「内网 CA」。例：`export BUILD_ARGS="--build-arg APT_MIRROR=https://mirror.corp/ --build-arg PYPI_INDEX=https://pypi.corp/simple"`。容器要经宿主上的代理出网时再加 `export DOCKER_NETWORK=host`，并在环境里设好 `HTTPS_PROXY` / `NO_PROXY`（脚本会透传） |
| `T1=SKIPPED` | Docker 没注册 runsc，回到第 3 节最后一步确认 |
| 真模型的某一项偶尔 ✗（比如 `approval` 报“申请批准了，但文件没写出来”） | 模型偶尔不按要求做。单项重跑 2–3 次再下结论，例如 runc 下：`docker run --rm --user matrix --cap-drop ALL --security-opt seccomp=unconfined --security-opt systempaths=unconfined -e DEEPSEEK_API_KEY matrix:isolation-check matrix-dsh-check --live --only approval`（gVisor 下加 `--runtime=runsc`） |
| 真模型的项都报 `TRANSPORT` | 容器连不上 `api.deepseek.com`，先查网络 / 代理 |
| `t3` 某步 `FAIL` | 先看两个运行时是不是都失败：都失败多半是网络，只在 runsc 下失败才是兼容性问题（这正是要找的） |

## 7. 自己先看结果

`summary.txt` 里每行前面的 `[t1-default-probe]` 之类是“步骤-运行时或安全选项-内容”。

**T1（最关键）**，看 `[t1-default-probe]` 与 `[t1-compose-probe]`：

| 看到的 | 含义 |
| --- | --- |
| `BWRAP_DSH_ARGS=ok`、`A_INNER=bwrap` | dsh 的 bwrap 能在 gVisor 里跑。`default` 组就能跑最好（不用放开任何安全选项） |
| `BWRAP_*=FAIL`，但 `A_ok=True`、`A_INNER=not-bwrap` | bwrap 起不来，dsh 退到了别的沙箱方式。要看 `[t1-…-dshcheck]` 的 `approval`：报“没申请升权就写到了工作目录以外”说明这时 dsh 的写限制不完整 |
| `A_ok=False` | Agent 的命令根本跑不起来 |
| `[t1-…-dshcheck]` 全部 ✓ | dsh 的全部功能在 gVisor 里正常（`key-hidden`、`approval` 两项最重要） |

**T2**：预期只有 `compose` 组的 `BWRAP_*` 是 ok（和在另一台机器上的结果一致，见第 8 节）。如果 `default` 组也 ok，说明这台机器的 Docker 默认配置就允许 bwrap，记下来。

**T3**：对比 `[t3-runc-heavy]` 与 `[t3-runsc-heavy]` 每一步的秒数和 `HOST_PEAK_MEM`。只在 runsc 下 `FAIL` 的步骤是兼容性问题；
耗时比值用来估 gVisor 的 I/O 开销（小文件、npm 最敏感）。

**T4**：`[t4-<运行时>-round] HOST_PEAK_MEM` 是一轮运行（委托时同时两个 dsh）从宿主看到的内存，包括 gVisor 自己的开销；
`COLD_START_MS` 是 5 次冷启动的毫秒数；`IDLE_10_SANDBOXES host_used_delta` 除以 10 约等于一个空闲沙箱的开销。

## 8. 参考：在另一台机器上的结果（runc，没有 gVisor）

Ubuntu 24.04、4 核 16 GB、Docker 29（seccomp 开、cgroup v1）、没有 KVM，2026-11-11：

- T2：只有 `compose` 组 bwrap 能跑；去掉 `seccomp=unconfined` 报不能建命名空间，去掉 `systempaths=unconfined` 报不能挂 `/proc`。
  bwrap 起不来时 dsh 没有拒绝执行，退到了别的沙箱方式：工作目录外的 `/data` 写不进，但 `matrix-dsh-check` 的 `approval` 报
  “没申请升权就写到了工作目录以外”（目标在 `/tmp` 下）。
- `compose` 组阶段 0 外层套内层：数据目录、别人的私有目录与会话、`vault.key` 都读不到，工作目录照常可写。
- T3（runc）：小文件 1.6 秒、大文件 0.8 秒、git clone 3.1 秒、pip 16.8 秒、npm 10.8 秒、pytest 7.6 秒，宿主看到的峰值 303 MiB。
- T4（runc）：一轮运行宿主看到的峰值 354 MiB，冷启动 250–300 毫秒，10 个空闲容器共 18 MiB。

gVisor 的实测结果（另一台机器，2026-11-11）记在 [docs/cloud-runtime.md](../../docs/cloud-runtime.md) 第 11 节“gVisor 实测”。

## 9. 测完恢复机器

```bash
sudo runsc uninstall && sudo systemctl restart docker
```

```bash
docker rmi matrix:isolation-check && sudo apt-get remove -y runsc
```

## 10. 文件

| 文件 | 作用 |
| --- | --- |
| `run.sh` | 入口，按步骤跑并汇总 |
| `incontainer.sh` | 容器里的探针：直接起 bwrap、裸 dsh（A）与阶段 0 外层包住的 dsh（B）去读别人的东西 |
| `probe.py` | 用假模型服务驱动 dsh 里 Agent 的 bash，执行指定命令 |
| `outer_dsh` | 阶段 0 原型：在 dsh 外面再包一层 bwrap（复用 `matrix/sandbox.py`） |
| `heavy.py` | 重活 |
| `memsample.py` | 容器里按进程类别采样常驻内存 |

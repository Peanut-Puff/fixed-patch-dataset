# AEO / Pier 数据集接入

本仓库的五题支持 AEO 通用 `pier` 适配器，不需要修改 AEO 或注册专用插件。
兼容基准是原始 Pier **0.3.1**，提交
`df89f994623a0a6a57229103b6fe910766693c30`。
本次交付只准备仓库和本地检查；尚未推送、在线导入、构建 AEO 缓存或发布数据集。

## 运行语义与文件

每题保留 `task.json` 指向的四类权威输入：`prompt.txt`、`candidate.patch`、
`candidate-files.json`、`controls/historical-final.patch`，以及原有隐藏验收文件。
`pier.metadata` 保存原始任务元信息。`sync-pier` 从这些输入确定性生成：

```text
tasks/<id>/
  task.toml                         # 无 environment.docker_image；从 Dockerfile 构建
  instruction.md                    # 与 prompt.txt 字节一致
  pre_artifacts.sh                   # 与 verifier.collect 同为 BASE→HEAD
  environment/
    Dockerfile                      # digest 基础镜像 + C0 初始化
    .dockerignore                   # 构建输入白名单
    candidate.patch
    candidate-files.json
    state.json                      # baseline、patch 指纹和预期 C0 树
    candidate_state.py              # 来自 bfoc/candidate_state.py
  tests/
    Dockerfile                      # 干净 digest 基础镜像 + 隐藏 tests
    test.sh / test.patch / grader.py / config.json
  solution/
    solve.sh
    state.json
    candidate-files.json
    historical-final.patch
    candidate_state.py
```

Agent 构建检查原 baseline、工作区干净状态、C0 patch 指纹、文件前后哈希和完整文件树，
保留原 HEAD、未提交且未暂存的 C0。不会在镜像中放入 tests 或 solution。
不能把 `task.json.image` 的干净基础镜像直接填回 `environment.docker_image`，否则 Pier
会跳过 C0 构建。AEO 构建并冻结的派生缓存镜像可以通过平台既有机制使用。

Oracle 由 Pier 原生 `oracle` 执行，只在 Oracle 容器中上传 `solution/`。
它检查加载的 C0、恢复原 baseline、仅删除候选新建文件，然后应用历史正对照并提交。
它不会执行全局 `git clean -fdx`，避免删除 `node_modules` 等离线依赖。
正常 Agent 和 Oracle 都收集 `git diff --binary BASE HEAD`；未提交内容不计入交付。
独立 verifier 从干净 baseline 应用完整交付 patch，原四个验收文件保持字节不变。

两个执行入口保持独立：原 `experiment.py run` 继续从干净镜像自行加载 C0；Pier 直接使用
上述标准目录。资源为 2 CPU / 8192 MiB / 20480 MiB、无 GPU；题面时限为 Agent 10800 秒、
verifier 1800 秒。任务网络保持 `no-network`，模型访问由 AEO Binding 的网关机制提供。

## 修改与本地检查

修改权威输入或公共初始化逻辑后，重生成受影响任务，检查 diff，再显式封存：

```bash
python3 experiment.py tasks sync-pier tasks/expr-try-catch-errors
python3 experiment.py tasks seal tasks/expr-try-catch-errors
python3 experiment.py tasks check
python3 -m unittest discover -s tests -v
```

公共生成规则变化时，对五题分别执行 sync 和 seal。sync 不更新 checksum；seal 会拒绝
未同步的派生内容。生成目录禁止软链接和额外文件。原来没有 `pier` 字段的实验任务仍可运行。

`scripts/check_aeo_compat.py` 使用真实 Pier 解析器和 AEO `FilesystemDatasetAdapter`，
不启动 Controller、Worker、Docker 或模型请求。需要 AEO 开发环境已安装依赖，以及固定 Pier
源码的解析依赖；示例在本仓库根目录执行：

```bash
# 把 /path/to/aeo 换成 AEO checkout；依赖安装在本仓库忽略目录，不修改 AEO。
uv pip install --python /path/to/aeo/backend/.venv/bin/python \
  --target .local/compat-deps --no-deps \
  'datacurve-pier @ https://github.com/datacurve-ai/pier/archive/df89f994623a0a6a57229103b6fe910766693c30.tar.gz' \
  'toml==0.10.2' 'shortuuid==1.0.13'
PYTHONPATH=.local/compat-deps /path/to/aeo/backend/.venv/bin/python scripts/check_aeo_compat.py
```

检查包括五题解析、资源/网络/工作目录、原生 Oracle 路径、验收命令配置、AEO 两次物化的
manifest 一致性，以及 solution 在源码归档中的完整性。AEO 普通 Case 文件清单排除 solution；
Oracle 通过完整源码归档获取它。脚本按 Git 跟踪文件和未忽略新文件快照当前工作区，明确输出
`working_tree_snapshot=true`；输出的父 commit 不代表未提交内容已发布。

## 接入前的 Linux amd64 无模型验收

这一步需要原生 Linux amd64 Docker 主机、Docker Compose/Buildx、至少 3 个逻辑 CPU、
约 10 GiB 可用内存和 40 GiB 空闲磁盘。默认仅 1 并发。
安装并使用上述固定提交的完整 Pier CLI，例如在专用主机安装到 `.local/pier-runtime`：

```bash
uv venv --python 3.12 .local/pier-runtime
uv pip install --python .local/pier-runtime/bin/python \
  'datacurve-pier @ https://github.com/datacurve-ai/pier/archive/df89f994623a0a6a57229103b6fe910766693c30.tar.gz'
python3 scripts/verify_pier_dataset.py \
  --pier .local/pier-runtime/bin/pier \
  --output results/aeo-validation-01
```

此命令会拉取固定基础镜像、创建一次性容器，按每题 `task.json.expectations` 顺序验证三个对照：
五题 baseline=0、历史正对照=1；Scriggo 的 C0=1，其余四题 C0=0。
再构建 C0/独立 verifier 镜像并运行五题原生 Oracle，要求全部 reward=1 且没有执行异常。
不使用 BF/OC 二进制或模型凭证，不调用 AEO API。现有输出目录拒绝覆盖，失败保留日志和现场，
不自动重跑。结果位于 `summary.json`、`verification/`、`pier.log` 和 `pier/oracle/`；
只有完整终态、五题齐全且 Oracle 脚本成功才报告 passed。构建镜像保留供检查。

Pier 0.3.1 内部有两个 verifier 超时 attempt，`retry.max_retries=0` 不能关闭它。
验收配置沿用 AEO 的 `verifier_timeout_multiplier=0.5`，限制整个 verifier 阶段的总预算；
每次 attempt 最多 900 秒。这里的自动重复仅指固定 Pier 原生超时行为。

## 后续 AEO 导入

在完成 amd64 验收，并将审核后的内容提交、推送后：

1. 使用公开源 `https://github.com/Peanut-Puff/fixed-patch-dataset`，ref 填完整 commit SHA。
2. AEO 管理页选择当前 active 的通用 `pier` 适配器；自动发现 `tasks/`，应得到五个原 Case ID。
3. 检查 candidate 版本题面为 C0 续修、独立 verifier 配置和资源正确。
4. 按平台流程构建缓存，执行全部五题 Oracle 门禁，成功后正式发布并显式设为默认版本。

数据集 slug 为 `fixed-patch-dataset`，稳定 profile 为 `pier`。不需要新增 AEO Adapter、
Engine、数据库迁移或硬编码分支。BF/OC 真实模型评测在数据集正式接入后另行发起。

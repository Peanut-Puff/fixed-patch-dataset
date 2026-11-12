# BF / OC 实验仓库

在相同候选代码 C0 上运行 BitFun、OpenCode 或双方，隔离容器执行并用独立 verifier 验收。
仓库自带五个任务的全部文本输入、C0、历史最终正对照、隐藏测试、镜像定义、运行/统计代码。
不依赖旧实验目录，不包含 BF/OC 二进制、模型凭证或历史运行结果。

## 环境

- 原生 Linux x86_64、Docker、Python 3.10+；Python 代码仅用标准库，不需要 pip/npm 安装。
- BF/OC Linux x86_64 原生 ELF 文件；每次运行单独传路径，可更换版本。
- 任务镜像按 digest 固定。首次下载需要网络/镜像仓库访问；镜像本身不装进 Git。
- 模型服务支持 OpenAI `/v1/chat/completions`。默认 DeepSeek-V4.1-Flash-1017、high、300000 上下文、65536 输出上限。

默认每个 Agent 2 CPU / 8 GiB，最多 3 并发，主机需至少约 26 GiB 可用内存、40 GiB 空闲磁盘。
可用 `--concurrency 1` 降低需求。每次最长 10800 秒，验收最长 1800 秒。

## 开始运行

先检查任务并获取固定镜像：

```bash
python3 experiment.py tasks check
python3 experiment.py images --pull
```

凭证由运行时环境变量提供，也可用未入库的私有 JSON：

```bash
export MODEL_BASE_URL='http://你的模型服务:8080/v1'
read -rsp 'Model API key: ' MODEL_API_KEY; echo
export MODEL_API_KEY
```

只运行 BF（每次都可换二进制、任务、结果目录）：

```bash
python3 experiment.py run --agents BF \
  --bf-binary /root/bin/bitfun-v1 \
  --output results/bf-v1-r1 --background
```

只运行 OC：

```bash
python3 experiment.py run --agents OC \
  --oc-binary /root/bin/opencode \
  --output results/oc-r1 --background
```

运行双方并指定任务子集：

```bash
python3 experiment.py run --agents BF OC \
  --bf-binary /root/bin/bitfun-v2 --oc-binary /root/bin/opencode \
  --tasks koota-pair-relation-tracking expr-try-catch-errors \
  --output results/paired-r1 --background
```

不传 `--tasks` 时运行 `configs/default-suite.json` 中的五项；也可传 `--suite 自定义.json`。
`--output` 支持相对当前目录的路径或绝对路径；已有目录拒绝覆盖。
多批建议先后执行，三并发指单批内部上限。

先预览计划，可加 `--dry-run`；该操作不连接 Docker/模型、不创建结果目录，也不要求模型凭证。
私有配置可用 `--upstream /root/private/upstream.json`，结构见 `configs/upstream.example.json`。
`--model configs/model.json` 可替换模型合同；配置变化会在 plan 中留存。

## 实验流程与结果

1. 冻结所选二进制、任务输入与 runner 源码到结果目录，记录 SHA-256；后续替换原文件不会改变本次运行。
2. 每种任务镜像/Agent 做离线 mock 检查，记录真实版本和 CLI 请求字段，无真实模型调用。
3. 新建独立 verifier 容器，复核 baseline、历史最终正对照和 C0。
4. 为所有选定 Agent 创建全新容器/会话，应用同一 C0，核对初始树；全部匹配后才启动真实模型任务。
5. 最多指定并发数运行。任务容器仅连内部网络，只有模型代理能访问上游；隐藏 tests、正对照不挂给 Agent。
6. 所有 Agent 结束后，独立验收本次已提交 BASE→HEAD patch，生成 summary 和 metrics。

运行轨迹和结果：

```text
results/你的批次/
  plan.json                   # 任务/Agent/版本来源/模型/资源
  binaries/BF|OC/             # 本次可执行文件快照、哈希
  inputs/                     # 本次任务输入快照；包含隐藏测试，协调器保存
  harness/                    # 本次 runner 源码快照
  preflight/                  # 离线合同和版本记录
  agents/<task>-BF|OC/
    artifacts/stdout.jsonl    # 原生轨迹
    artifacts/stderr.log
    artifacts/delivery.patch # 已提交交付
    artifacts/working-tree.patch
    artifacts/               # 原生会话、数据库、状态、未跟踪文件
  model-ledger/               # 请求/响应/usage（含拒绝和错误）
  verification/               # 对照和最终官方验收
  status.json                 # complete 或 stopped 等阶段
  controller.log             # --background 时的控制日志
  summary.json
  metrics.csv
  metrics.json
```

查看进度：`tail -f results/bf-v1-r1/controller.log`。退出 tail/断开 SSH 不终止后台控制进程。
中断后不自动继续或重跑，保留现场，下一次使用新结果目录。
事后重算统计：`python3 experiment.py summarize results/bf-v1-r1`，无模型请求。

CLI 退出码、原生会话完成和独立验收分开。OC 已知可能在成功提交后因 Web Search 收尾退出 1，不能单凭退出码判失败。
共同 HTTP prompt 包含缓存、completion 包含 reasoning；usage 缺失保留 partial/missing，不补零，不用于完整 Token 对比。
代理记录已认证拒绝尝试，不修改请求体、不添加模型 fallback 或重试。
结果包含原始代码、模型文本和临时 scoped 凭证，均被 Git 忽略；分享前按需脱敏。

## 扩展任务集

每个任务位于 `tasks/<id>/`，有独立 `task.json` 和 `checksums.json`，调度器动态发现。
可添加任务目录，再更新 suite 或启动时用 `--tasks` 选它，无需改 Python 调度代码。
完整步骤和字段见 [扩展任务](docs/tasks.md)。

## 开发与验证

```bash
python3 -m unittest discover -s tests -v
python3 experiment.py tasks check
```

本仓库沿用已有 BF 0.2.19 / OC 2.2.6 CLI 合同；新二进制先过离线检查再跑实验。
内置任务来自冻结数据集 revision `802975a295b64bdc9c17316081a493ba61bc906b`，C0 为 1107 BF 历史截断候选。
Koota pair 正对照包含此前已验证的 README unchanged context 修复，原始来源与哈希见各任务 `provenance.json`。
历史结果没有导入仓库。跨日期、模型服务波动、程序版本或参数变化需在比较时单列。

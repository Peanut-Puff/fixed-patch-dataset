# 扩展任务

任务是独立目录，不在 Python 代码里硬编码。已有任务只读可运行；修改后必须显式重新 seal，防止无意变化。

```text
tasks/my-case/
  task.json
  prompt.txt
  candidate.patch
  candidate-files.json
  controls/historical-final.patch
  tests/test.sh
  tests/test.patch
  tests/grader.py
  tests/config.json
  environment/Dockerfile       # 可选，建议保留镜像来源
  provenance.json             # 可选，建议保留输入来源
  checksums.json              # seal 自动生成
```

`task.json` 示例：

```json
{
  "schema_version": 1,
  "id": "my-case",
  "description": "任务说明",
  "image": "registry/image@sha256:64位镜像digest",
  "base_commit": "40位完整Git提交",
  "expected_initial_tree_sha256": "64位C0文件树哈希",
  "prompt": "prompt.txt",
  "candidate_patch": "candidate.patch",
  "candidate_files": "candidate-files.json",
  "tests": "tests",
  "positive_control": "controls/historical-final.patch",
  "expectations": {
    "baseline_reward": 0,
    "positive_reward": 1,
    "candidate_reward": 0
  },
  "verifier_command": ["bash", "-lc", "export PATH=\"/root/go/bin:$PATH\"; bash /tests/test.sh"]
}
```

要求原生 Linux amd64 Docker 镜像，干净仓库位于 `/app` 且 HEAD 为 `base_commit`，内置 Python 3、Git、bash、timeout、tar 和离线可用的项目依赖。
`/tests` 只挂给 verifier；其命令从 `/logs/artifacts/model.patch` 读取交付，将结构化结果写 `/logs/verifier/reward.json`，至少包含数值 `reward`。
固定 digest 避免同一标签漂移。可自行构建镜像并推送，或者使用已有可访问的 digest。此仓库不内置 Docker 镜像层。

准备新任务：

1. 创建上述目录、题面、C0 patch、有效正对照和 tests。`id` 必须等于目录名，只用小写字母、数字、连字符。
2. `candidate-files.json` 可先写 `{"files":[]}`，`expected_initial_tree_sha256` 暂填空字符串。
3. 在具备镜像的 Linux Docker 机器运行：

```bash
python3 experiment.py tasks capture tasks/my-case
```

capture 无模型请求：在无网络的一次性镜像里校验 baseline、应用 C0，计算变更文件的 pre/post SHA-256 和完整 C0 树。
支持文件新增、修改、删除；不接受候选中修改的 symlink。结果回写任务 manifest 和 task.json，并 seal。

4. 检查任务输入：

```bash
python3 experiment.py tasks check
```

5. 使用 `--tasks my-case` 运行，或将其加入自定义 suite：

```json
{"schema_version":1,"name":"my-suite","tasks":["my-case","expr-try-catch-errors"]}
```

从仓库外导入已经准备的目录可用 `python3 experiment.py tasks import /path/to/my-case`；不会覆盖同名任务。
仅供实验运行器使用的任务，刻意更改输入后用 `python3 experiment.py tasks seal tasks/my-case` 更新哈希。
启用 Pier 的任务（`task.json` 含 `pier.schema_version = 1`）必须先同步派生输入再 seal：

```bash
python3 experiment.py tasks sync-pier tasks/my-case
python3 experiment.py tasks seal tasks/my-case
python3 experiment.py tasks check
```

`sync-pier` 也可将新任务接入 Pier，要求隐藏测试目录名为 `tests`。
`task.json` 中的 `pier.metadata` 保存语言、类别、原始任务来源等展示信息；`task.toml` 是生成物。
权威输入仍为 `task.json` 引用的题面、候选 patch、文件清单和正对照。
不要手改 `instruction.md`、`environment/`、`solution/` 或生成的 Dockerfile；生成规则和完整布局见
[AEO 接入说明](aeo.md)。`tasks check` 和 `seal` 均拒绝派生副本漂移或构建上下文中的额外文件。
`capture` 为已启用 Pier 的任务重新计算 C0 后，会先同步派生输入再封存。

seal 只冻结事实，不证明正负对照可复现；正式运行仍会检验三个对照，失败则停止，不启动 Agent。

任务集、suite、模型配置和资源值均在结果 plan 中保存；可执行文件按当次选择快照。
仓库不替 agent 提交代码，只验收已提交 patch，工作区诊断单独保留。

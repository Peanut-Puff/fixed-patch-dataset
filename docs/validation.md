# 验证范围

本地自动测试覆盖 BF only、OC only、BF+OC 调度与并发、动态任务发现/选取、输入哈希与路径边界、
每次二进制/任务/源码快照、旧结果保护、C0 不匹配禁止启动、Agent 容器不挂隐藏测试、原生结果与退出码分离、
真实子进程的 BF/OC 离线 mock、模型代理原样转发/拒绝/usage 账本，以及缺失 usage 的 partial 统计。
Git 临时仓库验证 C0 应用的 preimage/postimage 与完整文件树。
还覆盖后台真实子进程的锁交接和源码快照、未完成上游请求的开始记录及缺失用量计数。

当前本机 Docker 为 Linux ARM64，故未在此运行真实 amd64 BF/OC 和全量官方 verifier。
正式执行要求原生 Linux x86_64 Docker 主机；运行器会在模型请求前逐镜像做实际二进制离线检查及三个质量对照。
本次仓库整理没有发起真实模型请求、拉取任务镜像或重跑原实验。

2026-11-12 本地验证：21 项标准库 unittest 全部通过，五个 task checksum 检查通过。
迁入的五项官方 test.sh/test.patch/grader.py/config.json 与冻结实验规格完全一致，C0 patch 指纹一致。
扫描全部源文件，无实验中已有的模型凭证值；仓库只包含文本源文件，约 6 MB。

CLI 合同支持当前 BF `exec ... --output-format stream-json`、OC `run --standalone --format json --thinking`。
二进制版本变化由离线检查发现。配置 schema 若有不兼容变化，更新 `bfoc/adapters.py` 并重新检查；不静默用旧二进制替代。

## 2026-11-13：AEO / Pier 数据准备

本次检查覆盖五题生成的 Pier 输入和现有 BF/OC 实验入口：

- `python3 -m unittest discover -s tests -v`：32 项通过。新增覆盖同步幂等与派生漂移、
  构建上下文白名单、旧任务兼容、C0 新增/修改/删除和文件权限、错误 baseline/哈希/完整树、
  仅验收已提交 patch、Oracle 恢复正对照并保留离线依赖，以及缺失/异常结果不能报告成功。
- `python3 experiment.py tasks check`：五题 checksum 与派生输入一致性全部通过。
- 使用固定提交 `df89f994623a0a6a57229103b6fe910766693c30` 构建的原生 Pier 0.3.1
  解析器和本地 AEO `FilesystemDatasetAdapter` 执行 `scripts/check_aeo_compat.py`：
  五题解析、资源、网络、工作目录、Oracle JobConfig、两次物化 manifest 一致性及源码归档
  中 solution 完整性通过。依赖仅补在本仓库忽略的 `.local/compat-deps`，未修改 AEO。
- 相对原始 Git HEAD，五题 `prompt.txt`、C0 patch/文件清单、历史正对照、provenance 及
  `test.sh/test.patch/grader.py/config.json` 均保持字节不变。
- `bash -n` 检查生成 shell 与测试入口通过，`git diff --check` 通过。

上述是本地格式和行为回归结果，不是实际任务评分。本机 Docker 为 Linux ARM64，未构建任务镜像、
执行真实五题 verifier/Oracle、请求模型或操作线上 AEO。原生 Linux amd64 验收命令及后续接入流程见
[AEO 接入说明](aeo.md)；需在接入前按每题 `task.json.expectations` 验收：五题 baseline=0、
历史正对照=1、Pier Oracle=1；Scriggo 的 C0=1，其余四题 C0=0。

后续补查修正验收脚本缺失的批次字段，并增加完整命令编排回归（模拟 Docker/Pier 边界，
实际执行状态记录、十五个对照的预期检查和五个 Oracle 结果汇总）。验收汇总按题记录
expectations，不再错误地把全部 C0 描述为 0；原任务配置及评分规则保持不变。

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

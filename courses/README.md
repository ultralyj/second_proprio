# 模仿学习与强化学习课程

面向已有 IL/RL 基础的你，用灵巧操作问题重新连接直觉、公式与最简代码。初版交付前三节；此后每天北京时间 08:00 最多生成一小节。首次三节是初始化样例，不代表已经学完，也不影响之后按序推进。

## 课程目录

| 节 | 主题 | 内容状态 |
|---|---|---|
| 01 | [统一建模：策略如何改变未来数据](lessons/01-shared-model.md) | 已生成 |
| 02 | [行为克隆：低监督误差为何不等于可靠控制](lessons/02-bc-shift.md) | 已生成 |
| 03 | [DAgger：让专家标注学习者真正遇到的状态](lessons/03-dagger.md) | 已生成 |
| 04 | [回报、价值与 Bellman 递推](lessons/04-returns-values.md) | 已生成 |

本索引只展示已生成章节；完整课程计划保存在机器可读目录 [syllabus.json](syllabus.json)。后续研究进展在写对应章节时检索原论文，注明资料截止日期，不把尚未核验的内容写成“最新结论”。

## 怎样与课程互动

运行课程任务后，每个已生成章节会有自己的 `private/notes/<lesson-id>.md`。把问题答案、自己的解释和卡点写进去；读完后可手动勾选“已学完本节”。文件留在本机，不进入 Git。技能会保留你的文字，在下次定时运行或手动调用时，将反馈写入 `private/feedback/<lesson-id>.md`。

反馈先处理具体误解，再用一个问题检查理解；下一节会结合你的卡点调整热身例子。没有笔记时仍按顺序日更，但不推断你已经掌握。公共目录只记录内容是否生成，个人学习状态留在私有文件里。

Mac 离线时无法在本机生成内容；恢复后执行下一次任务，只接着生成一节，不把离线天数变成必须补齐的课程积压。当天重复运行主要处理新增反馈，不重复生成新节。

## 运行示例

在仓库根目录运行：

```bash
uv sync --extra learning
uv run python courses/examples/shared_model.py
uv run python courses/examples/bc_shift.py
uv run python courses/examples/dagger.py
uv run --extra learning python courses/examples/returns_values.py
```

示例使用 CPU 和固定随机种子，不下载数据集。它们用于理解核心组件；维度与动力学刻意简化，数值结果不能作为真实灵巧手性能结论。Python 代码按 Google 风格编写，注释和文档说明使用中文。

## 内容维护记录

- 2026-10-29：建立学习路线和前三节；来源核验包含 Ross 与 Bagnell（2010）、Ross 等（2011）、Ho 与 Ermon（2016）。
- 2026-10-30：生成第 04 节《回报、价值与 Bellman 递推》；CPU 示例已运行并用线性方程解核对，来源核验包含 OpenAI Spinning Up 官方文档与源码、Gymnasium 官方文档。

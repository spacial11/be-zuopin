# Tidal Harness · Hope of Light

可验证的**多 Agent 编排**原型：裁决标准不是"答得像"，而是**两道门都通过**。

> 这是参赛版的严格/裁剪实现：只处理受限 Python 纯函数任务，不接 QQ、不接任何外部机器人框架，
> 所有交互都在浏览器面板里完成。**零第三方依赖**，只用 Python 标准库。

---

## 一分钟跑起来

```bash
# 1) 命令行演示：第 1 轮测试挂 → 第 2 轮审查驳回 → 第 3 轮双门通过
python -m tidal_harness demo

# 2) 打开面板（对话 + 主动插话 + 任务 + 阶段事件）
python -m tidal_harness serve --demo
#   浏览器打开 http://127.0.0.1:8765
```

面板上做三件事：

- **左边**：填任务描述、函数名、测试用例 → 提交 → 看阶段事件（queued / plan / do / check-static / check-tests / check-review / act）
- **右边**：直接和助手对话（问状态、问为什么被驳回）
- **主动插话**：任务每走到关键一步，它会自己往对话里说一句（"测试没过，我把用例塞回去了"），可开关

---

## 五个模块对应到哪

| 模块 | 代码 | 可观察行为 |
|---|---|---|
| Agent 定义与配置 | `contract.py` AgentRegistry | 角色、能力、指令；注册即校验 |
| 工作流编排 | `contract.py` Planner | 只允许合法角色与检查顺序 |
| 任务调度与执行 | `engine.py` TaskScheduler | 有界并发、优先级、取消 |
| 结果验证与监控 | `engine.py` validate_static / run_tests / Reviewer | 受限 AST + 真跑测试 + 独立审查；事件落 SQLite |
| 反馈循环与优化 | `engine.py` Engine | 最多三轮，审查意见 + 失败用例回灌 |

---

## 目录

```
tidal_harness/
  contract.py   任务契约（排队前校验、执行中冻结、缓存隔离用它的摘要）
  runtime.py    上下文窗口 / 候选缓存 / SQLite / 模型适配（Demo & OpenAI 兼容）
  engine.py     受限 AST + 子进程跑测试 + Reviewer + PDCA 循环 + 调度器
  web.py        浏览器面板（对话、主动插话、任务、事件流，SSE 推送）
  __main__.py   CLI：demo / run / serve
data/           tidal.db（任务、事件、对话）+ 运行日志
```

---

## 两道门是什么

**第一道：确定性验证**（不依赖模型）

- 函数名必须存在，AST 只能是白名单节点；
- 不许 import、不许 exec/eval、不许碰文件和网络；
- 节点数、常量大小有上限；
- 用户给定的测试**真跑**（独立子进程 + 超时 + 受限 builtins）。

**第二道：独立审查**（不同角色、不同提示词）

- 审查者是 `reviewer-b`，与 `coder-a` 不是同一个角色；
- 只输出 `{"approve": bool, "issues": [...]}`；
- **测试全过也可以被驳回**——比如"任务要求支持小数，实现没有显式处理类型分支"。

任一道不过，意见和失败用例一起回灌，最多三轮；两道都过才写缓存。

---

## 演示脚本

`--script fail-first`（默认）：第 1 轮把加法写成减法 → 测试挂；第 2 轮改对但审查挑出任务遗漏 → 驳回；第 3 轮通过。

`--script clean`：一次通过，用来看"正常路径"。

真实模型模式：

```bash
export TIDAL_MODEL_BASE=https://你的服务地址/v1      # 任意 OpenAI 兼容服务
export TIDAL_MODEL_KEY=你的密钥
export TIDAL_MODEL_NAME=你的模型名
python -m tidal_harness serve        # 不用 --demo 即走真实模型
```

> 模型层是通用的 OpenAI 兼容适配器，**不绑定任何特定服务商**；真实模式只认这三个环境变量，
> 不读取聊天客户端、登录态或机器人历史里的凭据。

---

## 已知边界（不要当成能力）

- **只支持受限 Python 纯函数**。跑仓库级代码、调用外部工具需要另建隔离执行后端，不能靠删掉现有约束。
- 上下文窗口的 token 数是**工程估计**（中文约 1.6 字/token），不是分词器结果。
- 离线压测只验证调度链路，不代表模型质量或费用。
- 单样本运行数据不能推导平均成功率。

---

## 数据在哪

`data/tidal.db`：`tasks`（任务状态）、`events`（每个阶段一条，含轮次与载荷）、`candidates`（缓存）、`chat`（对话与插话）。
进程重启时，未完成的任务会被标成 `interrupted`，不会假装成功。

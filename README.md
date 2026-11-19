# be队作品 · 可验证的多 Agent 编排

第五届“未来杯”创新大赛赛题一，参赛队名为 be。这个原型为小型 Python 纯函数任务串联规划、编码、测试和审查。每轮公开测试、独立审查、候选实现和裁决可在本地面板回放。

## 项目创作分工

AI 提出了项目方向，并编写了整体框架。参赛者马君逸负责调试，并对代码做了少量修改。

## 本机复现

在本目录用 Python 3.12 执行以下命令。Python 项目使用标准库，无需安装额外包。Windows 自带运行时位于提交包的「演示」目录，完整使用步骤见「演示/使用说明.txt」。

```text
python -m tidal_harness demo
python -m tidal_harness serve --demo
```

打开 http://127.0.0.1:8765，选择「可复现离线演示」，再点击「开始执行」。数据默认写入当前目录的 data 下，环境变量 TIDAL_DATA 可指定其他目录。

参赛包还提供 DeepSeek 在线入口。用包内 Windows 启动器或在「演示/demo」下起服务时，后端从该目录读取 deepseek-competition.key。在线生成和审查使用 DeepSeek 官方 API 的 deepseek-flash。自行从源码运行时，密钥文件可放在 tidal_harness 的上一级目录，或在控制台配置自己的兼容接口。真实密钥仅供获准评审的本地参赛包使用。

## 评审可以复核什么

三条公开加法测试为 (1, 2) → 3、(-1, 5) → 4、(0.5, 0.5) → 1.0。它们的正确结果恰好都是整数值。离线模式按固定顺序构造三个候选，独立的审查模块对实际代码追加运行 (0.5, 0.25) 和 (-2, 0.5)。

| 轮次 | 候选返回表达式 | 公开测试 | 两个附加用例的实际结果 | 裁决 |
| --- | --- | --- | --- | --- |
| 1 | a - b | 0/3 | 未进入审查 | 修改运算 |
| 2 | int(a + b) | 3/3 | 0、-1，期望 0.75、-1.5 | 驳回 |
| 3 | a + b | 3/3 | 0.75、-1.5 | 接受 |

脚本化候选和预先选定的反例用于稳定重现流程。公开测试、反例执行、裁决和事件落盘都由程序实际完成。离线样例的能力范围是加法函数。在线模式由模型生成候选和审查意见，用例执行仍在本机完成。模型请求使用所选密钥所属账号的额度。

## 代码入口

| 文件 | 职责 |
| --- | --- |
| tidal_harness/contract.py | 契约、角色注册和规划 |
| tidal_harness/engine.py | 优先级调度、受限语法检查、隔离进程执行、独立审查、修改迭代 |
| tidal_harness/runtime.py | 离线候选生成、模型适配、缓存、上下文预算、SQLite |
| tidal_harness/web.py | 本地控制台、HTTP 接口、事件推送、回放 |
| tidal_harness/inner.py、predict.py | 本轮约束记录与后续操作建议 |
| tidal_harness/__main__.py | 命令行、服务启动 |
| Windows启动器/LauncherWebView2.cs | WinForms 和 WebView2 启动窗口 |

Web 接口 POST /api/tasks 接收 description、function_name 和 tests，支持可选的 priority（0–9，数字越小优先级越高）、max_rounds 和 task_id。默认两个工作线程，最多 64 个任务等待，优先级相同按入队顺序处理。POST 成功返回 202 和任务 ID，GET /api/tasks/{任务ID} 返回终态与逐轮事件。离线面板每次重新运行示例，以保留每一轮可回放的数据。

公开项目仓库为 https://github.com/spacial11/be-zuopin 。随包源码包含本次交付新加的 DeepSeek 评审入口。项目源码使用本目录的 MIT 许可证。演示包内 CPython 与 Windows 运行组件分别沿用各自许可证。

## 历史版本

[Windows 演示包 v1.0.0](dist/ye-zuopin-v1.0.0.zip) 是前期打包版本。本次参赛实录中的小数反例与任务调度可从当前源码或本次参赛压缩包复现。

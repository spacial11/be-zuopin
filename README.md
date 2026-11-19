# be队作品 · 可验证的多 Agent 编排

第五届“未来杯”创新大赛赛题一，参赛队名为 be。输入一项 Python 任务，编码智能体按要求交付脚本或函数，本机执行候选代码，审查智能体对照原始要求、代码和运行结果独立验收。每轮实现、测试、审查和裁决都可以在面板回看。

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

### 自定义脚本与函数

在欢迎页选择「一键 DeepSeek 在线」或「用我的 API Key」。例如输入「写一个 Python 脚本，打印 hello world」，将交付形式设为「脚本」，把期望输出填为 `hello world`，然后开始执行。程序会实际运行候选脚本并对照输出。若只定义了函数而没有打印，实际输出为空，这一轮会退回给编码角色。

处理文本时，可以逐项填写标准输入和期望输出。比如让脚本读取一行文字并转为大写，可填入 `Hello`，期望 `HELLO`。未填写输出用例时，以空输入试运行，审查智能体仍须核对真实输出是否满足任务说明。函数任务则按填写的函数名、参数和期望返回值执行。脚本和函数均在受限 Python 子进程内运行。

### 离线加法课例

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

Web 接口 `POST /api/tasks` 接收任务说明和验收条件。脚本请求可以写成 `{"description":"打印 hello world","task_type":"script","tests":[{"stdin":"","stdout":"hello world"}]}`。只传 `description` 会按脚本试运行。函数请求沿用 `function_name` 和 `tests`。可选的 `priority` 取值为 0–9，数字越小优先级越高，还可设置 `max_rounds` 和 `task_id`。默认两个工作线程，最多 64 个任务等待。成功入队后由 `GET /api/tasks/{任务ID}` 获取结果。CLI 的 `run --file` 接受相同的脚本与函数用例格式，在线运行时通过环境变量 `TIDAL_MODEL_BASE`、`TIDAL_MODEL_KEY`、`TIDAL_MODEL_NAME` 选择接口。

公开项目仓库为 https://github.com/spacial11/be-zuopin 。随包源码包含本次交付新加的 DeepSeek 评审入口。项目源码使用本目录的 MIT 许可证。演示包内 CPython 与 Windows 运行组件分别沿用各自许可证。

## 历史版本

[Windows 演示包 v1.0.0](dist/ye-zuopin-v1.0.0.zip) 是前期打包版本。本次参赛实录中的小数反例与任务调度可从当前源码或本次参赛压缩包复现。

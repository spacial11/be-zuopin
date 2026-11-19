# be队作品 · 可验证的多 Agent 编排

第五届“未来杯”创新大赛赛题一，参赛队名为 be。在同一个输入框里提问、追问、写文稿、整理表格或要求生成程序。在线助手直接回答问题。需要交付 Python 程序时，编码和审查智能体按原始要求协作，本机验证候选代码。可保存的成果完成后，用户自行决定是否写入桌面。

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

### 提问、追问与保存

选择「一键 DeepSeek 在线」或「用我的 API Key」，直接输入需求。例如先问「为什么傍晚的天空常常是红色的？」，再说「讲得适合小学生听」。若输入「给我做一张下周复习计划表，保存成 Excel」，助手会给出结果和保存选项。点「保存到桌面」后才写入文件，重名时保留原文件。也可以选择「暂不保存」。

要生成程序，可以直接说「写一个 Python 脚本，运行时打印 hello world」。编码角色交付代码，本机执行后把代码、测试或试运行输出交给审查角色。通过后可以把实际验收的代码保存为 `.py`。纯粹询问某段代码的含义时，助手直接解释。

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
| tidal_harness/assistant_service.py、artifacts.py | 在线问答与任务交接、生成文档和表格、按用户选择保存到桌面 |
| tidal_harness/inner.py、predict.py | 本轮约束记录与后续操作建议 |
| tidal_harness/__main__.py | 命令行、服务启动 |
| Windows启动器/LauncherWebView2.cs | WinForms 和 WebView2 启动窗口 |

Web 接口 `POST /api/tasks` 接收任务说明和验收条件。脚本请求可以写成 `{"description":"打印 hello world","task_type":"script","tests":[{"stdin":"","stdout":"hello world"}]}`。只传 `description` 会按脚本试运行。函数请求沿用 `function_name` 和 `tests`。可选的 `priority` 取值为 0–9，数字越小优先级越高，还可设置 `max_rounds` 和 `task_id`。默认两个工作线程，最多 64 个任务等待。成功入队后由 `GET /api/tasks/{任务ID}` 获取结果。CLI 的 `run --file` 接受相同的脚本与函数用例格式，在线运行时通过环境变量 `TIDAL_MODEL_BASE`、`TIDAL_MODEL_KEY`、`TIDAL_MODEL_NAME` 选择接口。

公开项目仓库为 https://github.com/spacial11/be-zuopin 。随包源码包含本次交付新加的 DeepSeek 评审入口。项目源码使用本目录的 MIT 许可证。演示包内 CPython 与 Windows 运行组件分别沿用各自许可证。

## 历史版本

[Windows 演示包 v1.0.0](dist/ye-zuopin-v1.0.0.zip) 是前期打包版本。本次参赛实录中的小数反例与任务调度可从当前源码或本次参赛压缩包复现。

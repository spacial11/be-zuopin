"""Interpret a conversational request, answer it or hand a Python contract to the engine."""

from __future__ import annotations

import json
import re

from .contract import ContractError, TaskContract
from .runtime import DemoModel, ModelError, ScriptedDemoModel


_INSTRUCTIONS = """你是 be队作品的中文对话助手。直接帮助用户解决当前问题，可以结合历史理解追问。
根据实际意图回应，不要让用户先选类别。解释、翻译、写作、建议、比较、简单计算、讲解代码和纠错，都可以直接回答。
没有浏览、执行系统命令、查看电脑或保存文件的工具；请勿虚构已执行的操作。用户将自行决定是否保存生成的文件。
只返回一个合法 JSON 对象，形式如下：
1. 通常为 {"kind":"answer","answer":"直接给出具体答案"}。普通问题无需生成附件。
2. 若用户要写一份可以带走的材料，如邮件草稿、清单、Word 报告或 Excel 表格，可以返回
{"kind":"answer","answer":"简短引言或阅读提示","artifact":{"name":"合适的文件名.txt","content":"完整可用的正文"}}。
客户端会将正文显示给用户。若用户指定文件名，请保留。支持 .txt、.md、.csv、.json、.docx、.xlsx；
Word 内容提供纯文本段落，用换行分隔；Excel 和 CSV 内容提供标准 CSV（含列名），JSON 提供可解析的 JSON 文本。
不要返回路径、Base64 或空文件；其他格式若无法直接生成，说明具体情况并提供可读内容。
3. 用户确实需要编写、修改或运行可在本机受限环境验证的 Python 程序时，交给编码与独立审查角色，返回
{"kind":"task","task":{"description":"包含相关上下文的完整需求","task_type":"script","tests":[{"stdin":"","stdout":"期望输出"}]}}。
若任务需读取标准输入，给出一个合理的具体输入例子；如无可确定的输出，可用空 tests，让后续引擎试运行并审查。
只有明确需要按函数名和返回值验收时才提交 function 类型：
{"kind":"task","task":{"description":"完整要求","task_type":"function","function_name":"函数名","tests":[{"args":[1,2],"expected":3}]}}。
请根据这一条需求给用例，示例中的名字和数字不是默认值。有人问“脚本为何如此”“怎么算”“这段代码什么意思”时，
提供实质解释，不发起新编程任务。Python 文件仅能由可运行的任务流程提供；不适合该环境的程序可提供解释、示例或询问必要信息。
输出不要带 Markdown 围栏。"""


def _messages(message, history, requirements):
    if not isinstance(message, str) or not message.strip() or len(message) > 12000:
        raise ContractError("请填写 1 至 12000 个字符的问题。")
    if not isinstance(history, (list, tuple)):
        raise ContractError("对话历史须为消息列表。")
    if not isinstance(requirements, str) or len(requirements) > 6000:
        raise ContractError("补充要求须为不超过 6000 个字符的文本。")
    recent = []
    remaining = 16000
    for item in reversed(history[-20:]):
        if not isinstance(item, dict) or item.get("role") not in ("user", "assistant"):
            raise ContractError("对话历史只接受用户和助手消息。")
        body = item.get("content")
        if not isinstance(body, str):
            raise ContractError("历史消息内容须为文本。")
        if remaining < 120:
            break
        budget = min(8000, remaining)
        if len(body) > budget:
            marker = "\n[此条历史消息中段已省略]\n"
            head = (budget - len(marker)) * 2 // 3
            body = body[:head] + marker + body[-(budget - head - len(marker)):]
        if body.strip():
            recent.append({"role": item["role"], "content": body})
            remaining -= len(body)
    current = message.strip()
    if requirements.strip():
        current += "\n\n用户补充要求：\n" + requirements.strip()
    return [{"role": "system", "content": _INSTRUCTIONS}, *reversed(recent),
            {"role": "user", "content": current}]


def _answer(record):
    answer = record.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        raise ModelError("模型没有返回回答正文，请重试。")
    result = {"kind": "answer", "answer": answer.strip()}
    artifact = record.get("artifact")
    if artifact is None:
        return result
    if not isinstance(artifact, dict):
        result["answer"] += "\n\n这次未生成可保存的附件，可以指定文件格式再试。"
        return result
    name, content = artifact.get("name"), artifact.get("content")
    if not isinstance(content, str) or not content.strip():
        result["answer"] += "\n\n附件缺少正文，可以要求重新生成。"
        return result
    preview = content.strip()
    if len(preview) > 120000:
        preview = preview[:120000] + "\n[预览已截短]"
    if preview not in result["answer"]:
        result["answer"] += "\n\n" + preview
    # Office files are rendered from text by the artifact store, and Python files require engine approval.
    if not isinstance(name, str) or not re.fullmatch(r'[^/\\:*?"<>|\x00-\x1f]{1,108}\.(?:txt|md|csv|json|docx|xlsx)', name, re.IGNORECASE):
        result["answer"] += "\n\n这次附件的文件名或格式不可用。可指定 Word、Excel 或文本等格式重新生成；Python 程序要在编码与审查通过后保存。"
        return result
    if len(content) > 120000:
        result["answer"] += "\n\n这份内容超过单个附件上限，可以要求分成几份保存。"
        return result
    result["artifact"] = {"name": name, "content": content}
    return result


def _task(record, original, requirements):
    payload = record.get("task")
    if not isinstance(payload, dict):
        raise ModelError("模型没有生成完整的编程任务，请重试。")
    kind = payload.get("task_type")
    description, tests = payload.get("description"), payload.get("tests", [])
    if (kind not in ("script", "function") or not isinstance(description, str) or not description.strip()
            or len(description) > 12000 or not isinstance(tests, list) or len(tests) > 32):
        raise ModelError("模型生成的编程任务或验收条件无效，请重试。")
    # Preserve the current user's unmodified instructions as evidence for both agents.
    description = description.strip()
    if original.strip() not in description:
        description = original.strip() + "\n\n任务上下文：" + description
    if requirements.strip() and requirements.strip() not in description:
        description += "\n补充要求：" + requirements.strip()
    task = {"description": description, "task_type": kind, "tests": tests}
    if kind == "function":
        task["function_name"] = payload.get("function_name")
    try:
        TaskContract.from_data(task, task_id="validate")
    except ContractError as exc:
        raise ModelError("模型生成的任务无法验收：" + str(exc)) from None
    return {"kind": "task", "task": task}


def solve_request(model, message, *, history=(), requirements=""):
    """Use one model turn; return a displayable answer or a validated execution contract."""
    messages = _messages(message, history, requirements)
    if model is None or isinstance(model, (DemoModel, ScriptedDemoModel)):
        raise ModelError("自由问答需要在线模型。请选择 DeepSeek 在线或使用自己的 API Key。")
    raw = model.complete(messages, role="assistant", contract=None)
    if not isinstance(raw, str) or len(raw) > 200000:
        raise ModelError("模型返回了无法读取的内容，请重试。")
    raw = raw.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", raw, flags=re.IGNORECASE)
    try:
        record = json.loads(fenced.group(1) if fenced else raw)
    except (ValueError, RecursionError):
        if raw and not raw.startswith(("{", "[", "```")):
            return {"kind": "answer", "answer": raw}
        raise ModelError("模型未返回有效的对话结果，请重试。") from None
    if isinstance(record, dict) and record.get("kind") == "answer":
        return _answer(record)
    if isinstance(record, dict) and record.get("kind") == "task":
        return _task(record, message, requirements)
    raise ModelError("模型返回了无法识别的对话结果，请重试。")

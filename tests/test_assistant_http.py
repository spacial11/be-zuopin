"""Check that opening or reading an answer never saves its attachment."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import build_opener, ProxyHandler, Request

from tidal_harness.__main__ import build_engine
from tidal_harness.artifacts import ArtifactStore
from tidal_harness.assistant_service import solve_request
from tidal_harness.web import ChatBrain, ChatStore, Hub, make_server


class AssistantHttpTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.desktop = root / "test-desktop"
        engine = build_engine("clean", str(root))
        self.addCleanup(engine.store.conn.close)
        chat = ChatStore(str(root / "tidal.db"))
        self.addCleanup(chat.conn.close)
        self.solve = self.enterContext(patch("tidal_harness.assistant_service.solve_request"))
        self.server = make_server(engine, Hub(), chat, ChatBrain(engine), port=0,
                                  artifact_store=ArtifactStore(self.desktop))
        worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.client = build_opener(ProxyHandler({}))
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def api(self, path, data):
        request = Request(self.base + path, data=json.dumps(data).encode(),
                          headers={"Content-Type": "application/json", "Origin": self.base})
        with self.client.open(request, timeout=5) as response:
            return json.load(response)

    def test_conversation_and_explicit_save(self):
        self.api("/api/config", {"base": "http://127.0.0.1:1/v1", "model": "mock", "key": "fixture"})
        self.solve.return_value = {"kind": "answer", "answer": "阳光经过水滴后分成不同颜色。"}
        first = self.api("/api/ask", {"message": "彩虹是怎么形成的？"})
        history = [{"role": "assistant", "content": first["answer"]}]
        self.solve.return_value = {"kind": "answer", "answer": "雨后的彩虹",
                                   "artifact": {"name": "彩虹.txt", "content": "阳光和小水滴。"}}
        second = self.api("/api/ask", {"message": "改写给小学生，做成文本", "history": history})
        self.assertEqual(self.solve.call_args.kwargs["history"], history)
        self.assertNotIn("content", second["artifact"])
        self.assertFalse(self.desktop.exists())
        saved = self.api("/api/artifacts/save", {"id": second["artifact"]["id"]})
        self.assertEqual(Path(saved["path"]).read_text(encoding="utf-8"), "阳光和小水滴。")

    def test_offline_questions_and_unfinished_tasks(self):
        with self.assertRaises(HTTPError) as offline:
            self.api("/api/ask", {"message": "翻译这句话"})
        self.assertEqual(offline.exception.code, 409)
        self.solve.assert_not_called()
        with self.assertRaises(HTTPError) as unsaved:
            self.api("/api/artifacts/save", {"id": "task:missing"})
        self.assertEqual(unsaved.exception.code, 409)
        self.assertFalse(self.desktop.exists())


class ModelReplyTest(unittest.TestCase):
    def test_long_attachment_keeps_topic_and_latest_question(self):
        model = Mock()
        model.complete.return_value = "可以，把第三项改成十分钟。"
        old = [{"role": "user", "content": "请为复习计划列出时间表"},
               {"role": "assistant", "content": "计划开头" + "正文" * 12000 + "计划结尾"}]
        result = solve_request(model, "把第三项改成十分钟", history=old)
        self.assertEqual(result["kind"], "answer")
        messages = model.complete.call_args.args[0]
        self.assertEqual(messages[1], old[0])
        self.assertIn("计划结尾", messages[2]["content"])
        self.assertIn("已省略", messages[2]["content"])

    def test_unavailable_attachment_preserves_content_without_saving(self):
        model = Mock()
        model.complete.return_value = json.dumps({"kind": "answer", "answer": "复习安排",
            "artifact": {"name": "练习.unknown", "content": "周一复习第六章"}})
        result = solve_request(model, "给我安排复习时间")
        self.assertIn("周一复习第六章", result["answer"])
        self.assertNotIn("artifact", result)

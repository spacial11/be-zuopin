"""Regression tests for script routing, stdout validation, and independent review."""

import json
import unittest
from unittest.mock import Mock

from tidal_harness.contract import AgentRegistry, TaskContract
from tidal_harness.engine import Engine, run_tests
from tidal_harness.runtime import Store


class ScriptTasksTest(unittest.TestCase):
    def setUp(self):
        store = Store(":memory:")
        self.addCleanup(store.conn.close)
        self.model = Mock()
        self.engine = Engine(AgentRegistry.default(), self.model, store)

    def test_wrong_function_fails_stdout_and_revised_script_passes(self):
        task = TaskContract.from_data({
            "description": "读取名字，打印问候。",
            "tests": [{"stdin": "Ada\n", "stdout": "Hello, Ada!"}],
        }, task_id="greeting")
        self.model.complete.side_effect = [
            json.dumps({"code": "def add(a, b):\n    return a + b"}),
            json.dumps({"code": "print('Hello, ' + input() + '!')"}),
            json.dumps({"approve": True}),
        ]

        result = self.engine.run(task)

        self.assertTrue(result.ok)
        first, second = result.rounds
        self.assertFalse(first.tests_ok)
        self.assertEqual(first.tests[0]["stdout"], "")
        self.assertEqual(first.tests[0]["expected"], "Hello, Ada!")
        self.assertEqual(second.tests[0]["stdout"], "Hello, Ada!\n")
        self.assertTrue(second.accepted)
        roles = [call.kwargs["role"] for call in self.model.complete.call_args_list]
        self.assertEqual(roles, ["coder", "coder", "reviewer"])

    def test_description_only_script_runs_and_reviewer_can_reject(self):
        task = TaskContract.from_data({"description": "输出三乘以四的结果。"}, task_id="product")
        self.assertEqual(task.task_type, "script")
        self.assertEqual(task.function_name, "")
        self.assertEqual(task.tests, ())
        self.model.complete.side_effect = [
            json.dumps({"code": "print(3 + 4)"}),
            json.dumps({"approve": False, "issues": ["应当相乘"]}),
            json.dumps({"code": "print(3 * 4)"}),
            json.dumps({"approve": True}),
        ]

        result = self.engine.run(task)

        self.assertTrue(result.ok)
        first, second = result.rounds
        self.assertFalse(first.accepted)
        self.assertEqual(first.review_issues, ["应当相乘"])
        self.assertTrue(second.accepted)
        for index, (record, output) in enumerate(((first, "7\n"), (second, "12\n"))):
            trial, = record.tests
            self.assertEqual(trial["kind"], "smoke")
            self.assertNotIn("expected", trial)
            self.assertEqual(trial["stdout"], output)
            review_call = self.model.complete.call_args_list[index * 2 + 1]
            self.assertEqual(review_call.kwargs["role"], "reviewer")
            prompt = "\n".join(message["content"] for message in review_call.args[0])
            self.assertIn(json.dumps(output), prompt)

    def test_legacy_function_format_still_checks_return_values(self):
        task = TaskContract.from_data({
            "description": "返回较长字符串的长度",
            "function_name": "longer_length",
            "tests": [{"args": ["sky", "sunset"], "expected": 6}],
        }, task_id="length")

        self.assertEqual(task.task_type, "function")
        report = run_tests("def longer_length(a, b):\n    return max(len(a), len(b))", task)
        self.assertTrue(report.ok)
        self.assertEqual((report.passed, report.total), (1, 1))

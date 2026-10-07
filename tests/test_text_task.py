from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from installer import text_task as tt
from installer.provisioning import Provisioner

ROOT = Path(__file__).resolve().parents[1]
NODE_HELPERS = ROOT / "integrations" / "LLM" / "ComfyUI_Qwen_H3_Prompt" / "text_task.py"
SPEC = importlib.util.spec_from_file_location("qwen_text_task_helpers_for_tests", NODE_HELPERS)
helpers = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = helpers
SPEC.loader.exec_module(helpers)


def envelope(text="ok", fmt="text", structured=None):
    return json.dumps({
        "schema": tt.RESULT_SCHEMA, "ok": True, "text": text, "structured": structured,
        "metadata": {"model": "qwen3.8-27b", "backend": "cpu", "response_format": fmt},
    })


class FakeTransport:
    """Scripted ComfyUI: history is empty for `pending` polls, then `entry`."""

    def __init__(self, entry=None, pending=0, submit=None, errors=None):
        self.entry, self.pending = entry, pending
        self.submit = submit if submit is not None else {"prompt_id": "p1", "node_errors": {}}
        self.errors = errors or {}
        self.calls = []

    def post_json(self, path, payload):
        self.calls.append(("POST", path, payload))
        if path in self.errors:
            raise self.errors[path]
        return self.submit if path == "/prompt" else {}

    def get_json(self, path):
        self.calls.append(("GET", path, None))
        if path in self.errors:
            raise self.errors[path]
        if self.pending > 0 or self.entry is None:
            self.pending -= 1
            return {}
        return {"p1": self.entry}

    def paths(self):
        return [(m, p) for m, p, _ in self.calls]


def success_entry(payload):
    return {"status": {"status_str": "success", "messages": []},
            "outputs": {"1": {tt.RESULT_KEY: [payload]}}}


def client(transport, clock=None):
    ticks = iter(range(0, 10_000))
    return tt.TextTaskClient(transport, poll_interval=0, sleeper=lambda _s: None,
                             clock=clock or (lambda: next(ticks)))


class ExecutionTests(unittest.TestCase):
    def test_text_execution_builds_workflow_from_request(self):
        fake = FakeTransport(success_entry(envelope("olá")), pending=2)
        result = client(fake).run(tt.TextTaskRequest(
            user_input="entrada", instructions="seja breve", max_tokens=64, temperature=0.5, seed=7))
        self.assertEqual(result.text, "olá")
        self.assertIsNone(result.structured)
        self.assertEqual(result.metadata["prompt_id"], "p1")
        inputs = fake.calls[0][2]["prompt"]["1"]["inputs"]
        self.assertEqual(inputs["instructions"], "seja breve")
        self.assertEqual(inputs["user_input"], "entrada")
        self.assertEqual((inputs["max_tokens"], inputs["temperature"], inputs["seed"]), (64, 0.5, 7))
        self.assertEqual(inputs["response_format"], "text")

    def test_structured_response_and_schema_forwarding(self):
        schema = {"type": "object", "properties": {"a": {"type": "integer"}}}
        fake = FakeTransport(success_entry(envelope('{"a": 1}', "json", {"a": 1})))
        result = client(fake).run(tt.TextTaskRequest(
            user_input="x", response_format="json", json_schema=schema))
        self.assertEqual(result.structured, {"a": 1})
        self.assertEqual(json.loads(fake.calls[0][2]["prompt"]["1"]["inputs"]["json_schema"]), schema)
        self.assertEqual(result.to_dict()["ok"], True)

    def test_invalid_requests_never_reach_comfy(self):
        fake = FakeTransport()
        for bad in (
            tt.TextTaskRequest(user_input="  "),
            tt.TextTaskRequest(user_input="x", response_format="xml"),
            tt.TextTaskRequest(user_input="x", json_schema={}),
            tt.TextTaskRequest(user_input="x", max_tokens=1),
        ):
            with self.assertRaises(tt.TextTaskError) as ctx:
                client(fake).run(bad)
            self.assertEqual(ctx.exception.code, tt.INVALID_REQUEST)
        self.assertEqual(fake.calls, [])

    def test_runtime_unreachable(self):
        err = tt.TextTaskError(tt.RUNTIME_UNAVAILABLE, "ComfyUI is not reachable")
        with self.assertRaises(tt.TextTaskError) as ctx:
            client(FakeTransport(errors={"/prompt": err})).run(tt.TextTaskRequest(user_input="x"))
        self.assertEqual(ctx.exception.code, tt.RUNTIME_UNAVAILABLE)

    def test_comfy_validation_rejection(self):
        fake = FakeTransport(submit={"prompt_id": "p1", "node_errors": {"1": {"errors": ["bad"]}}})
        with self.assertRaises(tt.TextTaskError) as ctx:
            client(fake).run(tt.TextTaskRequest(user_input="x"))
        self.assertEqual(ctx.exception.code, tt.COMFY_REJECTED)
        self.assertIn("1", ctx.exception.to_dict()["details"]["node_errors"])

    def test_comfy_execution_error(self):
        entry = {"status": {"status_str": "error", "messages": [
            ["execution_error", {"node_type": tt.NODE_CLASS, "exception_type": "RuntimeError",
                                 "exception_message": "llama.cpp exploded"}]]}, "outputs": {}}
        with self.assertRaises(tt.TextTaskError) as ctx:
            client(FakeTransport(entry)).run(tt.TextTaskRequest(user_input="x"))
        self.assertEqual(ctx.exception.code, tt.COMFY_EXECUTION_ERROR)
        self.assertEqual(ctx.exception.message, "llama.cpp exploded")
        self.assertEqual(ctx.exception.details["prompt_id"], "p1")

    def test_node_reported_failure_keeps_code(self):
        payload = json.dumps({"schema": tt.RESULT_SCHEMA, "ok": False,
                              "error": {"code": tt.INVALID_STRUCTURED_OUTPUT, "message": "not json"}})
        with self.assertRaises(tt.TextTaskError) as ctx:
            client(FakeTransport(success_entry(payload))).run(tt.TextTaskRequest(user_input="x"))
        self.assertEqual(ctx.exception.code, tt.INVALID_STRUCTURED_OUTPUT)

    def test_missing_or_malformed_result(self):
        for entry in (
            {"status": {"status_str": "success"}, "outputs": {}},
            success_entry("not json"),
            success_entry(json.dumps({"schema": "other", "ok": True})),
        ):
            with self.assertRaises(tt.TextTaskError) as ctx:
                client(FakeTransport(entry)).run(tt.TextTaskRequest(user_input="x"))
            self.assertEqual(ctx.exception.code, tt.INVALID_RESULT)

    def test_timeout_interrupts_comfy(self):
        fake = FakeTransport(entry=None)
        with self.assertRaises(tt.TextTaskError) as ctx:
            client(fake).run(tt.TextTaskRequest(user_input="x", timeout_s=3))
        self.assertEqual(ctx.exception.code, tt.TIMEOUT)
        self.assertIn(("POST", "/interrupt"), fake.paths())

    def test_cancellation_interrupts_comfy(self):
        fake = FakeTransport(entry=None)
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(tt.TextTaskError) as ctx:
            client(fake).run(tt.TextTaskRequest(user_input="x"), cancel_event=cancel)
        self.assertEqual(ctx.exception.code, tt.CANCELLED)
        self.assertIn(("POST", "/interrupt"), fake.paths())

    def test_interrupted_history_maps_to_cancelled(self):
        entry = {"status": {"status_str": "error", "messages": [["execution_interrupted", {}]]}, "outputs": {}}
        with self.assertRaises(tt.TextTaskError) as ctx:
            client(FakeTransport(entry)).run(tt.TextTaskRequest(user_input="x"))
        self.assertEqual(ctx.exception.code, tt.CANCELLED)


class HttpTransportTests(unittest.TestCase):
    def test_connection_failure_is_runtime_unavailable(self):
        def opener(_request, timeout):
            raise OSError("refused")
        with self.assertRaises(tt.TextTaskError) as ctx:
            tt.HttpTransport("http://127.0.0.1:8189", opener=opener).get_json("/history/x")
        self.assertEqual(ctx.exception.code, tt.RUNTIME_UNAVAILABLE)

    def test_default_endpoint_is_managed_comfyui(self):
        self.assertEqual(tt.default_base_url(), "http://127.0.0.1:8189")


class NodeHelperTests(unittest.TestCase):
    def test_messages_and_response_format(self):
        self.assertEqual([m["role"] for m in helpers.build_messages("sys", "in")], ["system", "user"])
        self.assertEqual([m["role"] for m in helpers.build_messages("", "in")], ["user"])
        with self.assertRaises(helpers.TextTaskInputError):
            helpers.build_messages("sys", " ")
        self.assertIsNone(helpers.build_response_format("text"))
        self.assertEqual(helpers.build_response_format("json"), {"type": "json_object"})
        fmt = helpers.build_response_format("json", '{"type":"object"}')
        self.assertEqual(fmt["json_schema"]["schema"], {"type": "object"})
        with self.assertRaises(helpers.TextTaskInputError):
            helpers.build_response_format("text", '{"type":"object"}')
        with self.assertRaises(helpers.TextTaskInputError):
            helpers.build_response_format("json", "[1]")

    def test_envelope_structured(self):
        kwargs = dict(usage={"total_tokens": 5}, model="m", backend="cpu", seed=1, max_tokens=9, elapsed_s=0.1234)
        env = helpers.build_envelope('```json\n{"a": 1}\n```', response_format="json", **kwargs)
        self.assertEqual(env["structured"], {"a": 1})
        self.assertEqual(env["metadata"]["usage"], {"total_tokens": 5})
        self.assertEqual(env["schema"], tt.RESULT_SCHEMA)
        with self.assertRaises(helpers.StructuredOutputError):
            helpers.build_envelope("nope", response_format="json", **kwargs)
        self.assertIsNone(helpers.build_envelope("nope", response_format="text", **kwargs)["structured"])

    def test_workflow_template_matches_node_schema(self):
        node = json.loads(tt.WORKFLOW_PATH.read_text(encoding="utf-8"))["1"]
        self.assertEqual(node["class_type"], tt.NODE_CLASS)
        node_source = (ROOT / "integrations/LLM/ComfyUI_Qwen_H3_Prompt/node.py").read_text(encoding="utf-8")
        for name in node["inputs"]:
            self.assertIn(f'"{name}"', node_source)


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.comfy = Path(self.tmp.name) / "ComfyUI"
        self.provisioner = Provisioner()

    def tearDown(self):
        self.tmp.cleanup()

    def test_capability_is_discoverable_and_shares_the_qwen_node_and_runtime(self):
        catalog = {c["id"]: c for c in self.provisioner.capability_catalog()}
        self.assertIn("text-task", catalog)
        reqs = catalog["text-task"]["providers"][0]["requirements"]
        self.assertEqual(reqs["models"], ["qwen3-14b-q4-k-m"])
        self.assertEqual(reqs["components"], ["qwen-h3-runtime"])
        self.assertIn("qwen-text-task", reqs["workflow_sets"])
        for rel in self.provisioner.workflow_sets["qwen-text-task"]["workflows"]:
            self.assertTrue((ROOT / rel).is_file())

    def test_not_installed_without_runtime(self):
        status = tt.describe_availability(self.provisioner, self.comfy)
        self.assertEqual(status["status"], "not_installed")
        self.assertIn("base_runtime", status["missing"])

    def test_installed(self):
        with mock.patch.object(Provisioner, "profile_installation",
                               return_value={"installed": True, "missing": []}):
            status = tt.describe_availability(self.provisioner, self.comfy)
        self.assertEqual(status["status"], "installed")

    def test_unavailable_when_source_missing(self):
        with mock.patch.object(Provisioner, "profile_provisionability",
                               return_value={"installable": False, "reason_codes": ["MODEL_SOURCE_UNAVAILABLE:x"]}):
            status = tt.describe_availability(self.provisioner, self.comfy)
        self.assertEqual(status["status"], "unavailable")
        self.assertEqual(status["reasons"], ["MODEL_SOURCE_UNAVAILABLE:x"])


class NoLegacyServerTests(unittest.TestCase):
    def test_new_code_does_not_reference_legacy_server(self):
        for path in (ROOT / "installer" / "text_task.py", NODE_HELPERS, tt.WORKFLOW_PATH):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("5001", text, path)
            self.assertNotIn("localhost", text, path)


if __name__ == "__main__":
    unittest.main()

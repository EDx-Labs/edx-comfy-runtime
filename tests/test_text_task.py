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

    def test_cancellation_cancels_only_its_own_prompt_never_a_global_interrupt(self):
        fake = FakeTransport(entry=None)
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(tt.TextTaskError) as ctx:
            client(fake).run(tt.TextTaskRequest(user_input="x"), cancel_event=cancel)
        self.assertEqual(ctx.exception.code, tt.CANCELLED)
        self.assertIn(("POST", "/api/jobs/p1/cancel"), fake.paths())
        self.assertNotIn(("POST", "/interrupt", {}), fake.calls)
        self.assertNotIn(("POST", "/interrupt"), fake.paths())

    def test_interrupted_history_maps_to_cancelled(self):
        entry = {"status": {"status_str": "error", "messages": [["execution_interrupted", {}]]}, "outputs": {}}
        with self.assertRaises(tt.TextTaskError) as ctx:
            client(FakeTransport(entry)).run(tt.TextTaskRequest(user_input="x"))
        self.assertEqual(ctx.exception.code, tt.CANCELLED)


class SimClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += max(float(seconds), 0.5)


class FakeComfy:
    """ComfyUI com fila FIFO única, histórico, estatísticas e os modos de cancelamento (API de jobs ou legado)."""

    def __init__(self, clock, jobs_api=True, vram_changes=False):
        self.clock, self.jobs_api, self.vram_changes = clock, jobs_api, vram_changes
        self.running, self.pending, self.done, self.calls, self.events = [], [], {}, [], []
        self.interrupted, self.number = [], 0

    def at(self, seconds, action):
        self.events.append((seconds, action))

    def _advance(self):
        for event in [event for event in self.events if event[0] <= self.clock.t]:
            self.events.remove(event)
            event[1]()

    def add_running(self, pid):  # job de outro projeto (ex.: vídeo) que já ocupa a GPU
        self.running.append(pid)

    def finish(self, pid, entry=None):
        self.running = [p for p in self.running if p != pid]
        self.pending = [p for p in self.pending if p != pid]
        self.done[pid] = entry or success_entry(envelope("ok"))
        if self.pending and not self.running:
            self.running.append(self.pending.pop(0))

    def _interrupt(self, pid):
        self.interrupted.append(pid)
        self.finish(pid, {"status": {"status_str": "error", "messages": [["execution_interrupted", {"prompt_id": pid}]]}, "outputs": {}})

    def post_json(self, path, payload):
        self._advance()
        self.calls.append(("POST", path, payload))
        if path == "/prompt":
            self.number += 1
            pid = f"text-{self.number}"
            if self.running:
                self.pending.append(pid)
            else:
                self.running.append(pid)
            return {"prompt_id": pid, "node_errors": {}}
        if path.startswith("/api/jobs/") and path.endswith("/cancel"):
            if not self.jobs_api:
                raise tt.TextTaskError(tt.COMFY_REJECTED, "ComfyUI HTTP 404", details={"status": 404})
            pid = path.split("/")[3]
            if pid in self.running:
                self._interrupt(pid)
            self.pending = [p for p in self.pending if p != pid]
            return {"cancelled": True}
        if path == "/queue":
            self.pending = [p for p in self.pending if p not in payload.get("delete", [])]
            return {}
        if path == "/interrupt":
            target = payload.get("prompt_id")
            for pid in list(self.running):
                if not target or target == pid:  # sem prompt_id = interrupção GLOBAL
                    self._interrupt(pid)
            return {}
        raise AssertionError(path)

    def get_json(self, path):
        self._advance()
        self.calls.append(("GET", path, None))
        if path == "/queue":
            return {"queue_running": [[0, p, {}] for p in self.running],
                    "queue_pending": [[i + 1, p, {}] for i, p in enumerate(self.pending)]}
        if path.startswith("/history/"):
            pid = path.rsplit("/", 1)[1]
            return {pid: self.done[pid]} if pid in self.done else {}
        if path == "/system_stats":
            vram = (1000 + (int(self.clock.t) if self.vram_changes else 0)) * 64 * 1024 * 1024
            # ram_free oscila sempre (como no servidor real); só a VRAM do pool do torch decide "sem variação"
            return {"system": {"ram_free": 5000 + int(self.clock.t)},
                    "devices": [{"vram_free": vram, "torch_vram_total": 64 * 1024 * 1024, "torch_vram_free": vram}]}
        raise AssertionError(path)

    def posted(self):
        return [(path, payload) for method, path, payload in self.calls if method == "POST"]


def sim_client(fake, clock):
    return tt.TextTaskClient(fake, poll_interval=0.5, sleeper=clock.sleep, clock=clock)


class QueueAwareWaitingTests(unittest.TestCase):
    def setUp(self):
        self.clock = SimClock()
        self.fake = FakeComfy(self.clock)
        self.states = []

    def on_state(self, state, info):
        self.states.append((state, dict(info)))

    def kinds(self):
        return [state for state, _ in self.states]

    def test_original_bug_a_text_task_queued_behind_a_long_video_never_interrupts_it(self):
        self.fake.add_running("video-1")  # vídeo de outro projeto: ~50 min na GPU
        self.fake.at(3000, lambda: self.fake.finish("video-1", {"status": {"status_str": "success", "completed": True, "messages": []}, "outputs": {}}))
        self.fake.at(3060, lambda: self.fake.finish("text-1"))
        result = sim_client(self.fake, self.clock).run(tt.TextTaskRequest(user_input="x", timeout_s=900), on_state=self.on_state)
        self.assertEqual(result.text, "ok")
        self.assertEqual(self.fake.interrupted, [])  # o vídeo terminou sozinho
        self.assertEqual(self.fake.done["video-1"]["status"]["status_str"], "success")
        self.assertEqual(self.fake.posted(), [("/prompt", self.fake.calls[0][2])])  # nenhum /interrupt, nenhum cancel
        self.assertGreater(self.clock.t, 3000)  # esperou muito além do timeout de 900 s da tarefa

    def test_the_harness_reproduces_the_incident_with_the_old_algorithm(self):
        """Algoritmo ANTIGO (prazo desde a submissão + POST /interrupt global) contra o mesmo servidor falso: mata o vídeo."""
        fake = FakeComfy(self.clock)
        fake.add_running("video-1")
        pid = fake.post_json("/prompt", {"prompt": {}, "client_id": "x"})["prompt_id"]
        deadline = self.clock() + 900
        with self.assertRaises(TimeoutError):
            while True:
                if fake.get_json(f"/history/{pid}").get(pid):
                    break
                if self.clock() >= deadline:
                    fake.post_json("/interrupt", {})
                    raise TimeoutError
                self.clock.sleep(0.5)
        self.assertEqual(fake.interrupted, ["video-1"])  # o vídeo de OUTRO projeto morreu: o defeito observado

    def test_queue_wait_is_reported_with_position_and_never_counts_toward_the_timeout(self):
        self.fake.add_running("video-1")
        self.fake.at(2400, lambda: self.fake.finish("video-1"))
        self.fake.at(2460, lambda: self.fake.finish("text-1"))
        sim_client(self.fake, self.clock).run(tt.TextTaskRequest(user_input="x", timeout_s=900), on_state=self.on_state)
        self.assertEqual(self.kinds()[0], "QUEUED")
        queued = [info for state, info in self.states if state == "QUEUED" and info.get("position")]
        self.assertEqual((queued[0]["position"], queued[0]["ahead"]), (1, 1))
        running = next(info for state, info in self.states if state == "RUNNING")
        self.assertGreaterEqual(running["waited_seconds"], 2399)
        self.assertNotIn("STALLED_SUSPECTED", self.kinds())
        self.assertEqual(self.kinds()[-1], "COMPLETED")

    def test_silence_beyond_the_threshold_is_suspected_but_the_task_is_not_cancelled(self):
        self.fake.at(1200, lambda: self.fake.finish("text-1"))  # roda 1200 s > 900 s sem terminar
        result = sim_client(self.fake, self.clock).run(tt.TextTaskRequest(user_input="x", timeout_s=900), on_state=self.on_state)
        self.assertEqual(result.text, "ok")
        self.assertIn("STALLED_SUSPECTED", self.kinds())
        self.assertEqual([p for p in self.fake.posted() if p[0] != "/prompt"], [])
        self.assertEqual(self.fake.interrupted, [])

    def test_a_silent_running_task_with_a_flat_server_is_never_cancelled_by_the_client(self):
        # roda 5,5 h sem nenhum sinal de atividade e com o servidor "plano": só suspeita, nunca cancelamento automático
        self.fake.at(1, lambda: self.fake.pending.append("other-project-job"))
        self.fake.at(20000, lambda: self.fake.finish("text-1"))
        result = sim_client(self.fake, self.clock).run(tt.TextTaskRequest(user_input="x", timeout_s=900), on_state=self.on_state)
        self.assertEqual(result.text, "ok")
        self.assertGreaterEqual(self.clock.t, 20000)
        self.assertEqual(self.kinds().count("STALLED_SUSPECTED"), 1)
        self.assertEqual(self.fake.interrupted, [])
        self.assertEqual([path for path, _ in self.fake.posted()], ["/prompt"])  # nenhum cancel, nenhum interrupt

    def test_no_hard_limit_while_the_server_shows_activity(self):
        fake = FakeComfy(self.clock, vram_changes=True)
        fake.at(5000, lambda: fake.finish("text-1"))
        result = sim_client(fake, self.clock).run(tt.TextTaskRequest(user_input="x", timeout_s=900))
        self.assertEqual(result.text, "ok")
        self.assertEqual(fake.interrupted, [])

    def test_cancel_while_pending_only_removes_this_prompt(self):
        self.fake.add_running("video-1")
        cancel = threading.Event()
        self.fake.at(30, cancel.set)
        with self.assertRaises(tt.TextTaskError) as ctx:
            sim_client(self.fake, self.clock).run(tt.TextTaskRequest(user_input="x"), cancel_event=cancel)
        self.assertEqual(ctx.exception.code, tt.CANCELLED)
        self.assertEqual(self.fake.running, ["video-1"])  # o vídeo em execução não foi tocado
        self.assertEqual(self.fake.interrupted, [])
        self.assertEqual(self.fake.pending, [])

    def test_legacy_server_without_the_jobs_api_uses_queue_delete_for_pending(self):
        fake = FakeComfy(self.clock, jobs_api=False)
        fake.add_running("video-1")
        cancel = threading.Event()
        fake.at(30, cancel.set)
        with self.assertRaises(tt.TextTaskError):
            sim_client(fake, self.clock).run(tt.TextTaskRequest(user_input="x"), cancel_event=cancel)
        self.assertIn(("/queue", {"delete": ["text-1"]}), fake.posted())
        self.assertNotIn("/interrupt", [path for path, _ in fake.posted()])
        self.assertEqual(fake.running, ["video-1"])

    def test_legacy_server_interrupts_by_prompt_id_when_the_task_itself_is_running(self):
        fake = FakeComfy(self.clock, jobs_api=False)
        cancel = threading.Event()
        fake.at(30, cancel.set)
        with self.assertRaises(tt.TextTaskError):
            sim_client(fake, self.clock).run(tt.TextTaskRequest(user_input="x"), cancel_event=cancel)
        interrupts = [payload for path, payload in fake.posted() if path == "/interrupt"]
        self.assertEqual(interrupts, [{"prompt_id": "text-1"}])
        self.assertEqual(fake.interrupted, ["text-1"])

    def test_a_prompt_the_server_forgot_fails_with_job_lost(self):
        fake = FakeComfy(self.clock)
        fake.at(20, lambda: setattr(fake, "running", []))  # ComfyUI reiniciou
        with self.assertRaises(tt.TextTaskError) as ctx:
            sim_client(fake, self.clock).run(tt.TextTaskRequest(user_input="x"), on_state=self.on_state)
        self.assertEqual(ctx.exception.code, tt.JOB_LOST)
        self.assertEqual(self.kinds()[-1], "FAILED")

    def test_state_callback_errors_never_change_the_outcome(self):
        self.fake.at(5, lambda: self.fake.finish("text-1"))

        def broken(state, info):
            raise RuntimeError("telemetria quebrada")

        self.assertEqual(sim_client(self.fake, self.clock).run(tt.TextTaskRequest(user_input="x"), on_state=broken).text, "ok")

    def test_source_never_posts_a_global_interrupt(self):
        source = (ROOT / "installer" / "text_task.py").read_text(encoding="utf-8")
        self.assertNotIn('post_json("/interrupt", {})', source)
        self.assertNotIn("def _interrupt", source)


class HttpTransportTests(unittest.TestCase):
    def test_http_errors_expose_the_status_so_callers_can_tell_a_missing_route(self):
        import io
        import urllib.error

        def opener(_request, timeout):
            raise urllib.error.HTTPError("http://x/api/jobs/1/cancel", 404, "nf", {}, io.BytesIO(b"<html>"))

        transport = tt.HttpTransport("http://127.0.0.1:8190", opener=opener)
        with self.assertRaises(tt.TextTaskError) as ctx:
            transport.post_json("/api/jobs/1/cancel", {})
        self.assertEqual((ctx.exception.code, ctx.exception.details["status"]), (tt.COMFY_REJECTED, 404))

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

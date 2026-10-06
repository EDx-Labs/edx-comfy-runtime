"""Defaults de inferência do caminho textual (qwen-text) e preservação do qwen-h3 multimodal.

Sem modelo real, sem llama-server real e sem rede.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

from installer import text_task as client

ROOT = Path(__file__).resolve().parents[1]
NODE_DIR = ROOT / "integrations" / "LLM" / "ComfyUI_Qwen_H3_Prompt"


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"qwen_defaults_{name}", NODE_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


runtime = load("runtime")
text = load("text_task")


def spec(**overrides):
    values = dict(
        executable=Path("C:/comfy/custom_nodes/ComfyUI_Qwen_H3_Prompt/runtime/llama-server.exe"),
        library_dirs=(Path("C:/comfy/custom_nodes/ComfyUI_Qwen_H3_Prompt/runtime"),),
        platform_name="windows", backend="cuda13", n_gpu_layers="auto", fit=True,
        fit_target_mib=1536, flash_attention="auto",
    )
    values.update(overrides)
    return runtime.RuntimeSpec(**values)


def value_after(arguments: list[str], flag: str) -> str:
    return arguments[arguments.index(flag) + 1]


class TextDefaultsTests(unittest.TestCase):
    def test_defaults_are_validated_values(self) -> None:
        self.assertEqual(text.resolve_inference_options({}), {"context_size": 8192, "fit_target_mib": 768})
        self.assertEqual((text.TEXT_CONTEXT_SIZE_DEFAULT, text.TEXT_FIT_TARGET_MIB_DEFAULT), (8192, 768))

    def test_no_fixed_gpu_layers_in_text_options(self) -> None:
        self.assertNotIn("n_gpu_layers", text.resolve_inference_options({}))

    def test_context_override(self) -> None:
        options = text.resolve_inference_options({text.CONTEXT_SIZE_ENV: "16384"})
        self.assertEqual(options, {"context_size": 16384, "fit_target_mib": 768})

    def test_fit_target_override(self) -> None:
        options = text.resolve_inference_options({text.FIT_TARGET_ENV: " 600 "})
        self.assertEqual(options, {"context_size": 8192, "fit_target_mib": 600})

    def test_blank_override_keeps_default(self) -> None:
        self.assertEqual(text.resolve_inference_options({text.CONTEXT_SIZE_ENV: "  "})["context_size"], 8192)

    def test_invalid_overrides_are_rejected(self) -> None:
        for name, value in ((text.CONTEXT_SIZE_ENV, "abc"), (text.CONTEXT_SIZE_ENV, "512"),
                            (text.FIT_TARGET_ENV, "-1"), (text.FIT_TARGET_ENV, "9999999")):
            with self.assertRaises(text.RuntimeConfigError, msg=f"{name}={value}"):
                text.resolve_inference_options({name: value})

    def test_environment_is_read_by_default(self) -> None:
        with mock.patch.dict("os.environ", {text.FIT_TARGET_ENV: "900"}):
            self.assertEqual(text.resolve_inference_options()["fit_target_mib"], 900)


class ServerArgumentTests(unittest.TestCase):
    model, projector = Path("C:/models/qwen.gguf"), Path("C:/models/mmproj.gguf")

    def text_args(self, **kwargs) -> list[str]:
        options = text.resolve_inference_options(kwargs.pop("environ", {}))
        server = runtime.LlamaServer(self.model, None, options["context_size"], spec(), options["fit_target_mib"])
        return server.build_arguments()

    def test_text_path_uses_8192_and_768_with_fit_on(self) -> None:
        arguments = self.text_args()
        self.assertEqual(value_after(arguments, "--ctx-size"), "8192")
        self.assertEqual(value_after(arguments, "--fit"), "on")
        self.assertEqual(value_after(arguments, "--fit-target"), "768")

    def test_text_path_never_fixes_gpu_layers(self) -> None:
        arguments = self.text_args()
        self.assertEqual(value_after(arguments, "--n-gpu-layers"), "auto")
        self.assertFalse(value_after(arguments, "--n-gpu-layers").isdigit())

    def test_text_path_overrides_reach_the_command_line(self) -> None:
        arguments = self.text_args(environ={text.CONTEXT_SIZE_ENV: "16384", text.FIT_TARGET_ENV: "600"})
        self.assertEqual(value_after(arguments, "--ctx-size"), "16384")
        self.assertEqual(value_after(arguments, "--fit-target"), "600")

    def test_text_only_model_gets_no_vision_arguments(self) -> None:
        arguments = self.text_args()
        for flag in ("--mmproj", "--image-min-tokens", "--image-max-tokens"):
            self.assertNotIn(flag, arguments)

    def test_cpu_backend_keeps_fit_disabled(self) -> None:
        cpu = spec(backend="cpu", n_gpu_layers="0", fit=False)
        arguments = runtime.LlamaServer(self.model, None, 8192, cpu, 768).build_arguments()
        self.assertNotIn("--fit", arguments)
        self.assertNotIn("--fit-target", arguments)

    def test_h3_defaults_are_unchanged(self) -> None:
        arguments = runtime.LlamaServer(self.model, self.projector, runtime_spec=spec()).build_arguments()
        self.assertEqual(runtime.DEFAULT_CONTEXT_SIZE, 32768)
        self.assertEqual(value_after(arguments, "--ctx-size"), "32768")
        self.assertEqual(value_after(arguments, "--fit-target"), "1536")  # from runtime_config, not the text default
        self.assertEqual(value_after(arguments, "--n-gpu-layers"), "auto")
        self.assertEqual(value_after(arguments, "--mmproj"), str(self.projector))
        self.assertEqual(value_after(arguments, "--image-min-tokens"), "1024")
        self.assertEqual(value_after(arguments, "--image-max-tokens"), "1024")
        # --mmproj keeps its historical position: right after --model, before --alias
        self.assertEqual(arguments.index("--mmproj"), arguments.index("--model") + 2)
        self.assertEqual(arguments.index("--alias"), arguments.index("--mmproj") + 2)

    def test_h3_and_text_do_not_share_a_server_key(self) -> None:
        created = []

        class FakeServer:
            is_running = True
            def __init__(self, model, mmproj, context_size, runtime_spec, fit_target_mib=None):
                created.append((mmproj, context_size, fit_target_mib))
            def start(self): pass
            def stop(self): pass

        with mock.patch.object(runtime, "LlamaServer", FakeServer), mock.patch.object(runtime, "load_runtime_spec", return_value=spec()):
            manager = runtime.LlamaServerManager()
            manager.acquire(self.model, self.projector)  # exactly how QwenH3PromptLocal calls it
            _, loaded_new = manager.acquire(self.model, self.projector)
            self.assertFalse(loaded_new, "o servidor H3 residente deve ser reaproveitado")
            manager.acquire(self.model, self.projector, context_size=8192, fit_target_mib=768)  # text path
        self.assertEqual(created, [(self.projector, 32768, None), (self.projector, 8192, 768)])


class NodeWiringTests(unittest.TestCase):
    """node.py importa o ComfyUI; confere o roteamento pelo AST."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tree = ast.parse((NODE_DIR / "node.py").read_text(encoding="utf-8"))

    def acquire_calls(self, class_name: str) -> list[ast.Call]:
        cls = next(n for n in self.tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)
        return [n for n in ast.walk(cls) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "acquire"]

    def test_h3_node_acquires_with_defaults_only(self) -> None:
        calls = self.acquire_calls("QwenH3Prompt")
        self.assertEqual(len(calls), 1)
        self.assertEqual((len(calls[0].args), calls[0].keywords), (2, []), "qwen-h3 não pode receber ctx/fit-target do texto")

    def test_text_node_acquires_with_resolved_options(self) -> None:
        calls = self.acquire_calls("QwenTextTask")
        self.assertEqual(len(calls), 1)
        keywords = {k.arg: ast.unparse(k.value) for k in calls[0].keywords}
        self.assertEqual(keywords, {"context_size": "options['context_size']", "fit_target_mib": "options['fit_target_mib']"})

    def test_text_node_checks_context_before_generating(self) -> None:
        cls = next(n for n in self.tree.body if isinstance(n, ast.ClassDef) and n.name == "QwenTextTask")
        run = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "_run")
        calls = sorted((n for n in ast.walk(run) if isinstance(n, ast.Call)), key=lambda n: (n.lineno, n.col_offset))
        names = [ast.unparse(n.func) for n in calls]
        self.assertIn("ensure_fits_context", names)
        self.assertLess(names.index("ensure_fits_context"), names.index("server.chat"))


class ContextProtectionTests(unittest.TestCase):
    def test_fits(self) -> None:
        text.ensure_fits_context(1000, 2048, 8192)
        text.ensure_fits_context(8192 - 2048 - text.CONTEXT_CHECK_MARGIN, 2048, 8192)

    def test_overflow_is_structured_and_explicit(self) -> None:
        with self.assertRaises(text.ContextExceededError) as raised:
            text.ensure_fits_context(7000, 2048, 8192)
        error = raised.exception
        self.assertEqual(error.details(), {"prompt_tokens": 7000, "max_tokens": 2048, "context_size": 8192})
        self.assertIn("7000", str(error)); self.assertIn("8192", str(error)); self.assertIn(text.CONTEXT_SIZE_ENV, str(error))

    def test_boundary_counts_the_margin(self) -> None:
        with self.assertRaises(text.ContextExceededError):
            text.ensure_fits_context(8192 - 2048 - text.CONTEXT_CHECK_MARGIN + 1, 2048, 8192)

    def test_prompt_tokens_come_from_llama_cpp_template_and_tokenizer(self) -> None:
        server = runtime.LlamaServer(Path("m.gguf"), None, 8192, spec())
        calls = []

        def fake_request(method, path, payload=None, timeout=0):
            calls.append((method, path, payload))
            if path == "/apply-template":
                return {"prompt": "<|im_start|>user\nolá<|im_end|>\n"}
            return {"tokens": list(range(11))}

        with mock.patch.object(server, "_request", side_effect=fake_request):
            count = server.count_prompt_tokens([{"role": "user", "content": "olá"}])
        self.assertEqual(count, 11)
        self.assertEqual([c[1] for c in calls], ["/apply-template", "/tokenize"])
        self.assertEqual(calls[1][2]["content"], "<|im_start|>user\nolá<|im_end|>\n")

    def test_envelope_records_effective_context_settings(self) -> None:
        envelope = text.build_envelope("ok", response_format="text", usage={}, model="m", backend="cuda13", seed=0,
                                       max_tokens=16, elapsed_s=1.0, context_size=8192, fit_target_mib=768, prompt_tokens=50)
        self.assertEqual({k: envelope["metadata"][k] for k in ("context_size", "fit_target_mib", "prompt_tokens")},
                         {"context_size": 8192, "fit_target_mib": 768, "prompt_tokens": 50})

    def test_runtime_client_forwards_structured_context_error(self) -> None:
        payload = json.dumps({"schema": client.RESULT_SCHEMA, "ok": False, "error": {
            "code": client.CONTEXT_EXCEEDED, "message": "Contexto insuficiente",
            "details": {"prompt_tokens": 7000, "max_tokens": 2048, "context_size": 8192}}})
        with self.assertRaises(client.TextTaskError) as raised:
            client.TextTaskClient._parse([payload], "p1")
        self.assertEqual(raised.exception.code, client.CONTEXT_EXCEEDED)
        self.assertEqual(raised.exception.details, {"prompt_id": "p1", "prompt_tokens": 7000, "max_tokens": 2048, "context_size": 8192})


if __name__ == "__main__":
    unittest.main()

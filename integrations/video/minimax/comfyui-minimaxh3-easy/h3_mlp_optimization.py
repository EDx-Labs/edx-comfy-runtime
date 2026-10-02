"""Exact MiniMax H3 MLP execution modes and per-generation profiling.

The native H3 MLP is ``fc1 -> SwiGLU -> fc2``. Current ComfyUI builds route
that expression through ``comfy.ops.linear_input_act`` so compatible quantized
weights can use the fused Comfy Kitchen epilogue. This module preserves that
path and adds opt-in row chunking whose two weights are prepared once for the
complete MLP call, never once per chunk.
"""

from __future__ import annotations

import contextlib
import functools
import inspect
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import torch
import torch.nn.functional as F

import comfy.model_management
import comfy.ops
import comfy.patcher_extension


LOGGER = logging.getLogger(__name__)
MODE_ORIGINAL = "original"
MODE_OPTIMIZED = "optimized"
MODE_CHUNKED = "chunked"
MLP_MODES = (MODE_ORIGINAL, MODE_OPTIMIZED, MODE_CHUNKED)
_OUTER_WRAPPER_KEY = "minimax_h3_easy_mlp_profile_outer"
_DIFFUSION_WRAPPER_KEY = "minimax_h3_easy_mlp_profile_step"


def _is_cuda_device(device: Any) -> bool:
    return getattr(device, "type", None) == "cuda" and torch.cuda.is_available()


def _tensor_device(args: tuple[Any, ...], kwargs: dict[str, Any]) -> torch.device | None:
    values = list(args) + list(kwargs.values())
    while values:
        value = values.pop(0)
        if isinstance(value, torch.Tensor):
            return value.device
        if isinstance(value, dict):
            values[0:0] = list(value.values())
        elif isinstance(value, (list, tuple)):
            values[0:0] = list(value)
    return None


@dataclass
class _TimedCall:
    start: Any
    end: Any
    cpu_ms: float | None = None


@dataclass
class _MLPProfile:
    mode: str
    chunk_rows: int
    block_count: int
    mlp_calls: list[_TimedCall] = field(default_factory=list)
    step_calls: list[_TimedCall] = field(default_factory=list)
    wall_started: float = 0.0
    cuda_device: torch.device | None = None
    baseline_allocated: int = 0
    fallback_reasons: set[str] = field(default_factory=set)
    warned_fallbacks: set[str] = field(default_factory=set)
    runtime_paths: set[str] = field(default_factory=set)
    active: bool = False

    def reset(self, device: torch.device | None) -> None:
        self.mlp_calls.clear()
        self.step_calls.clear()
        self.fallback_reasons.clear()
        self.warned_fallbacks.clear()
        self.runtime_paths.clear()
        self.cuda_device = device if _is_cuda_device(device) else None
        self.baseline_allocated = 0
        if self.cuda_device is not None:
            torch.cuda.synchronize(self.cuda_device)
            self.baseline_allocated = int(torch.cuda.memory_allocated(self.cuda_device))
            torch.cuda.reset_peak_memory_stats(self.cuda_device)
        self.wall_started = time.perf_counter()
        self.active = True

    def warn_fallback_once(self, kind: str, error: Exception) -> None:
        if kind in self.warned_fallbacks:
            return
        self.warned_fallbacks.add(kind)
        LOGGER.warning("MiniMax H3 %s MLP unavailable; using original: %s", kind, error)

    def timed(self, calls: list[_TimedCall], device: torch.device | None):
        profile = self

        class _Timer:
            def __enter__(self):
                self.enabled = profile.active
                if not self.enabled:
                    self.start = self.end = self.cpu_start = None
                elif _is_cuda_device(device):
                    if profile.cuda_device is None:
                        profile.cuda_device = device
                        profile.baseline_allocated = int(torch.cuda.memory_allocated(device))
                        torch.cuda.reset_peak_memory_stats(device)
                    self.start = torch.cuda.Event(enable_timing=True)
                    self.end = torch.cuda.Event(enable_timing=True)
                    self.start.record()
                    self.cpu_start = None
                else:
                    self.start = self.end = None
                    self.cpu_start = time.perf_counter()
                return self

            def __exit__(self, *_exc):
                if not self.enabled:
                    return
                if self.start is not None:
                    self.end.record()
                    calls.append(_TimedCall(self.start, self.end))
                else:
                    elapsed = (time.perf_counter() - self.cpu_start) * 1000.0
                    calls.append(_TimedCall(None, None, elapsed))

        return _Timer()

    @staticmethod
    def _milliseconds(calls: list[_TimedCall]) -> list[float]:
        result = []
        for call in calls:
            if call.cpu_ms is not None:
                result.append(float(call.cpu_ms))
            else:
                result.append(float(call.start.elapsed_time(call.end)))
        return result

    def finish(self) -> None:
        if not self.active:
            return
        self.active = False
        if self.cuda_device is not None:
            torch.cuda.synchronize(self.cuda_device)
        wall_s = time.perf_counter() - self.wall_started
        mlp_ms = self._milliseconds(self.mlp_calls)
        step_ms = self._milliseconds(self.step_calls)
        total_mlp_ms = sum(mlp_ms)
        mean_block_ms = total_mlp_ms / len(mlp_ms) if mlp_ms else 0.0
        peak_allocated = peak_reserved = 0
        if self.cuda_device is not None:
            peak_allocated = int(torch.cuda.max_memory_allocated(self.cuda_device))
            peak_reserved = int(torch.cuda.max_memory_reserved(self.cuda_device))
        peak_delta = max(0, peak_allocated - self.baseline_allocated)
        mib = 1024.0 * 1024.0
        step_text = ", ".join(f"{value / 1000.0:.3f}s" for value in step_ms) or "n/a"
        LOGGER.info(
            "MiniMax H3 MLP profile | mode=%s chunk_rows=%d blocks=%d calls=%d "
            "generation=%.3fs mlp_total=%.3fs mlp_mean_block=%.3fms "
            "peak_allocated=%.1fMiB peak_delta=%.1fMiB peak_reserved=%.1fMiB "
            "baseline_allocated=%.1fMiB "
            "steps=[%s] runtime=%s fallbacks=%s",
            self.mode,
            self.chunk_rows,
            self.block_count,
            len(mlp_ms),
            wall_s,
            total_mlp_ms / 1000.0,
            mean_block_ms,
            peak_allocated / mib,
            peak_delta / mib,
            peak_reserved / mib,
            self.baseline_allocated / mib,
            step_text,
            "+".join(sorted(self.runtime_paths)) or "original",
            "; ".join(sorted(self.fallback_reasons)) or "none",
        )


def _core_optimized_mlp(mlp: Any, value: torch.Tensor) -> torch.Tensor:
    linear_input_act = getattr(comfy.ops, "linear_input_act", None)
    if not callable(linear_input_act):
        raise RuntimeError("this ComfyUI build has no comfy.ops.linear_input_act")
    return linear_input_act(mlp.fc2, mlp.fc1(value), "swiglu")


def _optimized_runtime_path(mlp: Any) -> str:
    layout = str(getattr(getattr(mlp.fc2, "weight", None), "_layout_cls", ""))
    if layout == "TensorWiseINT8Layout":
        return "core-fused-int8-swiglu"
    return "core-linear-input-act"


def _patch_functions_empty(linear: Any) -> bool:
    return not getattr(linear, "weight_function", ()) and not getattr(linear, "bias_function", ())


def _uses_quantized_input(linear: Any) -> bool:
    return (
        getattr(linear, "layout_type", None) is not None
        and not getattr(linear, "_full_precision_mm", False)
        and not getattr(linear, "comfy_force_cast_weights", False)
        and _patch_functions_empty(linear)
    )


@functools.lru_cache(maxsize=8)
def _call_parameters(callable_object: Callable[..., Any]) -> frozenset[str]:
    try:
        return frozenset(inspect.signature(callable_object).parameters)
    except (TypeError, ValueError):
        return frozenset()


def _cast_context(linear: Any, value: torch.Tensor, *, want_requant: bool):
    """Prepare one linear once, retaining a quantized representation when possible."""
    context_type = getattr(comfy.ops, "CastBiasWeightContext", None)
    cast = getattr(comfy.ops, "cast_bias_weight", None)
    if context_type is None or not callable(cast):
        raise RuntimeError("this ComfyUI build has no one-shot weight preparation context")

    kwargs: dict[str, Any] = {"offloadable": True}
    parameters = _call_parameters(cast)
    if "compute_dtype" in parameters:
        kwargs["compute_dtype"] = value.dtype
    if "want_requant" in parameters:
        # Keep supported quantized weights quantized. With live weight/bias
        # patches, mirror the module's original dequantized fallback instead.
        kwargs["want_requant"] = want_requant
    return context_type(linear, value, **kwargs)


@dataclass
class _PreparedLinear:
    weight: Any
    bias: Any
    pre_quant_scale: torch.Tensor | None = None
    input_scale: torch.Tensor | None = None
    quantized_tensor_type: Any = None
    layout_type: Any = None

    def __call__(self, value: torch.Tensor) -> torch.Tensor:
        if self.pre_quant_scale is not None:
            value = value * self.pre_quant_scale
        if self.quantized_tensor_type is not None:
            value = self.quantized_tensor_type.from_float(
                value,
                self.layout_type,
                scale=self.input_scale,
            )
        # QuantizedTensor participates in torch linear dispatch. Plain tensors
        # use the same F.linear operation as Comfy's non-fused fallback.
        return F.linear(value, self.weight, self.bias)


def _prepare_linear(linear: Any, value: torch.Tensor, stack: contextlib.ExitStack) -> _PreparedLinear:
    run_every_op = getattr(comfy.ops, "run_every_op", None)
    if callable(run_every_op):
        run_every_op()

    quantized_input = _uses_quantized_input(linear)
    quantized_tensor_type = layout_type = input_scale = None
    if quantized_input:
        try:
            from comfy.quant_ops import QUANT_ALGOS, QuantizedTensor
        except ImportError as exc:
            raise RuntimeError("quantized chunking requires comfy.quant_ops") from exc

        quant_format = getattr(linear, "quant_format", None)
        if not QUANT_ALGOS.get(quant_format, {}).get("quantize_input", True):
            raise RuntimeError(f"weight-only quantization {quant_format!r} has no exact chunked path")
        quantized_tensor_type = QuantizedTensor
        layout_type = linear.layout_type
        input_scale = getattr(linear, "input_scale", None)
        if input_scale is not None:
            input_scale = comfy.model_management.cast_to_device(input_scale, value.device, None)

    pre_quant_scale = getattr(linear, "pre_quant_scale", None)
    if pre_quant_scale is not None:
        pre_quant_scale = comfy.model_management.cast_to_device(
            pre_quant_scale,
            value.device,
            value.dtype,
        )

    weight, bias = stack.enter_context(
        _cast_context(linear, value, want_requant=quantized_input)
    )
    return _PreparedLinear(
        weight=weight,
        bias=bias,
        pre_quant_scale=pre_quant_scale,
        input_scale=input_scale,
        quantized_tensor_type=quantized_tensor_type,
        layout_type=layout_type,
    )


def _chunked_mlp(mlp: Any, value: torch.Tensor, chunk_rows: int) -> torch.Tensor:
    if getattr(comfy.model_management, "in_training", False) or torch.is_grad_enabled():
        raise RuntimeError("chunked inference is disabled while autograd/training is active")
    if value.ndim < 2:
        raise RuntimeError(f"expected an MLP tensor with at least 2 dimensions, got {value.ndim}")

    original_shape = value.shape
    flat = value.reshape(-1, original_shape[-1])
    rows = int(flat.shape[0])
    if rows <= chunk_rows:
        return _core_optimized_mlp(mlp, value)

    ffn = int(getattr(mlp.fc2, "in_features", 0))
    if ffn <= 0:
        raise RuntimeError("could not determine the H3 fc2 input width")

    # Two phases avoid making fc1 and fc2 resident simultaneously. Each weight
    # is still prepared exactly once, outside its corresponding chunk loop.
    intermediate = torch.empty((rows, ffn), device=value.device, dtype=value.dtype)
    with contextlib.ExitStack() as stack:
        fc1 = _prepare_linear(mlp.fc1, flat, stack)
        for start in range(0, rows, chunk_rows):
            stop = min(rows, start + chunk_rows)
            hidden = fc1(flat[start:stop])
            gate, up = hidden.chunk(2, dim=-1)
            activated = F.silu(gate).mul_(up)
            intermediate[start:stop].copy_(activated)
            del hidden, gate, up, activated
    del fc1

    out_features = int(getattr(mlp.fc2, "out_features", original_shape[-1]))
    output = torch.empty((rows, out_features), device=value.device, dtype=value.dtype)
    with contextlib.ExitStack() as stack:
        fc2 = _prepare_linear(mlp.fc2, intermediate, stack)
        for start in range(0, rows, chunk_rows):
            stop = min(rows, start + chunk_rows)
            chunk = fc2(intermediate[start:stop])
            output[start:stop].copy_(chunk)
            del chunk
    del fc2

    return output.reshape(*original_shape[:-1], out_features)


def _original_forward(forward: Callable[..., Any]) -> Callable[..., Any]:
    return getattr(forward, "_minimax_h3_easy_original", forward)


def _is_cuda_oom(error: Exception) -> bool:
    oom_type = getattr(torch.cuda, "OutOfMemoryError", None)
    return oom_type is not None and isinstance(error, oom_type)


def _make_mlp_forward(
    mlp: Any,
    original: Callable[[torch.Tensor], torch.Tensor],
    profile: _MLPProfile,
) -> Callable[[torch.Tensor], torch.Tensor]:
    original = _original_forward(original)

    def forward(value: torch.Tensor) -> torch.Tensor:
        with profile.timed(profile.mlp_calls, value.device):
            if profile.mode == MODE_ORIGINAL:
                profile.runtime_paths.add("original")
                return original(value)
            if profile.mode == MODE_OPTIMIZED:
                try:
                    result = _core_optimized_mlp(mlp, value)
                    profile.runtime_paths.add(_optimized_runtime_path(mlp))
                    return result
                except Exception as exc:
                    if _is_cuda_oom(exc):
                        raise
                    reason = f"optimized->original: {type(exc).__name__}: {exc}"
                    profile.fallback_reasons.add(reason)
                    profile.warn_fallback_once("optimized", exc)
                    return original(value)
            try:
                result = _chunked_mlp(mlp, value, profile.chunk_rows)
                rows = value.numel() // value.shape[-1]
                if rows <= profile.chunk_rows:
                    profile.runtime_paths.add(f"{_optimized_runtime_path(mlp)}-no-chunk")
                else:
                    profile.runtime_paths.add("prepared-two-phase-chunked")
                return result
            except Exception as exc:
                if _is_cuda_oom(exc):
                    raise
                reason = f"chunked->original: {type(exc).__name__}: {exc}"
                profile.fallback_reasons.add(reason)
                profile.warn_fallback_once("chunked", exc)
                return original(value)

    forward._minimax_h3_easy_original = original  # type: ignore[attr-defined]
    return forward


def _remove_wrapper(model: Any, wrapper_type: Any, key: str) -> None:
    remove = getattr(model, "remove_wrappers_with_key", None)
    if callable(remove):
        remove(wrapper_type, key)


def apply_h3_mlp_optimization(model: Any, mode: str, chunk_rows: int) -> Any:
    """Return a model-scoped H3 MLP patch with exact fallbacks and profiling."""
    mode = str(mode or MODE_ORIGINAL).lower()
    if mode not in MLP_MODES:
        raise ValueError(f"MLP mode must be one of {MLP_MODES}, got {mode!r}")
    chunk_rows = max(64, int(chunk_rows))
    if not callable(getattr(model, "clone", None)):
        raise ValueError("The connected MODEL does not support ComfyUI model cloning")

    patched = model.clone()
    diffusion = patched.get_model_object("diffusion_model")
    blocks = getattr(diffusion, "blocks", None)
    if blocks is None or diffusion.__class__.__name__ != "MiniMaxH3Model":
        raise ValueError("MiniMax H3 MLP Optimization accepts only the native ComfyUI MiniMaxH3Model")
    if len(blocks) != 50:
        LOGGER.warning("MiniMax H3 MLP Optimization found %d blocks instead of the expected 50", len(blocks))

    profile = _MLPProfile(mode=mode, chunk_rows=chunk_rows, block_count=len(blocks))
    for index, block in enumerate(blocks):
        mlp = getattr(block, "mlp", None)
        if mlp is None or not hasattr(mlp, "fc1") or not hasattr(mlp, "fc2"):
            raise ValueError(f"MiniMax H3 block {index} does not expose the expected fc1/SwiGLU/fc2 MLP")
        original = patched.get_model_object(f"diffusion_model.blocks.{index}.mlp.forward")
        patched.add_object_patch(
            f"diffusion_model.blocks.{index}.mlp.forward",
            _make_mlp_forward(mlp, original, profile),
        )

    wrappers = comfy.patcher_extension.WrappersMP
    add_wrapper = getattr(patched, "add_wrapper_with_key", None)
    if not callable(add_wrapper):
        raise RuntimeError("MiniMax H3 MLP Optimization requires current ComfyUI model wrapper support")
    _remove_wrapper(patched, wrappers.OUTER_SAMPLE, _OUTER_WRAPPER_KEY)
    _remove_wrapper(patched, wrappers.DIFFUSION_MODEL, _DIFFUSION_WRAPPER_KEY)

    def outer_sample(executor, *args, **kwargs):
        device = getattr(patched, "load_device", None)
        if not _is_cuda_device(device):
            get_device = getattr(comfy.model_management, "get_torch_device", None)
            if callable(get_device):
                device = get_device()
        profile.reset(device)
        try:
            return executor(*args, **kwargs)
        finally:
            profile.finish()

    def diffusion_step(executor, *args, **kwargs):
        device = _tensor_device(args, kwargs)
        with profile.timed(profile.step_calls, device):
            return executor(*args, **kwargs)

    add_wrapper(wrappers.OUTER_SAMPLE, _OUTER_WRAPPER_KEY, outer_sample)
    add_wrapper(wrappers.DIFFUSION_MODEL, _DIFFUSION_WRAPPER_KEY, diffusion_step)
    return patched


class MiniMaxH3EasyMLPOptimization:
    CATEGORY = "MiniMax H3 Easy/model patches"
    FUNCTION = "patch"
    RETURN_TYPES = ("MODEL",)
    RETURN_NAMES = ("model",)
    DESCRIPTION = (
        "Exact H3 MLP execution: original, Comfy fused optimized, or prepared-weight row chunking. "
        "Logs MLP time, mean block time, VRAM peak, and per-step time for each sampling run."
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": ("MODEL",),
                "mode": (list(MLP_MODES), {"default": MODE_OPTIMIZED}),
                "chunk_rows": (
                    "INT",
                    {
                        "default": 1024,
                        "min": 64,
                        "max": 16384,
                        "step": 64,
                        "tooltip": "Used only in chunked mode. Lower values reduce peak activation VRAM but launch more kernels.",
                    },
                ),
            }
        }

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return repr((kwargs.get("mode"), kwargs.get("chunk_rows")))

    @staticmethod
    def patch(model, mode, chunk_rows):
        return (apply_h3_mlp_optimization(model, mode, chunk_rows),)

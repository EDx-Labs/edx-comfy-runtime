from __future__ import annotations
import atexit
import gc
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from server import PromptServer
from pathlib import Path
import comfy.model_management
import folder_paths
from comfy_api.latest import ComfyExtension, io
from .media import (
    image_content,
    image_grid_content,
    sample_indices_per_second,
    text_content,
)
from .runtime import LlamaServerManager
from .skills import (
    SKILL_NAMES,
    detect_h3_mode,
    mode_router_prompt,
    output_issues,
    parse_mode_selection,
    parse_skill_selection,
    router_prompt,
    system_prompt,
)
WEB_DIRECTORY = "./web"
MODEL_DIR = Path(folder_paths.models_dir) / "LLM" / "Qwen3.8"
DEFAULT_MODEL = "Qwen3.8-27B-Q4_K_M.gguf"
DEFAULT_MMPROJ = "mmproj-F16.gguf"
MODEL_DOWNLOADS = {
    DEFAULT_MODEL: "https://huggingface.co/unsloth/Qwen3.8-27B-GGUF/resolve/2c80088ea5e6033bed6a180e28a6573d98e8c0cf/Qwen3.8-27B-Q4_K_M.gguf",
    DEFAULT_MMPROJ: "https://huggingface.co/unsloth/Qwen3.8-27B-GGUF/resolve/main/mmproj-F16.gguf",
}
DOWNLOAD_CHUNK_SIZE = 8 * 1024 * 1024
REFERENCE_VIDEO_FPS = 24.0
INFERENCE_LOCK = threading.Lock()
DOWNLOAD_LOCK = threading.Lock()
SERVER_MANAGER = LlamaServerManager()
LOGGER = logging.getLogger("ComfyUI.QwenH3Prompt")
GREEN = "\033[92m"
RED = "\033[91m"
RESET = "\033[0m"
atexit.register(SERVER_MANAGER.release)
def _success(message: str) -> None:
    LOGGER.info("%s%s%s", GREEN, message, RESET)
def _failure(message: str) -> None:
    LOGGER.error("%s%s%s", RED, message, RESET)
def _usage_summary(usage: dict[str, int]) -> str:
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    total_tokens = usage.get("total_tokens")
    if total_tokens is None:
        return "token usage unavailable"
    return (
        f"prompt {prompt_tokens or 0}, completion {completion_tokens or 0}, "
        f"total {total_tokens} tokens"
    )
def _release_node_resources() -> None:
    SERVER_MANAGER.release()
    gc.collect()
    comfy.model_management.soft_empty_cache(force=True)
def _model_options(projector: bool) -> list[str]:
    files = sorted(path.name for path in MODEL_DIR.glob("*.gguf")) if MODEL_DIR.is_dir() else []
    if projector:
        options = [name for name in files if "mmproj" in name.lower()]
        fallback = DEFAULT_MMPROJ
    else:
        options = [name for name in files if "mmproj" not in name.lower()]
        fallback = DEFAULT_MODEL
    return options or [fallback]
def _resolve_model(name: str) -> Path:
    root = MODEL_DIR.resolve()
    path = (root / name).resolve()
    if path.parent != root or not path.is_file() or path.suffix.lower() != ".gguf":
        raise FileNotFoundError(f"Invalid or missing GGUF model in {root}: {name}")
    return path
def _autogrow_values(inputs) -> list:
    if not inputs:
        return []
    def index(item):
        match = re.search(r"(\d+)$", item[0])
        return int(match.group(1)) if match else 0
    return [value for _, value in sorted(inputs.items(), key=index) if value is not None]
def _validate_reference_images(images: list) -> None:
    if len(images) > 9:
        raise ValueError(f"At most 9 reference images are supported; got {len(images)}.")
    for image_index, image in enumerate(images, 1):
        image_count = int(image.shape[0])
        if image_count != 1:
            raise ValueError(
                f"Reference image {image_index} must contain exactly one image; got a batch of {image_count}."
            )
def _reference_video_details(
    videos: list,
    sample_frames_per_second: int,
) -> list[tuple]:
    if len(videos) > 3:
        raise ValueError(f"At most 3 reference videos are supported; got {len(videos)}.")
    details = []
    total_duration = 0.0
    for video_index, frames in enumerate(videos, 1):
        frame_count = int(frames.shape[0])
        if frame_count < 1:
            raise ValueError(
                f"Reference video {video_index} must contain at least 1 frame; got {frame_count}."
            )
        source_duration = frame_count / REFERENCE_VIDEO_FPS
        if source_duration > 15.0:
            raise ValueError(
                f"Reference video {video_index} must be at most 15 seconds at 24 fps; "
                f"got {source_duration:.2f}s."
            )
        total_duration += source_duration
        details.append(
            (
                frames,
                frame_count,
                source_duration,
                sample_indices_per_second(
                    frame_count,
                    REFERENCE_VIDEO_FPS,
                    sample_frames_per_second,
                ),
            )
        )
    if total_duration > 15.0:
        raise ValueError(
            f"Reference videos may total at most 15 seconds; got {total_duration:.2f}s."
        )
    return details
def _picture_role(mode: str, picture_index: int) -> str:
    if mode == "i2va":
        return "the target video's first frame"
    if mode == "l2va":
        return "the target video's last frame"
    if mode == "fl2va":
        return "the target video's first frame" if picture_index == 1 else "the target video's last frame"
    return "a general visual reference whose role follows the user request"
def _sampling_settings(think_mode: bool) -> dict[str, float | int]:
    if think_mode:
        return {
            "temperature": 1.0,
            "top_p": 0.95,
            "top_k": 20,
            "min_p": 0.0,
            "presence_penalty": 0.0,
            "repetition_penalty": 1.0,
        }
    return {
        "temperature": 0.7,
        "top_p": 0.8,
        "top_k": 20,
        "min_p": 0.0,
        "presence_penalty": 1.5,
        "repetition_penalty": 1.0,
    }
def _user_content(
    prompt: str,
    duration: float,
    images: list,
    video_details: list[tuple],
    mode: str,
) -> tuple[list[dict[str, object]], str]:
    content: list[dict[str, object]] = [
        text_content(f"User request:\n{prompt}\n\nTarget duration: {duration:.2f} seconds."),
    ]
    assets = []
    for picture_index, image in enumerate(images, 1):
        role = _picture_role(mode, picture_index)
        content.append(text_content(f"<Picture {picture_index}>: connected as {role}."))
        content.append(image_content(image))
        assets.append(f"Picture {picture_index}={role}")
    for video_index, details in enumerate(video_details, 1):
        frames, frame_count, source_duration, sample_groups = details
        sampled_frame_count = sum(len(indices) for indices in sample_groups)
        content.append(
            text_content(
                f"<Video {video_index}>: {frame_count} ordered frames at {REFERENCE_VIDEO_FPS:.3f} fps "
                f"({source_duration:.2f} seconds), represented by {sampled_frame_count} sampled frames "
                f"grouped into {len(sample_groups)} chronological one-second contact sheets."
            )
        )
        for second_index, indices in enumerate(sample_groups):
            timestamps = ", ".join(
                f"{frame_index / REFERENCE_VIDEO_FPS:.3f}s" for frame_index in indices
            )
            content.append(
                text_content(
                    f"<Video {video_index}> second {second_index + 1}/{len(sample_groups)} contact sheet. "
                    f"Read cells in row-major chronological order at: {timestamps}."
                )
            )
            content.append(image_grid_content(frames, indices))
        assets.append(
            f"Video {video_index}={source_duration:.2f}s/{sampled_frame_count} sampled frames"
        )
    return content, ", ".join(assets) if assets else "none"
def _remote_size(url: str) -> int | None:
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "ComfyUI-QwenH3Prompt/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            value = response.headers.get("Content-Length")
            return int(value) if value else None
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError):
        return None
def _download_model(filename: str, url: str) -> str:
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    target = MODEL_DIR / filename
    partial = target.with_suffix(target.suffix + ".part")
    expected_size = _remote_size(url)
    if target.is_file():
        local_size = target.stat().st_size
        if expected_size is None or local_size == expected_size:
            LOGGER.info("[Qwen H3 Downloader] Already present: %s (%d bytes)", target, local_size)
            return f"OK: {filename} already exists"
        LOGGER.warning(
            "[Qwen H3 Downloader] Existing file has unexpected size; redownloading | file=%s | local=%d | remote=%s",
            target, local_size, expected_size,
        )
        target.unlink()
    downloaded = partial.stat().st_size if partial.is_file() else 0
    headers = {"User-Agent": "ComfyUI-QwenH3Prompt/1.0"}
    if downloaded:
        headers["Range"] = f"bytes={downloaded}-"
    request = urllib.request.Request(url, headers=headers)
    LOGGER.info(
        "[Qwen H3 Downloader] Download started | file=%s | resume_from=%d | destination=%s",
        filename, downloaded, target,
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            # A server returning 200 ignored our Range request. Restart instead of appending duplicates.
            if downloaded and getattr(response, "status", None) != 206:
                downloaded = 0
                partial.unlink(missing_ok=True)
                return _download_model(filename, url)
            mode = "ab" if downloaded else "wb"
            last_logged_percent = -1
            with partial.open(mode) as output:
                while True:
                    chunk = response.read(DOWNLOAD_CHUNK_SIZE)
                    if not chunk:
                        break
                    output.write(chunk)
                    downloaded += len(chunk)
                    if expected_size:
                        percent = int(downloaded * 100 / expected_size)
                        if percent >= last_logged_percent + 5:
                            LOGGER.info(
                                "[Qwen H3 Downloader] %s: %d%% (%.2f / %.2f GiB)",
                                filename, percent, downloaded / 2**30, expected_size / 2**30,
                            )
                            last_logged_percent = percent
    except Exception:
        LOGGER.exception(
            "[Qwen H3 Downloader] Download interrupted. Partial file kept for resume: %s", partial
        )
        raise
    final_size = partial.stat().st_size
    if expected_size is not None and final_size != expected_size:
        raise RuntimeError(
            f"Incomplete download for {filename}: got {final_size} bytes, expected {expected_size}. "
            f"Partial file was kept at {partial}."
        )
    partial.replace(target)
    LOGGER.info("[Qwen H3 Downloader] Download complete: %s", target)
    return f"DOWNLOADED: {filename}"
def _ensure_model_available(filename: str) -> Path:
    """Resolve a GGUF model, downloading a known default lazily when absent."""
    try:
        return _resolve_model(filename)
    except FileNotFoundError:
        url = MODEL_DOWNLOADS.get(filename)
        if url is None:
            raise FileNotFoundError(
                f"GGUF model {filename!r} is missing from {MODEL_DIR} and no automatic "
                "download URL is configured for it."
            )
    # API executions can arrive concurrently. Only one request may download/check
    # the missing file at a time; waiting requests reuse the completed download.
    with DOWNLOAD_LOCK:
        try:
            return _resolve_model(filename)
        except FileNotFoundError:
            LOGGER.info(
                "[Qwen H3] Required model is missing; starting on-demand download | model=%s",
                filename,
            )
            _download_model(filename, url)
            return _resolve_model(filename)
class QwenH3Prompt(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        models = _model_options(False)
        projectors = _model_options(True)
        return io.Schema(
            node_id="QwenH3PromptLocal",
            display_name="Qwen H3 Prompt (Local)",
            category="😺dzNodes/Qwen_H3_Prompt",
            description="Runs a local llama.cpp server with Qwen3.8 and a discovered H3 Skill from the bundled or custom Skill directories.",
            inputs=[
                io.String.Input(
                    "prompt",
                    multiline=True,
                    dynamic_prompts=True,
                    default="Describe the video or production task.",
                ),
                io.Combo.Input(
                    "skill",
                    options=["auto", *SKILL_NAMES],
                    default="auto",
                    tooltip="Auto routes across bundled and custom Skills, or choose one directly.",
                ),
                io.Float.Input(
                    "duration",
                    default=10.0,
                    min=1.0,
                    max=60.0,
                    step=0.1,
                    tooltip="Target H3 video duration in seconds.",
                ),
                io.Combo.Input(
                    "llm_model",
                    options=models,
                    default=DEFAULT_MODEL if DEFAULT_MODEL in models else models[0],
                    tooltip="GGUF language model from ComfyUI/models/LLM/Qwen3.8.",
                ),
                io.Combo.Input(
                    "vision_model",
                    options=projectors,
                    default=DEFAULT_MMPROJ if DEFAULT_MMPROJ in projectors else projectors[0],
                    tooltip="GGUF multimodal projector from ComfyUI/models/LLM/Qwen3.8.",
                ),
                io.Boolean.Input(
                    "think_mode",
                    display_name="think_mode",
                    default=False,
                    tooltip="Off uses Qwen's official instruct settings. On enables thinking and uses Qwen's official thinking settings.",
                ),
                io.Combo.Input(
                    "reasoning_effort",
                    options=["low", "medium", "xhigh"],
                    default="medium",
                    tooltip="Applied only in thinking mode. Medium is the balanced RTX 3090 preset.",
                ),
                io.Int.Input(
                    "seed",
                    default=0,
                    min=0,
                    max=0xFFFFFFFFFFFFFFFF,
                    step=1,
                    control_after_generate=True,
                    tooltip="ComfyUI seed. It is mapped deterministically to llama.cpp's 32-bit seed range.",
                ),
                io.Int.Input(
                    "max_tokens",
                    default=8192,
                    min=256,
                    max=8192,
                    step=128,
                    tooltip="Maximum generated tokens, including thinking when thinking mode is enabled.",
                ),
                io.Int.Input(
                    "video_sample_frames_per_sec",
                    default=2,
                    min=1,
                    max=8,
                    step=1,
                    advanced=True,
                    tooltip="Frames sampled from each second of every reference video. Frames from the same second are sent as one chronological contact sheet.",
                ),
                io.Boolean.Input(
                    "force_unload_model",
                    display_name="force unload model",
                    default=True,
                    tooltip="On stops the node's llama.cpp server and clears its VRAM and system memory after each run. Errors always force an unload.",
                ),
                io.Autogrow.Input(
                    "reference_images",
                    optional=True,
                    template=io.Autogrow.TemplatePrefix(
                        input=io.Image.Input(
                            "reference_image",
                            tooltip="One reference image. State first-frame or last-frame intent explicitly in the prompt when needed.",
                        ),
                        prefix="reference_image_",
                        min=0,
                        max=9,
                    ),
                ),
                io.Autogrow.Input(
                    "reference_videos",
                    optional=True,
                    template=io.Autogrow.TemplatePrefix(
                        input=io.Image.Input(
                            "reference_video",
                            tooltip="Ordered video frames as an IMAGE batch, compatible with VHS Load Video.",
                        ),
                        prefix="reference_video_",
                        min=0,
                        max=3,
                    ),
                ),
            ],
            outputs=[
                io.String.Output("h3_prompt"),
                io.String.Output("selected_skill"),
                io.String.Output("detected_mode"),
            ],
            hidden=[io.Hidden.unique_id],
        )
    @classmethod
    def execute(
        cls,
        prompt,
        skill,
        duration,
        llm_model,
        vision_model,
        think_mode,
        reasoning_effort,
        seed,
        max_tokens,
        video_sample_frames_per_sec,
        force_unload_model,
        reference_images=None,
        reference_videos=None,
    ) -> io.NodeOutput:
        total_started = time.perf_counter()
        unique_id = str(cls.hidden.unique_id)

        def send_stream_event(event_type: str, **payload) -> None:
            try:
                PromptServer.instance.send_sync(
                    "qwen_h3_stream",
                    {"node_id": unique_id, "type": event_type, **payload},
                )
            except Exception as stream_error:  # noqa: BLE001
                LOGGER.debug(
                    "[Qwen H3] Frontend stream event failed | %s: %s",
                    type(stream_error).__name__,
                    stream_error,
                )

        def on_inference_delta(delta: str) -> None:
            send_stream_event("delta", stage="inference", delta=delta)

        def on_repair_delta(delta: str) -> None:
            send_stream_event("delta", stage="repair", delta=delta)

        send_stream_event("reset", stage="preparing")
        content = None
        messages = None
        repair_messages = None
        LOGGER.info(
            "[Qwen H3] Node execution started | skill=%s | think_mode=%s | seed=%d | force_unload_model=%s",
            skill,
            think_mode,
            seed,
            force_unload_model,
        )
        try:
            if not prompt.strip():
                raise ValueError("prompt must not be empty")
            stage_started = time.perf_counter()
            LOGGER.info("[Qwen H3] Preparing multimodal input")
            images = _autogrow_values(reference_images)
            videos = _autogrow_values(reference_videos)
            _validate_reference_images(images)
            video_details = _reference_video_details(
                videos,
                video_sample_frames_per_sec,
            )
            mode = detect_h3_mode(len(images), len(videos))
            # Lazy model acquisition for API/headless execution. Nothing is downloaded
            # at startup: missing known defaults are fetched only when this node runs.
            model = _ensure_model_available(llm_model)
            projector = _ensure_model_available(vision_model)
            settings = _sampling_settings(think_mode)
            LOGGER.info(
                "[Qwen H3] Sampling configuration selected | mode=%s | temperature=%.2f | top_p=%.2f | top_k=%d | min_p=%.2f | presence_penalty=%.2f | repetition_penalty=%.2f",
                "thinking" if think_mode else "instruct",
                settings["temperature"],
                settings["top_p"],
                settings["top_k"],
                settings["min_p"],
                settings["presence_penalty"],
                settings["repetition_penalty"],
            )
            LOGGER.info(
                "[Qwen H3] Multimodal input parsed | images=%d | videos=%d | initial_mode=%s | elapsed %.2f s",
                len(images),
                len(videos),
                mode or "automatic",
                time.perf_counter() - stage_started,
            )
            with INFERENCE_LOCK:
                stage_started = time.perf_counter()
                LOGGER.info("[Qwen H3] Unloading ComfyUI models for Qwen")
                comfy.model_management.unload_all_models()
                comfy.model_management.soft_empty_cache()
                LOGGER.info(
                    "[Qwen H3] ComfyUI model cleanup complete | elapsed %.2f s",
                    time.perf_counter() - stage_started,
                )
                stage_started = time.perf_counter()
                LOGGER.info(
                    "[Qwen H3] Model loading started | model=%s | mmproj=%s",
                    model.name,
                    projector.name,
                )
                server, loaded_new = SERVER_MANAGER.acquire(model, projector)
                LOGGER.info(
                    "[Qwen H3] Model loading complete | %s | platform=%s | backend=%s | port=%d | elapsed %.2f s",
                    "new model loaded" if loaded_new else "resident model reused",
                    server.runtime_spec.platform_name,
                    server.backend,
                    server.port,
                    time.perf_counter() - stage_started,
                )
                if mode is None:
                    stage_started = time.perf_counter()
                    LOGGER.info(
                        "[Qwen H3] Starting automatic H3 mode routing | images=%d",
                        len(images),
                    )
                    mode_selection, mode_usage = server.chat(
                        mode_router_prompt(prompt, len(images)),
                        seed=seed,
                        max_tokens=16,
                        temperature=0.0,
                        top_p=1.0,
                        top_k=1,
                        min_p=0.0,
                        presence_penalty=0.0,
                        repetition_penalty=1.0,
                        think_mode=False,
                        reasoning_effort="low",
                    )
                    mode = parse_mode_selection(mode_selection, len(images))
                    LOGGER.info(
                        "[Qwen H3] H3 mode routing complete | selected=%s | %s | elapsed %.2f s",
                        mode,
                        _usage_summary(mode_usage),
                        time.perf_counter() - stage_started,
                    )
                else:
                    LOGGER.info(
                        "[Qwen H3] H3 mode routing result | selected=%s (deterministic from media counts)",
                        mode,
                    )
                stage_started = time.perf_counter()
                content, asset_summary = _user_content(
                    prompt,
                    duration,
                    images,
                    video_details,
                    mode,
                )
                LOGGER.info(
                    "[Qwen H3] Multimodal input ready | mode=%s | assets=%s | elapsed %.2f s",
                    mode,
                    asset_summary,
                    time.perf_counter() - stage_started,
                )
                selected = skill
                if selected == "auto":
                    stage_started = time.perf_counter()
                    LOGGER.info("[Qwen H3] Starting automatic Skill routing")
                    selection, routing_usage = server.chat(
                        router_prompt(prompt, mode, asset_summary),
                        seed=seed,
                        max_tokens=48,
                        temperature=0.0,
                        top_p=1.0,
                        top_k=1,
                        min_p=0.0,
                        presence_penalty=0.0,
                        repetition_penalty=1.0,
                        think_mode=False,
                        reasoning_effort="low",
                    )
                    selected = parse_skill_selection(selection)
                    LOGGER.info(
                        "[Qwen H3] Skill routing complete | selected=%s | %s | elapsed %.2f s",
                        selected,
                        _usage_summary(routing_usage),
                        time.perf_counter() - stage_started,
                    )
                else:
                    LOGGER.info(
                        "[Qwen H3] Skill routing result | selected=%s (manual)",
                        selected,
                    )
                messages = [
                    {
                        "role": "system",
                        "content": system_prompt(selected, mode, duration),
                    },
                    {"role": "user", "content": content},
                ]
                stage_started = time.perf_counter()
                LOGGER.info(
                    "[Qwen H3] Inference started | mode=%s | skill=%s | max_tokens=%d",
                    mode,
                    selected,
                    max_tokens,
                )
                send_stream_event("start", stage="inference", skill=selected, mode=mode)
                result, inference_usage = server.chat(
                    messages,
                    seed=seed,
                    max_tokens=max_tokens,
                    think_mode=think_mode,
                    reasoning_effort=reasoning_effort,
                    on_delta=on_inference_delta,
                    **settings,
                )
                LOGGER.info(
                    "[Qwen H3] Inference complete | %s | elapsed %.2f s",
                    _usage_summary(inference_usage),
                    time.perf_counter() - stage_started,
                )
                LOGGER.info("[Qwen H3] Validating node output")
                issues = output_issues(
                    result,
                    mode,
                    duration,
                    skill=selected,
                )
                if issues:
                    LOGGER.warning(
                        "[Qwen H3] Detected %d output issue(s); starting automatic repair: %s",
                        len(issues),
                        "; ".join(issues),
                    )
                    stage_started = time.perf_counter()
                    repair_messages = [
                        *messages,
                        {"role": "assistant", "content": result},
                        {
                            "role": "user",
                            "content": (
                                "Repair the output and return the complete requested content only. "
                                "Do not add a wrapper or impose an output schema that the selected Skill does not "
                                "request. Problems: " + "; ".join(issues)
                            ),
                        },
                    ]
                    send_stream_event(
                        "start", stage="repair", skill=selected, mode=mode, reset_text=True
                    )
                    result, repair_usage = server.chat(
                        repair_messages,
                        seed=seed,
                        max_tokens=max_tokens,
                        think_mode=think_mode,
                        reasoning_effort=reasoning_effort,
                        on_delta=on_repair_delta,
                        **settings,
                    )
                    LOGGER.info(
                        "[Qwen H3] Automatic repair inference complete | %s | elapsed %.2f s",
                        _usage_summary(repair_usage),
                        time.perf_counter() - stage_started,
                    )
                    remaining = output_issues(
                        result,
                        mode,
                        duration,
                        skill=selected,
                    )
                    if remaining:
                        raise RuntimeError(
                            f"Qwen output still fails the {selected} output contract: "
                            + "; ".join(remaining)
                        )
                LOGGER.info("[Qwen H3] Node output validation passed")
                content = None
                messages = None
                repair_messages = None
                if force_unload_model:
                    stage_started = time.perf_counter()
                    LOGGER.info("[Qwen H3] Force-releasing node model, VRAM, and system memory")
                    _release_node_resources()
                    LOGGER.info(
                        "[Qwen H3] Forced resource release complete | elapsed %.2f s",
                        time.perf_counter() - stage_started,
                    )
                else:
                    LOGGER.info(
                        "[Qwen H3] Model unload disabled; the bundled llama.cpp model will remain resident for reuse"
                    )
            _success(
                f"[Qwen H3] Execution completed successfully | skill={selected} | mode={mode} | "
                f"total elapsed {time.perf_counter() - total_started:.2f} s"
            )
            send_stream_event(
                "complete", stage="complete", text=result, skill=selected, mode=mode,
                elapsed=time.perf_counter() - total_started,
            )
            return io.NodeOutput(result, selected, mode)
        except Exception as error:
            content = None
            messages = None
            repair_messages = None
            try:
                with INFERENCE_LOCK:
                    _release_node_resources()
            except Exception as cleanup_error:  # noqa: BLE001
                _failure(
                    f"[Qwen H3] Cleanup after failure also failed: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )
            _failure(
                f"[Qwen H3] Execution failed | {type(error).__name__}: {error} | "
                f"total elapsed {time.perf_counter() - total_started:.2f} s"
            )
            send_stream_event(
                "error", stage="error", message=f"{type(error).__name__}: {error}"
            )
            raise
class QwenH3PromptExtension(ComfyExtension):
    async def get_node_list(self):
        return [QwenH3Prompt]
async def comfy_entrypoint() -> QwenH3PromptExtension:
    return QwenH3PromptExtension()

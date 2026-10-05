"""Tests for MiniMax H3 native latent-tail continuation."""

import asyncio
import inspect
from collections.abc import Callable
from types import ModuleType
from typing import Any

import pytest
import torch
from comfy.nested_tensor import NestedTensor

HEIGHT = 2
WIDTH = 2


def make_cumulative_latent(
    nodes_module: ModuleType,
    frame_count: int,
    *,
    dtype: torch.dtype = torch.float32,
) -> dict[str, NestedTensor]:
    """Create a small, valid cumulative H3 AV latent with identifiable values."""
    video_t = nodes_module.video_latent_t(frame_count)
    audio_t = nodes_module.audio_t_at_frame_boundary(frame_count)
    video = torch.arange(
        1 * 24 * video_t * HEIGHT * WIDTH,
        dtype=dtype,
    ).reshape(1, 24, video_t, HEIGHT, WIDTH)
    audio = torch.arange(
        1 * 32 * 2 * audio_t,
        dtype=dtype,
    ).reshape(1, 32, 2, audio_t)
    return {"samples": NestedTensor((video, audio))}


def sampled_from_window(
    window: dict[str, Any],
    overlap_video_t: int,
    overlap_audio_t: int,
) -> dict[str, NestedTensor]:
    """Create a synthetic sampler result with nonzero newly generated regions."""
    window_video, window_audio = window["samples"].tensors
    sampled_video = window_video.clone()
    sampled_audio = window_audio.clone()
    sampled_video[:, :, overlap_video_t:] = 7
    sampled_audio[..., overlap_audio_t:] = 11
    return {"samples": NestedTensor((sampled_video, sampled_audio))}


def replace_streams(
    latent: dict[str, Any],
    transform: Callable[[torch.Tensor, torch.Tensor], tuple[Any, Any]],
) -> dict[str, NestedTensor]:
    """Return a latent whose streams have been transformed for a rejection test."""
    video, audio = latent["samples"].tensors
    changed_video, changed_audio = transform(video, audio)
    return {"samples": NestedTensor((changed_video, changed_audio))}


def make_conditioning(metadata: dict[str, Any] | None = None) -> list[list[Any]]:
    """Create a minimal canonical ComfyUI CONDITIONING value."""
    return [[torch.zeros((1, 1, 4)), dict(metadata or {})]]


def test_v3_entrypoint_and_exact_registration(package_module: ModuleType) -> None:
    """The V3 entrypoint registers exactly the three nodes used by the example."""
    extension = asyncio.run(package_module.comfy_entrypoint())
    assert isinstance(extension, package_module.MiniMaxH3ContinuationExtension)
    assert asyncio.run(extension.get_node_list()) == [
        package_module.MiniMaxH3GuidedContinuationWindow,
        package_module.MiniMaxH3LatentTailGuide,
        package_module.MiniMaxH3AppendContinuation,
    ]


@pytest.mark.parametrize(
    ("node_name", "expected_inputs"),
    [
        (
            "MiniMaxH3GuidedContinuationWindow",
            ["previous_av_latent", "overlap_frames", "extension_frames"],
        ),
        (
            "MiniMaxH3LatentTailGuide",
            [
                "positive",
                "previous_av_latent",
                "target_av_latent",
                "overlap_video_tokens",
                "overlap_audio_tokens",
            ],
        ),
        (
            "MiniMaxH3AppendContinuation",
            [
                "previous_av_latent",
                "sampled_window",
                "overlap_video_tokens",
                "overlap_audio_tokens",
                "transition_video_tokens",
                "transition_audio_tokens",
            ],
        ),
    ],
)
def test_schema_input_order_matches_execute_signature(
    nodes_module: ModuleType,
    node_name: str,
    expected_inputs: list[str],
) -> None:
    """V3 schema ordering remains synchronized with each execute signature."""
    node = getattr(nodes_module, node_name)
    schema = node.define_schema()
    schema.validate()
    assert [node_input.id for node_input in schema.inputs] == expected_inputs
    assert list(inspect.signature(node.execute).parameters) == expected_inputs


def test_guided_window_is_fresh_unmasked_and_globally_aligned(
    nodes_module: ModuleType,
) -> None:
    """The native-guide target contains only fresh zeros and hidden-overlap metadata."""
    previous = make_cumulative_latent(nodes_module, 124, dtype=torch.float64)
    previous_video, previous_audio = previous["samples"].tensors

    output = nodes_module.MiniMaxH3GuidedContinuationWindow.execute(previous, 22, 119)
    (
        target,
        window_length,
        overlap_video_t,
        overlap_audio_t,
        transition_video_t,
        transition_audio_t,
    ) = output.result
    target_video, target_audio = target["samples"].tensors

    assert window_length == 141
    assert overlap_video_t == 7
    assert overlap_audio_t == 37
    assert transition_video_t == 0
    assert transition_audio_t == 0
    assert target_video.shape == (1, 24, 42, HEIGHT, WIDTH)
    assert target_audio.shape == (1, 32, 2, 235)
    assert target_video.dtype == previous_video.dtype
    assert target_audio.dtype == previous_audio.dtype
    assert target_video.device == previous_video.device
    assert target_audio.device == previous_audio.device
    assert torch.count_nonzero(target_video) == 0
    assert torch.count_nonzero(target_audio) == 0
    assert "noise_mask" not in target


def test_guided_window_preserves_global_audio_rounding(nodes_module: ModuleType) -> None:
    """Fresh windows use source-global boundaries when an extension needs an extra audio tick."""
    previous = make_cumulative_latent(nodes_module, 5)

    target, window_length, _, overlap_audio_t, _, _ = (
        nodes_module.MiniMaxH3GuidedContinuationWindow.execute(previous, 5, 119).result
    )

    assert window_length == 124
    assert overlap_audio_t == 8
    assert target["samples"].tensors[1].shape[-1] == 207


def test_latent_tail_guide_adds_direct_synchronized_av_context(
    nodes_module: ModuleType,
) -> None:
    """The guide copies the exact previous AV tail into native MiniMax keyframe metadata."""
    previous = make_cumulative_latent(nodes_module, 124)
    previous_video, previous_audio = previous["samples"].tensors
    target, _, video_overlap, audio_overlap, _, _ = (
        nodes_module.MiniMaxH3GuidedContinuationWindow.execute(previous, 22, 119).result
    )
    terminal = {
        "resolved_frame_index": 140,
        "latent": torch.ones((1, 24, 1, HEIGHT, WIDTH)),
    }
    positive = make_conditioning({"marker": "preserved", "minimax_keyframes": [terminal]})

    guided = nodes_module.MiniMaxH3LatentTailGuide.execute(
        positive,
        previous,
        target,
        video_overlap,
        audio_overlap,
    ).result[0]
    metadata = guided[0][1]
    keyframes = metadata["minimax_keyframes"]
    tail = keyframes[0]

    assert guided is not positive
    assert metadata["marker"] == "preserved"
    assert len(keyframes) == 2
    assert keyframes[-1] is not terminal
    assert keyframes[-1]["resolved_frame_index"] == 140
    assert tail["resolved_frame_index"] == 0
    assert tail["latent"].shape == (1, 24, 7, HEIGHT, WIDTH)
    assert tail["audio_latent"].shape == (1, 32, 2, 37)
    assert torch.equal(tail["latent"], previous_video[:, :, -7:])
    assert torch.equal(tail["audio_latent"], previous_audio[..., -37:])
    assert (
        tail["latent"].untyped_storage().data_ptr() != previous_video.untyped_storage().data_ptr()
    )
    assert (
        tail["audio_latent"].untyped_storage().data_ptr()
        != previous_audio.untyped_storage().data_ptr()
    )
    assert positive[0][1]["minimax_keyframes"] == [terminal]


def test_latent_tail_guide_builds_the_upstream_native_packed_layout(
    nodes_module: ModuleType,
) -> None:
    """Current upstream H3 packs the direct video and audio guide as conditioning rows."""
    from comfy.ldm.minimax.model import PackedLayout

    previous = make_cumulative_latent(nodes_module, 124)
    target, _, video_overlap, audio_overlap, _, _ = (
        nodes_module.MiniMaxH3GuidedContinuationWindow.execute(previous, 22, 119).result
    )
    target_video, target_audio = target["samples"].tensors
    guided = nodes_module.MiniMaxH3LatentTailGuide.execute(
        make_conditioning(),
        previous,
        target,
        video_overlap,
        audio_overlap,
    ).result[0]
    keyframes = guided[0][1]["minimax_keyframes"]

    layout = PackedLayout(
        4,
        target_video.shape[2],
        target_video.shape[3],
        target_video.shape[4],
        target_audio.shape[-1],
        keyframes=keyframes,
    )
    segment_lengths = {
        kind: stop - start for start, stop, kind in layout.segments if kind.startswith("cond")
    }

    assert segment_lengths == {"cond": 7, "cond_audio": 74}


def test_latent_tail_guide_rejects_existing_opening_anchor(nodes_module: ModuleType) -> None:
    """A decoded first-frame guide cannot silently compete with the latent-tail guide."""
    previous = make_cumulative_latent(nodes_module, 124)
    target, _, video_overlap, audio_overlap, _, _ = (
        nodes_module.MiniMaxH3GuidedContinuationWindow.execute(previous, 22, 119).result
    )
    positive = make_conditioning(
        {
            "minimax_keyframes": [
                {
                    "resolved_frame_index": 0,
                    "latent": torch.zeros((1, 24, 1, HEIGHT, WIDTH)),
                }
            ]
        }
    )

    with pytest.raises(ValueError, match="disconnect first_frame"):
        nodes_module.MiniMaxH3LatentTailGuide.execute(
            positive,
            previous,
            target,
            video_overlap,
            audio_overlap,
        )


def test_latent_tail_guide_rejects_target_geometry_mismatch(nodes_module: ModuleType) -> None:
    """Guide and target video canvases must match before native H3 row packing."""
    previous = make_cumulative_latent(nodes_module, 124)
    target, _, video_overlap, audio_overlap, _, _ = (
        nodes_module.MiniMaxH3GuidedContinuationWindow.execute(previous, 22, 119).result
    )
    mismatched = replace_streams(
        target,
        lambda video, audio: (
            torch.zeros((1, 24, video.shape[2], HEIGHT, WIDTH + 1)),
            audio,
        ),
    )

    with pytest.raises(ValueError, match="spatial"):
        nodes_module.MiniMaxH3LatentTailGuide.execute(
            make_conditioning(),
            previous,
            mismatched,
            video_overlap,
            audio_overlap,
        )


def test_guided_append_discards_complete_hidden_overlap(nodes_module: ModuleType) -> None:
    """Zero transition counts preserve all prior tokens and append only new sampled tokens."""
    previous = make_cumulative_latent(nodes_module, 124)
    previous_video, previous_audio = previous["samples"].tensors
    target, _, video_overlap, audio_overlap, video_transition, audio_transition = (
        nodes_module.MiniMaxH3GuidedContinuationWindow.execute(previous, 22, 119).result
    )
    target_video, target_audio = target["samples"].tensors
    sampled_video = target_video.clone()
    sampled_audio = target_audio.clone()
    sampled_video[:, :, :video_overlap] = -5
    sampled_audio[..., :audio_overlap] = -7
    sampled_video[:, :, video_overlap:] = 13
    sampled_audio[..., audio_overlap:] = 17
    sampled = {"samples": NestedTensor((sampled_video, sampled_audio))}

    cumulative, total_length = nodes_module.MiniMaxH3AppendContinuation.execute(
        previous,
        sampled,
        video_overlap,
        audio_overlap,
        video_transition,
        audio_transition,
    ).result
    output_video, output_audio = cumulative["samples"].tensors

    assert total_length == 243
    assert torch.equal(output_video[:, :, : previous_video.shape[2]], previous_video)
    assert torch.equal(output_audio[..., : previous_audio.shape[-1]], previous_audio)
    assert torch.all(output_video[:, :, previous_video.shape[2] :] == 13)
    assert torch.all(output_audio[..., previous_audio.shape[-1] :] == 17)
    assert torch.count_nonzero(output_video == -5) == 0
    assert torch.count_nonzero(output_audio == -7) == 0


def test_guided_append_handles_alternate_global_audio_phase(nodes_module: ModuleType) -> None:
    """A 36-tick guide and 199-tick suffix still land on the cumulative audio boundary."""
    previous = make_cumulative_latent(nodes_module, 56)
    previous_video, previous_audio = previous["samples"].tensors
    target, _, video_overlap, audio_overlap, video_transition, audio_transition = (
        nodes_module.MiniMaxH3GuidedContinuationWindow.execute(previous, 22, 119).result
    )
    target_video, target_audio = target["samples"].tensors
    assert audio_overlap == 36
    assert target_audio.shape[-1] - audio_overlap == 199
    sampled = {"samples": NestedTensor((target_video + 3, target_audio + 5))}

    cumulative, total_length = nodes_module.MiniMaxH3AppendContinuation.execute(
        previous,
        sampled,
        video_overlap,
        audio_overlap,
        video_transition,
        audio_transition,
    ).result
    output_video, output_audio = cumulative["samples"].tensors

    assert total_length == 175
    assert output_video.shape[2] == 52
    assert output_audio.shape[-1] == 292
    assert torch.equal(output_video[:, :, : previous_video.shape[2]], previous_video)
    assert torch.equal(output_audio[..., : previous_audio.shape[-1]], previous_audio)


@pytest.mark.parametrize(
    "latent_factory",
    [
        pytest.param(lambda latent: {}, id="missing-samples"),
        pytest.param(lambda latent: {"samples": latent["samples"].tensors[0]}, id="plain-tensor"),
        pytest.param(
            lambda latent: {"samples": NestedTensor((latent["samples"].tensors[0],))},
            id="missing-stream",
        ),
        pytest.param(
            lambda latent: {
                "samples": NestedTensor(
                    (
                        *latent["samples"].tensors,
                        latent["samples"].tensors[1],
                    )
                )
            },
            id="extra-stream",
        ),
        pytest.param(
            lambda latent: {"samples": NestedTensor(tuple(reversed(latent["samples"].tensors)))},
            id="reversed-streams",
        ),
        pytest.param(
            lambda latent: replace_streams(
                latent,
                lambda video, audio: (video.squeeze(0), audio),
            ),
            id="wrong-video-dimensions",
        ),
        pytest.param(
            lambda latent: replace_streams(
                latent,
                lambda video, audio: (video, audio.squeeze(0)),
            ),
            id="wrong-audio-dimensions",
        ),
        pytest.param(
            lambda latent: replace_streams(
                latent,
                lambda video, audio: (video[:, :23], audio),
            ),
            id="wrong-video-channels",
        ),
        pytest.param(
            lambda latent: replace_streams(
                latent,
                lambda video, audio: (video, audio[:, :31]),
            ),
            id="wrong-audio-latent-channels",
        ),
        pytest.param(
            lambda latent: replace_streams(
                latent,
                lambda video, audio: (video, audio[:, :, :1]),
            ),
            id="wrong-audio-channels",
        ),
        pytest.param(
            lambda latent: replace_streams(
                latent,
                lambda video, audio: (
                    video.expand(2, -1, -1, -1, -1),
                    audio.expand(2, -1, -1, -1),
                ),
            ),
            id="batch-greater-than-one",
        ),
        pytest.param(
            lambda latent: replace_streams(
                latent,
                lambda video, audio: (video[:, :, :-1], audio),
            ),
            id="invalid-video-grid",
        ),
        pytest.param(
            lambda latent: replace_streams(
                latent,
                lambda video, audio: (video, audio[..., :-1]),
            ),
            id="invalid-cumulative-audio-length",
        ),
    ],
)
def test_rejects_invalid_cumulative_latents(
    nodes_module: ModuleType,
    latent_factory: Callable[[dict[str, Any]], dict[str, Any]],
) -> None:
    """Malformed or incompatible cumulative latent layouts fail at the node boundary."""
    valid = make_cumulative_latent(nodes_module, 124)
    with pytest.raises((TypeError, ValueError)):
        nodes_module.MiniMaxH3GuidedContinuationWindow.execute(latent_factory(valid), 22, 119)


@pytest.mark.parametrize(
    ("overlap_frames", "extension_frames"),
    [(4, 119), (21, 119), (141, 119), (22, 0), (22, 118)],
)
def test_rejects_invalid_guided_window_lengths(
    nodes_module: ModuleType,
    overlap_frames: int,
    extension_frames: int,
) -> None:
    """Guided windows reject misaligned or impossible overlap and extension requests."""
    previous = make_cumulative_latent(nodes_module, 124)

    with pytest.raises(ValueError):
        nodes_module.MiniMaxH3GuidedContinuationWindow.execute(
            previous,
            overlap_frames,
            extension_frames,
        )


def prepare_append_inputs(
    nodes_module: ModuleType,
) -> tuple[dict[str, Any], dict[str, Any], int, int, int, int]:
    """Create a valid previous latent and synthetic sampled window for append tests."""
    previous = make_cumulative_latent(nodes_module, 124)
    (
        window,
        _,
        video_overlap,
        audio_overlap,
        video_transition,
        audio_transition,
    ) = nodes_module.MiniMaxH3GuidedContinuationWindow.execute(previous, 22, 119).result
    sampled = sampled_from_window(window, video_overlap, audio_overlap)
    return (
        previous,
        sampled,
        video_overlap,
        audio_overlap,
        video_transition,
        audio_transition,
    )


def test_rejects_append_spatial_mismatch(nodes_module: ModuleType) -> None:
    """Append rejects video windows with different spatial dimensions."""
    previous, sampled, video_overlap, audio_overlap, video_transition, audio_transition = (
        prepare_append_inputs(nodes_module)
    )
    mismatched = replace_streams(
        sampled,
        lambda video, audio: (
            torch.zeros((1, 24, video.shape[2], HEIGHT, WIDTH + 1)),
            audio,
        ),
    )
    with pytest.raises(ValueError, match="spatial"):
        nodes_module.MiniMaxH3AppendContinuation.execute(
            previous,
            mismatched,
            video_overlap,
            audio_overlap,
            video_transition,
            audio_transition,
        )


def test_rejects_append_dtype_mismatch(nodes_module: ModuleType) -> None:
    """Append rejects sampled streams whose dtype differs from the cumulative stream."""
    previous, sampled, video_overlap, audio_overlap, video_transition, audio_transition = (
        prepare_append_inputs(nodes_module)
    )
    mismatched = replace_streams(
        sampled,
        lambda video, audio: (video.to(torch.float64), audio),
    )
    with pytest.raises(TypeError, match="dtype"):
        nodes_module.MiniMaxH3AppendContinuation.execute(
            previous,
            mismatched,
            video_overlap,
            audio_overlap,
            video_transition,
            audio_transition,
        )


def test_rejects_append_device_mismatch(nodes_module: ModuleType) -> None:
    """Append rejects a sampled stream on a different device."""
    previous, sampled, video_overlap, audio_overlap, video_transition, audio_transition = (
        prepare_append_inputs(nodes_module)
    )
    mismatched = replace_streams(
        sampled,
        lambda video, audio: (video.to("meta"), audio),
    )
    with pytest.raises(ValueError, match="device"):
        nodes_module.MiniMaxH3AppendContinuation.execute(
            previous,
            mismatched,
            video_overlap,
            audio_overlap,
            video_transition,
            audio_transition,
        )


@pytest.mark.parametrize(
    ("video_overlap", "audio_overlap"),
    [(0, 65), (47, 65), (12, 0), (12, 263), (11, 65), (12, 64), (17, 65)],
)
def test_rejects_invalid_append_overlap_tokens(
    nodes_module: ModuleType,
    video_overlap: int,
    audio_overlap: int,
) -> None:
    """Append requires in-range overlap counts and a five-token video extension grid."""
    previous, sampled, _, _, video_transition, audio_transition = prepare_append_inputs(
        nodes_module
    )
    with pytest.raises(ValueError):
        nodes_module.MiniMaxH3AppendContinuation.execute(
            previous,
            sampled,
            video_overlap,
            audio_overlap,
            video_transition,
            audio_transition,
        )


@pytest.mark.parametrize(
    ("video_transition", "audio_transition"),
    [(0, 29), (12, 29), (4, 29), (5, 0), (5, 65), (5, 28), (10, 29)],
)
def test_rejects_invalid_append_transition_tokens(
    nodes_module: ModuleType,
    video_transition: int,
    audio_transition: int,
) -> None:
    """Append requires transition counts to be aligned and inside the overlap."""
    previous, sampled, video_overlap, audio_overlap, _, _ = prepare_append_inputs(nodes_module)
    with pytest.raises(ValueError):
        nodes_module.MiniMaxH3AppendContinuation.execute(
            previous,
            sampled,
            video_overlap,
            audio_overlap,
            video_transition,
            audio_transition,
        )


def test_rejects_append_with_wrong_new_audio_length(nodes_module: ModuleType) -> None:
    """Append verifies the completed cumulative audio length at the global boundary."""
    previous, sampled, video_overlap, audio_overlap, video_transition, audio_transition = (
        prepare_append_inputs(nodes_module)
    )
    wrong_audio = replace_streams(
        sampled,
        lambda video, audio: (video, audio[..., :-1]),
    )
    with pytest.raises(ValueError, match="global MiniMax H3 frame boundary"):
        nodes_module.MiniMaxH3AppendContinuation.execute(
            previous,
            wrong_audio,
            video_overlap,
            audio_overlap,
            video_transition,
            audio_transition,
        )


@pytest.mark.parametrize(
    ("frame_count", "video_t"),
    [(5, 2), (39, 12), (124, 37), (243, 72), (362, 107)],
)
def test_temporal_helpers_round_trip(
    nodes_module: ModuleType,
    frame_count: int,
    video_t: int,
) -> None:
    """Local temporal helpers implement the native H3 frame/token grids exactly."""
    assert nodes_module.video_latent_t(frame_count) == video_t
    assert nodes_module.frame_count_from_video_latent_t(video_t) == frame_count


@pytest.mark.parametrize("frame_count", [0, 4, 6, 38])
def test_video_latent_helper_rejects_unaligned_frames(
    nodes_module: ModuleType,
    frame_count: int,
) -> None:
    """Frame-to-token conversion never silently aligns invalid values."""
    with pytest.raises(ValueError):
        nodes_module.video_latent_t(frame_count)

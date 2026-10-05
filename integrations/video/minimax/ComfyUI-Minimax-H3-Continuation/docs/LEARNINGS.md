# Learnings

## Ship only the validated native-guide workflow

Manual workflow testing established the native latent-tail path as the working continuation
approach. The public extension now registers only Guided Continuation Window, Latent Tail Guide,
and Append Continuation—the same three custom nodes used by
`example_workflows/guided_continuation.json`. The superseded masked-overlap and refinement nodes
were removed so users are not directed toward unsuccessful experimental paths.

## Native H3 temporal guides are preferable to masked overlap diffusion

ComfyUI commit `e01fb4c` removed MiniMax H3's first/last-only keyframe restriction and added native
image, clip, and audio guides at arbitrary frame positions. `PackedLayout` now accepts a multi-step
video latent and synchronized audio latent in one `minimax_keyframes` record and places both streams
on the target timeline. This permits a preceding generated AV tail to condition continuation
directly without a VAE round trip or a model/layout monkey patch.

The native-guide path uses a fresh, unmasked continuation target. A 22-frame hidden overlap contains
seven video latent steps and normally 36 or 37 globally aligned audio steps. The whole sampled head
is discarded after sampling; the completed previous latent remains byte-for-byte authoritative, and
only newly generated suffix tokens are appended. The fresh target must retain global audio-boundary
math because a 119-frame extension can contribute either 198 or 199 audio steps depending on the
cumulative starting position.

Opening image conditioning must not compete with the latent tail. The continuation Image to Video
node therefore has no `first_frame` or `last_frame`, and the latent-tail guide rejects an existing
guide whose starting position falls inside the hidden overlap. Fixed-segmentation tests should also
reuse the original seed and initially omit Differential Diffusion and post-hoc refinement so the
native motion-context mechanism is isolated.

## Global audio boundaries are required

MiniMax H3 video uses a fixed 24 fps frame grid while its audio latent uses 40 Hz. Segment-local
rounding loses information about the global timeline and can accumulate a one-tick A/V error.
Continuation preparation therefore computes audio slices from the previous and extended cumulative
frame boundaries. A 39+119-frame working window consequently has 263 audio ticks at some positions
and 264 at others.

The sampled working latent is structurally validated on append, while the previous and completed
cumulative latents are additionally checked against their exact global audio boundary.

## A hard latent splice can expose a continuation seam

Discarding every sampled overlap token and appending only the new region creates a hard boundary
between the old latent trajectory and the continuation. Even when the conditioning image is taken
from the correct overlap boundary, that splice can show up as flashes or garbling after decoding.

An initial mitigation separated the overlap into a stable prefix and a fully resampled transition,
then linearly blended the original and sampled transition after diffusion. It improved the handoff
slightly but still produced visual confetti. Post-sampling interpolation is problematic because two
completed latent trajectories need not remain on the model's learned manifold between endpoints;
an ordinary equal-weight interpolation can also reduce latent variance at the transition midpoint.

The continuation window offers linear and quintic smootherstep noise-mask ramps across the
transition. Linear is now the default: with only 10 video tokens in the recommended 34-frame
transition and 20 common sampling steps, it distributes Differential Diffusion activation
thresholds more evenly. Quintic remains available for comparison, but its endpoint flattening
clusters several token activations into a narrower part of the discrete sampling schedule.
ComfyUI's Differential Diffusion model patch turns either gradient into a progressive denoising
schedule, so transition tokens near the stable prefix remain anchored longer while later tokens
receive progressively more denoising. Append keeps the sampler-produced transition directly rather
than applying another latent interpolation. Video uses its 5-token-per-17-frame grid; audio mask
boundaries are computed on the global 40 Hz timeline so repeated appends cannot accumulate rounding
drift.

MiniMax H3 still receives one global timestep for the full AV latent. Differential Diffusion masks
tokens externally around each model call; it does not provide H3 with native per-token timesteps.
This means a temporal gradient can reduce the splice discontinuity but cannot guarantee that H3
will model a partially clean and partially noisy temporal window without flashing.

Differential Diffusion is a model patch and therefore cannot be carried inside a `LATENT`. The
workflow must pass the loaded H3 model through the built-in Differential Diffusion node before
BasicGuider or KSampler. The continuation node remains responsible for producing aligned nested
video/audio feather masks.

The conditioning image must still be taken from the start of the full overlap. For example, a
73-frame overlap with a 34-frame transition uses image batch index `-73`, not `-34`. A `-56` index
belongs to the alternative 56-frame overlap with a 17-frame transition.

## Uniform low-denoise refinement should precede append

A third sampling pass can regularize a completed continuation window before it is spliced into the
cumulative latent. The refinement input must be the sampled bounded window, not the appended
cumulative latent, so cost remains bounded. Removing the continuation `noise_mask` makes the whole
AV window receive the same low-noise schedule, which is a better match for H3's single global
timestep than applying a second fractional mask.

The refinement transform preserves the exact AV sample objects and removes only `noise_mask` from a
shallow copy of the latent mapping. It validates audio as a bounded slice: depending on its global
start boundary, a 158-frame window can correctly contain either 263 or 264 audio tokens. Applying
the cumulative-latent audio check here would incorrectly reject valid later continuation windows.

Append must consume the refined sampler output while retaining its overlap and transition metadata
from the original continuation-window node. A useful initial refinement schedule is 10 steps at
denoise 0.15 with the ordinary, non-Differential-Diffusion H3 model.

## Registry archives should contain runtime files only

The Comfy CLI builds registry archives from git-tracked files. The project therefore uses
`.comfyignore` to omit tests, development instructions, internal notes, and the development lockfile
while retaining the node implementation, package metadata, README, and license. Registry publishing
also requires an explicit publisher identity and repository URL in `pyproject.toml`.

## The latent-tail guide must be on the active conditioning edge

The direct native-guide workflow was manually validated successfully. The continuation path must
connect the MiniMax H3 Image to Video conditioning through `MiniMaxH3LatentTailGuide` and then into
the continuation `BasicGuider`. Executing the latent-tail guide while leaving `BasicGuider`
connected directly to Image to Video computes the guide but discards its output. In that topology,
the sampler receives an empty target latent with text-only conditioning and generates an unrelated
continuation.

With the guide correctly inserted into the conditioning edge, the hidden latent tail anchors the
new window to the preceding segment without requiring a continuation `first_frame` input. The
141-frame default window discards its 22-frame hidden overlap during append and contributes 119
frames, or approximately five seconds at 24 fps.

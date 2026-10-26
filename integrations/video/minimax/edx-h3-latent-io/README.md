# edx-h3-latent-io

Two ComfyUI nodes that move the MiniMax H3 cumulative audio+video latent (a `NestedTensor`) between ComfyUI jobs, which the stock
`SaveLatent` / `LoadLatent` cannot do. The Studio's Motion Context executor runs one ComfyUI job per window of a continuous video
and passes the latent of everything rendered so far to the next window through these files.

- `EDxH3SaveAVLatent` (`samples`, `name`): writes `<output>/h3-latents/<name>.safetensors` (tensors `video` and `audio`).
- `EDxH3LoadAVLatent` (`name`): reads it back; re-executes when the file changes.

`name` is a plain file name (`[A-Za-z0-9][A-Za-z0-9._-]{0,120}`); anything else is refused, so a workflow cannot reach outside
`h3-latents`. Requires `ComfyUI-Minimax-H3-Continuation`, which consumes the latent.

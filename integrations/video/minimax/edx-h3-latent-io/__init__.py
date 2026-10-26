"""Save/load the MiniMax H3 audio+video latent (a NestedTensor) to disk between ComfyUI jobs.

Experimental helper for the Motion Context continuation test: stock SaveLatent cannot store a NestedTensor.
Files live under ``<output>/h3-latents`` and are addressed by a plain file name.
"""

import hashlib
import os
import re

import safetensors.torch
import torch

import folder_paths
from comfy.nested_tensor import NestedTensor

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")


def _path(name: str) -> str:
    if not _NAME.fullmatch(name or ""):
        raise ValueError("invalid latent name")
    root = os.path.join(folder_paths.get_output_directory(), "h3-latents")
    os.makedirs(root, exist_ok=True)
    return os.path.join(root, name + ".safetensors")


class EDxH3SaveAVLatent:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"samples": ("LATENT",), "name": ("STRING", {"default": "h3-latent"})}}

    RETURN_TYPES = ()
    FUNCTION = "save"
    OUTPUT_NODE = True
    CATEGORY = "edx/h3"

    def save(self, samples, name):
        nested = samples["samples"]
        if not isinstance(nested, NestedTensor) or len(nested.tensors) != 2:
            raise TypeError("expected an H3 audio+video NestedTensor latent")
        video, audio = (t.detach().cpu().contiguous() for t in nested.tensors)
        target = _path(name)
        safetensors.torch.save_file({"video": video, "audio": audio}, target + ".tmp",
                                    metadata={"format": "edx-h3-av-latent/v1"})
        os.replace(target + ".tmp", target)
        return {"ui": {"text": [f"{video.shape} {audio.shape}"]}}


class EDxH3LoadAVLatent:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"name": ("STRING", {"default": "h3-latent"})}}

    RETURN_TYPES = ("LATENT",)
    FUNCTION = "load"
    CATEGORY = "edx/h3"

    def load(self, name):
        data = safetensors.torch.load_file(_path(name), device="cpu")
        return ({"samples": NestedTensor((data["video"], data["audio"]))},)

    @classmethod
    def IS_CHANGED(cls, name):
        stat = os.stat(_path(name))
        return f"{stat.st_mtime_ns}:{stat.st_size}"


NODE_CLASS_MAPPINGS = {"EDxH3SaveAVLatent": EDxH3SaveAVLatent, "EDxH3LoadAVLatent": EDxH3LoadAVLatent}
NODE_DISPLAY_NAME_MAPPINGS = {"EDxH3SaveAVLatent": "EDx H3 Save AV Latent", "EDxH3LoadAVLatent": "EDx H3 Load AV Latent"}

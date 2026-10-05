"""ComfyUI entrypoint for the MiniMax H3 continuation node pack."""

from comfy_api.latest import ComfyExtension

from .continuation_nodes import (
    MiniMaxH3AppendContinuation,
    MiniMaxH3GuidedContinuationWindow,
    MiniMaxH3LatentTailGuide,
)


class MiniMaxH3ContinuationExtension(ComfyExtension):
    """Register the validated MiniMax H3 native-guide continuation nodes."""

    async def get_node_list(self):
        """Return the custom nodes supplied by this extension."""
        return [
            MiniMaxH3GuidedContinuationWindow,
            MiniMaxH3LatentTailGuide,
            MiniMaxH3AppendContinuation,
        ]


async def comfy_entrypoint() -> MiniMaxH3ContinuationExtension:
    """Return the custom-node extension entrypoint."""
    return MiniMaxH3ContinuationExtension()


__all__ = ["comfy_entrypoint"]

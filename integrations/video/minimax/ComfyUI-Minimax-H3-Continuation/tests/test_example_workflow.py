"""Validate the loadable MiniMax H3 latent-tail continuation example."""

import json
from pathlib import Path
from typing import Any

WORKFLOW_PATH = (
    Path(__file__).resolve().parents[1]
    / "example_workflows"
    / "guided_continuation.json"
)


def _load_workflow() -> dict[str, Any]:
    """Load and return the example workflow mapping."""
    with WORKFLOW_PATH.open(encoding="utf-8") as workflow_file:
        return json.load(workflow_file)


def _nodes_by_id(workflow: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """Index workflow nodes by their integer identifier."""
    return {node["id"]: node for node in workflow["nodes"]}


def _accepts_link_type(input_type: str, link_type: str) -> bool:
    """Return whether a ComfyUI input type union accepts the link type."""
    return link_type in input_type.split(",")


def test_example_workflow_links_are_consistent() -> None:
    """Require every serialized link to agree with both endpoint nodes."""
    workflow = _load_workflow()
    nodes = _nodes_by_id(workflow)
    links = workflow["links"]

    assert len(nodes) == len(workflow["nodes"])
    assert workflow["last_node_id"] >= max(nodes)
    assert workflow["last_link_id"] == max(link[0] for link in links)
    assert len({link[0] for link in links}) == len(links)

    for link_id, source_id, source_slot, target_id, target_slot, link_type in links:
        source = nodes[source_id]
        target = nodes[target_id]
        assert source["outputs"][source_slot]["type"] == link_type
        assert _accepts_link_type(target["inputs"][target_slot]["type"], link_type)
        assert link_id in source["outputs"][source_slot]["links"]
        assert target["inputs"][target_slot]["link"] == link_id


def test_example_workflow_uses_the_native_tail_guide() -> None:
    """Require the extension conditioning to pass through the latent-tail guide."""
    workflow = _load_workflow()
    nodes = _nodes_by_id(workflow)
    links = {
        (source_id, source_slot, target_id, target_slot)
        for _, source_id, source_slot, target_id, target_slot, _ in workflow["links"]
    }
    extension = next(
        node for node in nodes.values() if node.get("title") == "Extension video conditioning"
    )
    guide = next(
        node for node in nodes.values() if node["type"] == "MiniMaxH3LatentTailGuide"
    )
    continuation_guider = next(
        node for node in nodes.values() if node.get("title") == "Continuation Basic Guider"
    )

    assert (extension["id"], 0, guide["id"], 0) in links
    assert (guide["id"], 0, continuation_guider["id"], 1) in links
    assert (extension["id"], 0, continuation_guider["id"], 1) not in links


def test_example_workflow_has_safe_prompts_and_no_optional_passes() -> None:
    """Keep the example focused on the minimal two-sampling continuation path."""
    workflow = _load_workflow()
    nodes = workflow["nodes"]
    prompts = [
        node["widgets_values_named"]["prompt"]
        for node in nodes
        if node["type"] == "MiniMaxH3ImageToVideo"
    ]
    node_types = {node["type"] for node in nodes}

    assert len(prompts) == 2
    assert "walk" in prompts[0].casefold()
    assert "hair suddenly turns purple" in prompts[1].casefold()
    assert "MiniMaxH3RefinementWindow" not in node_types
    assert "LoraLoaderModelOnly" not in node_types
    assert "DifferentialDiffusion" not in node_types

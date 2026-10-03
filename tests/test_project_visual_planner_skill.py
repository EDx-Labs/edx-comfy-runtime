from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILLS_FILE = ROOT / "integrations" / "LLM" / "ComfyUI_Qwen_H3_Prompt" / "skills.py"
SPEC = importlib.util.spec_from_file_location("qwen_h3_skills_for_tests", SKILLS_FILE)
skills = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = skills
assert SPEC.loader is not None
SPEC.loader.exec_module(skills)


def valid_plan(*, style_decision="reuse", scope="full"):
    preset_id = "preset-documentary" if style_decision == "reuse" else None
    window = None if scope == "full" else {
        "index": 1, "count": 3, "overlap_scene_ids": ["scene-002"],
    }
    return {
        "schema_version": "project-visual-planner/v1",
        "analysis_scope": scope,
        "window": window,
        "style": {
            "decision": style_decision,
            "preset_id": preset_id,
            "name": "Cinematic documentary",
            "prompt": "Natural light, restrained documentary composition",
            "reason": "Matches the grounded script.",
        },
        "characters": [{
            "id": "character:ana", "name": "Ana", "aliases": ["Dra. Ana"],
            "visual_description": "Woman in her forties with short dark hair and a blue coat.",
            "scene_ids": ["scene-001", "scene-002"],
        }],
        "objects": [{
            "id": "object:red-key", "name": "Red key", "aliases": ["the key"],
            "visual_description": "Small brass key with a worn red enamel head.",
            "scene_ids": ["scene-001", "scene-002"],
        }],
        "environments": [{
            "id": "environment:workshop", "name": "Workshop", "aliases": [],
            "visual_description": "Narrow repair workshop with timber benches and north-facing windows.",
            "scene_ids": ["scene-001", "scene-002"],
        }],
        "scenes": [
            {
                "scene_id": "scene-001", "characters": ["character:ana"],
                "objects": ["object:red-key"], "environment": "environment:workshop",
                "visual_context": "Ana stands at the central bench holding the key.",
                "image_prompt": "Wide view of Ana at a timber workbench, red key visible.",
                "motion_intent": "Ana crosses toward the locked cabinet.",
                "state_in": "Ana is at the workbench with the key in her right hand.",
                "required_action": "She walks to the cabinet without dropping the key.",
                "state_out": "Ana stands before the cabinet, still holding the key.",
                "framing": "Wide establishing shot", "camera": "Slow lateral track",
            },
            {
                "scene_id": "scene-002", "characters": ["character:ana"],
                "objects": ["object:red-key"], "environment": "environment:workshop",
                "visual_context": "Ana faces the cabinet with the key at its lock.",
                "image_prompt": "Medium close view of Ana presenting the red key to the cabinet lock.",
                "motion_intent": "The key enters and turns in the lock.",
                "state_in": "The cabinet is locked and the key is outside the lock.",
                "required_action": "Ana inserts and turns the key.",
                "state_out": "The lock is open and Ana keeps hold of the key.",
                "framing": "Medium close-up", "camera": "Static",
            },
        ],
    }


class ProjectVisualPlannerSkillTests(unittest.TestCase):
    def validate(self, plan):
        return skills.validate_project_visual_planner_output(json.dumps(plan))

    def test_custom_skill_is_discovered_with_metadata(self):
        spec = next(item for item in skills.SKILL_REGISTRY if item.id == "project-visual-planner")
        self.assertEqual(spec.source, "custom")
        self.assertEqual(spec.display_name, "Project Visual Planner")
        self.assertIn("structured-json", spec.tags)
        self.assertIn("project-visual-planner/v1", skills.skill_instructions(spec.id, "t2va"))

    def test_valid_schema_preserves_recurring_entities_aliases_and_scene_links(self):
        plan = valid_plan()
        self.assertEqual(self.validate(plan), [])
        self.assertEqual(plan["characters"][0]["id"], "character:ana")
        self.assertEqual(plan["characters"][0]["aliases"], ["Dra. Ana"])
        self.assertEqual(plan["characters"][0]["scene_ids"], ["scene-001", "scene-002"])

    def test_style_preset_reuse_and_new_style_are_both_valid(self):
        self.assertEqual(self.validate(valid_plan(style_decision="reuse")), [])
        self.assertEqual(self.validate(valid_plan(style_decision="new")), [])

    def test_window_output_carries_overlap_metadata(self):
        self.assertEqual(self.validate(valid_plan(scope="window")), [])

    def test_invalid_and_partially_corrupted_json_fail_clearly(self):
        invalid = skills.output_issues(
            '{"schema_version":"project-visual-planner/v1"', "t2va", 10,
            skill="project-visual-planner",
        )
        wrapped = skills.output_issues(
            "result: " + json.dumps(valid_plan()), "t2va", 10,
            skill="project-visual-planner",
        )
        self.assertTrue(any("invalid JSON" in issue for issue in invalid))
        self.assertTrue(any("invalid JSON" in issue for issue in wrapped))

    def test_unknown_scene_entity_and_ambiguous_alias_are_rejected(self):
        plan = valid_plan()
        plan["characters"][0]["aliases"].append("she")
        plan["scenes"][0]["objects"].append("object:invented")
        issues = self.validate(plan)
        self.assertTrue(any("ambiguous pronoun alias" in issue for issue in issues))
        self.assertTrue(any("unknown objects id" in issue for issue in issues))

    def test_mismatched_bidirectional_scene_association_is_rejected(self):
        plan = valid_plan()
        plan["objects"][0]["scene_ids"] = ["scene-001"]
        self.assertTrue(any("do not match scene associations" in issue for issue in self.validate(plan)))

    def test_generate_hold_decision_is_rejected(self):
        plan = valid_plan()
        plan["scenes"][0]["required_action"] = "HOLD"
        self.assertTrue(any("GENERATE/HOLD" in issue for issue in self.validate(plan)))

    def test_h3_prompt_writing_remains_unrestricted_and_loadable(self):
        self.assertIn("h3-prompt-writing", skills.SKILL_NAMES)
        self.assertEqual(skills.output_issues("free-form H3 prompt", "t2va", 10, skill="h3-prompt-writing"), [])
        self.assertIn("Selected H3 Skill: h3-prompt-writing", skills.system_prompt("h3-prompt-writing", "t2va", 10))


if __name__ == "__main__":
    unittest.main()

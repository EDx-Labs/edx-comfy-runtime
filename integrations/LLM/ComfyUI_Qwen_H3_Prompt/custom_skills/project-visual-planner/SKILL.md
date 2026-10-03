---
name: project-visual-planner
description: Analyze screenplay or transcript windows into strict project visual-planning JSON with stable recurring entities, StylePreset reuse, and scene state continuity.
display-name: Project Visual Planner
version: 1.0.0
tags: [visual-planning, screenplay-analysis, structured-json]
---

# Project Visual Planner

Analyze the supplied screenplay/transcript. Return a pre-production plan; do not generate media, call another model or service, write a final MiniMax H3 prompt, or decide `GENERATE`/`HOLD`.

## Input contract

The user payload should be a JSON object with:

- `scenes`: ordered scene objects. Preserve every supplied scene ID exactly.
- `analysis_scope`: `full` or `window`.
- `window`: optional `{ "index": integer, "count": integer, "overlap_scene_ids": [string] }`.
- `entity_registry`: optional entities already established by earlier windows. Reuse their IDs when names, explicit aliases, and narrative evidence identify the same entity.
- `style_presets`: optional objects with `id`, `name`, and `prompt`. Reuse one only when it genuinely fits.

If the caller supplies prose rather than this envelope, treat it as a single `full` analysis and create stable, descriptive scene IDs only when no IDs exist.

For long scripts, callers should send overlapping windows. Analyze only the current window, carry forward the supplied registry, include overlap scenes in the response, and set `analysis_scope` to `window`. A later reconciler can merge entities by exact IDs. Never create a new entity merely because an established entity appears under a known alias in a new window.

## Evidence and identity rules

- Include only visually relevant characters, objects, and environments supported by the text. Do not invent important entities.
- One real entity has one logical ID across all appearances. Use lowercase IDs with a type prefix, for example `character:ana`, `object:red-key`, or `environment:workshop`.
- An alias is an explicit alternate name or an unambiguous reference supported by context. Do not store generic pronouns (`he`, `she`, `they`, `it`, `ele`, `ela`, `eles`, `elas`) as aliases.
- If identity is uncertain, keep entities separate and do not claim an alias relationship.
- `visual_description` contains stable appearance only. Do not put scene action, camera movement, or changing state there.
- Associate an entity with every scene where it is visibly present or visually required, using the same ID in both the entity's `scene_ids` and the scene arrays.

## StylePreset decision

Return exactly one style decision:

- `reuse`: `preset_id` must identify a supplied preset; copy its `name` and `prompt` faithfully.
- `new`: use `preset_id: null` and propose one coherent style only when no supplied preset is suitable.

Do not propose a near-duplicate of an existing preset. Base the decision on the script and explain it briefly in `reason`.

## Scene planning rules

Keep `visual_context` and `image_prompt` about the still visual composition. Keep `motion_intent`, `required_action`, and state transitions separate. `state_in` describes the visible starting state; `state_out` describes the visible ending state. Use empty strings for unsupported motion/camera details rather than inventing them. `framing` and `camera` are optional in meaning but always present as strings.

This Skill does not make an optimization decision. Never output `GENERATE`, `HOLD`, `action`, `suggested_action`, or `effective_action`.

## Output contract

Return exactly one JSON object and nothing else: no Markdown fence, commentary, prefix, or suffix. Use this exact root shape and all fields shown:

```json
{
  "schema_version": "project-visual-planner/v1",
  "analysis_scope": "full",
  "window": null,
  "style": {
    "decision": "reuse",
    "preset_id": "style-preset-id",
    "name": "Preset name",
    "prompt": "Preset prompt",
    "reason": "Short evidence-based reason"
  },
  "characters": [
    {
      "id": "character:stable-id",
      "name": "Canonical name",
      "aliases": ["Explicit alias"],
      "visual_description": "Stable appearance only",
      "scene_ids": ["scene-id"]
    }
  ],
  "objects": [
    {
      "id": "object:stable-id",
      "name": "Canonical name",
      "aliases": [],
      "visual_description": "Stable appearance only",
      "scene_ids": ["scene-id"]
    }
  ],
  "environments": [
    {
      "id": "environment:stable-id",
      "name": "Canonical name",
      "aliases": [],
      "visual_description": "Stable appearance only",
      "scene_ids": ["scene-id"]
    }
  ],
  "scenes": [
    {
      "scene_id": "scene-id",
      "characters": ["character:stable-id"],
      "objects": ["object:stable-id"],
      "environment": "environment:stable-id",
      "visual_context": "Visible situation without motion instructions",
      "image_prompt": "Consistent still-image prompt",
      "motion_intent": "Intended visible change or empty string",
      "state_in": "Visible state at scene start",
      "required_action": "Action required by the script or empty string",
      "state_out": "Visible state at scene end",
      "framing": "Justified framing or empty string",
      "camera": "Justified camera behavior or empty string"
    }
  ]
}
```

For a `new` style, `preset_id` must be `null`. For a `window` result, `window` must be an object with `index`, `count`, and `overlap_scene_ids`; for a `full` result it must be `null`. Arrays may be empty, and a scene's `environment` may be `null`.

Before returning, verify that every referenced entity ID exists in the matching entity array, every entity `scene_ids` entry exists in `scenes`, IDs are unique, aliases are not duplicate entities, and the JSON parses strictly.

# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp[cli]", "pillow", "MaterialX"]
# ///
"""
Verifies match_material_from_photo / apply_photo_material_match — the
two-call photo-to-material pipeline, mirroring construction_mode() ->
calculate_world_coordinates()'s established shape (return the image + a
vision_prompt, consume the structured JSON response in a second call). Two
things matter most, same discipline as generate_procedural_material: (1) a
missing/malformed vision analysis returns an honest error, never a guessed
default for a photo-derived value; (2) apply_photo_material_match reuses
_calibrate_and_build_procedural_material — the SAME bake-and-measure
verification loop generate_procedural_material trusts — so
calibration_status still means what it always means, even though the target
here came from a vision estimate instead of a recorded recipe.
Run: uv run tests/test_photo_material_match.py
"""
import sys, os, json, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import server

failures = []


def check(name, condition):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}")
    if not condition:
        failures.append(name)


server._capture_plain_screenshot = lambda name: None

_DNA_RAW = {
    "get_mesh_quality_report": {"counts": {"verts": 100, "edges": 200, "faces": 100},
                                 "uv": {"has_uvs": True, "layer_count": 1}, "modifiers": []},
    "analyze_topology": {"topology_score": 90, "stats": {}},
    "run_unreal_readiness_check": {"checks": {}},
}


def make_fake(measured_roughness=0.5, bump_stdev=0.03, faces_using_material=100):
    def fake(cmd, **kwargs):
        if cmd in _DNA_RAW:
            return _DNA_RAW[cmd]
        if cmd == "execute_code_safe":
            code = kwargs.get("code", "")
            if "measured_roughness" in code:
                return {"result": json.dumps({
                    "object": "X", "material": "M",
                    "created_material": True, "measured_roughness": measured_roughness,
                    "measured_bump_stdev": bump_stdev,
                    "faces_using_material": faces_using_material, "auto_assigned_all_faces": False,
                })}
        raise AssertionError(f"unexpected command: {cmd} / {kwargs.get('code','')[:60]}")
    return fake


# ── match_material_from_photo: missing file -> honest error, no vision call attempted ──
out = json.loads(server.match_material_from_photo(
    object_name="X", material_name="M", reference_image_path="/tmp/does_not_exist_12345.png")[-1])
check("missing reference image returns an error, not a fabricated analysis", "error" in out)

# ── match_material_from_photo: real file -> [Image, request] with a well-formed prompt ──
tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
tmp.write(b"\x89PNG\r\n\x1a\n" + b"0" * 64)  # not a real PNG, just needs to exist and be readable
tmp.close()
try:
    out = server.match_material_from_photo(object_name="X", material_name="M", reference_image_path=tmp.name)
    check("returns exactly [Image, request_dict]", len(out) == 2)
    request = out[1]
    check("request carries the vision_prompt", "vision_prompt" in request)
    for key in ["dominant_base_color_rgb", "perceived_roughness", "perceived_metallic",
                "surface_pattern", "pattern_scale", "confidence"]:
        check(f"vision_prompt schema mentions {key}", key in request["vision_prompt"])
    check("request tells the caller which tool to call next",
          "apply_photo_material_match" in request["instruction"])
finally:
    os.unlink(tmp.name)

# ── apply_photo_material_match: malformed JSON -> honest error ──
server._send_raw = make_fake()
out = json.loads(server.apply_photo_material_match(
    object_name="X", material_name="M", vision_analysis_json="not json")[-1])
check("malformed vision_analysis_json returns an error", "error" in out)

# ── apply_photo_material_match: missing required field -> honest error, no guess ──
incomplete = json.dumps({"dominant_base_color_rgb": [0.5, 0.3, 0.2], "perceived_roughness": 0.5})
out = json.loads(server.apply_photo_material_match(
    object_name="X", material_name="M", vision_analysis_json=incomplete)[-1])
check("missing required vision fields returns an error naming what's missing",
      "error" in out and "perceived_metallic" in str(out.get("error", "")))

# ── apply_photo_material_match: unrecognized surface_pattern/pattern_scale -> honest error ──
bad_pattern = json.dumps({
    "dominant_base_color_rgb": [0.5, 0.3, 0.2], "perceived_roughness": 0.5, "perceived_metallic": 0.0,
    "surface_pattern": "shiny_glittery", "pattern_scale": "huge",
})
out = json.loads(server.apply_photo_material_match(
    object_name="X", material_name="M", vision_analysis_json=bad_pattern)[-1])
check("unrecognized surface_pattern/pattern_scale combo returns an error, not an invented bumpiness",
      "error" in out)

# ── apply_photo_material_match: full valid analysis, in-tolerance -> matched, honest color_source ──
server._send_raw = make_fake(measured_roughness=0.52, bump_stdev=0.03)
valid_vision = json.dumps({
    "dominant_base_color_rgb": [0.4, 0.25, 0.1], "secondary_color_rgb": [0.7, 0.5, 0.3],
    "perceived_roughness": 0.5, "roughness_reasoning": "diffuse highlights",
    "perceived_metallic": 0.1, "metallic_reasoning": "mostly non-metallic reflections",
    "surface_pattern": "grainy", "pattern_scale": "medium", "confidence": "medium",
})
out = json.loads(server.apply_photo_material_match(
    object_name="X", material_name="M", vision_analysis_json=valid_vision)[-1])
check("valid photo analysis in tolerance reports calibration_status=matched", out.get("calibration_status") == "matched")
check("color_source is honestly vision_estimated_from_photo, never blurred with a measured value",
      out.get("color_source") == "vision_estimated_from_photo")
check("metallic_set carries the real vision estimate (0.1), not a category-based 0/0.9 constant",
      out.get("metallic_set") == 0.1)
check("target_bumpiness comes from the documented grainy/medium table entry",
      out.get("bump_strength_heuristic") == round(min(1.0, 0.025 * 3.0), 4))
check("vision reasoning fields are carried into the result, not dropped",
      out.get("vision_roughness_reasoning") == "diffuse highlights")
check("no photo signal for subsurface/specular is stated explicitly, not silently assumed",
      "vision_note" in out and "no photo signal" in out["vision_note"])

# ── apply_photo_material_match: reuses the SAME calibration honesty as generate_procedural_material ──
server._send_raw = make_fake(measured_roughness=0.1, bump_stdev=0.03)  # far from target 0.5, both passes will miss
out = json.loads(server.apply_photo_material_match(
    object_name="X", material_name="M", vision_analysis_json=valid_vision)[-1])
check("an out-of-tolerance calibration is reported as approximate, never forced to matched",
      out.get("calibration_status") == "approximate")

# ── apply_photo_material_match: save_as_recipe records a real, reusable recipe ──
original_recipes = server._RECIPES
original_save = server._save_recipes
server._RECIPES = []
server._save_recipes = lambda: None
server._send_raw = make_fake(measured_roughness=0.5, bump_stdev=0.03)
out = json.loads(server.apply_photo_material_match(
    object_name="X", material_name="M", vision_analysis_json=valid_vision, save_as_recipe="test_photo_leather")[-1])
saved = server._find_recipe_by_canonical_name("test_photo_leather")
check("save_as_recipe records a real recipe_type=material entry", saved is not None)
check("the saved recipe is tagged category=photo_matched, not confused with a hand-authored recipe",
      saved is not None and saved.get("parameters", {}).get("category") == "photo_matched")
check("a later generate_procedural_material(target_recipe=...) can find this same recipe",
      server._find_recipe_by_canonical_name("test_photo_leather") is not None)
server._RECIPES = original_recipes
server._save_recipes = original_save

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All checks passed.")

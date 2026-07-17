# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp[cli]", "pillow", "MaterialX"]
# ///
"""
Verifies generate_procedural_material — the first tool that CREATES a real
node-based PBR material (noise/voronoi/bump) instead of only modifying an
existing one, calibrated against the material knowledge layer rather than
guessed. Two things matter most here, mirroring the discipline everywhere
else in this file: (1) category/target resolution follows the same
explicit > name > fingerprint precedence as apply_weathering_recipe, ending
in an honest error instead of a silent default; (2) the calibration loop
never claims a match it didn't verify — "matched" only after a real measured
Roughness bake lands in tolerance, "approximate" when a bounded retry still
misses, "uncalibrated" when no recipe was ever recorded for the target
category at all.
Run: uv run tests/test_procedural_material.py
"""
import sys, os, json
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

metal_recipe_fp = {"roughness_source": "texture_sampled", "roughness_avg": 0.9,
                    "normal_map_bumpiness": 0.1, "subsurface_weight": 0.0, "specular_ior_level": 0.5}


def make_fake(material_name, roughness_sequence, object_materials=None, fingerprint_for_scan=None, faces_using_material=100):
    """roughness_sequence: list of measured_roughness values returned by
    successive build_and_measure() calls (build script), popped in order.
    faces_using_material defaults to a nonzero value — these tests are about
    the calibration loop itself, not the separate 0-faces failure mode
    (covered by its own dedicated test below)."""
    seq = list(roughness_sequence)

    def fake(cmd, **kwargs):
        if cmd in _DNA_RAW:
            return _DNA_RAW[cmd]
        if cmd == "get_object_info":
            return {"name": "X", "materials": object_materials or [material_name]}
        if cmd == "execute_code_safe":
            code = kwargs.get("code", "")
            if "measured_roughness" in code:
                val = seq.pop(0) if seq else None
                return {"result": json.dumps({
                    "object": "X", "material": material_name,
                    "created_material": True, "measured_roughness": val,
                    "faces_using_material": faces_using_material, "auto_assigned_all_faces": False,
                })}
            if "has_principled" in code:
                return {"result": json.dumps([{
                    "name": material_name, "has_principled": True, "texture_fed": [], "missing_maps": [],
                    "fingerprint": fingerprint_for_scan or {},
                }])}
            if "filepath_raw" in code:
                return {"result": json.dumps({"path": None})}
        raise AssertionError(f"unexpected command: {cmd} / {kwargs.get('code','')[:60]}")
    return fake


original_recipes = server._RECIPES
original_save = server._save_recipes
server._RECIPES = [
    {"recipe_type": "material", "canonical_name": "test_metal",
     "trigger_phrases": ["named_organic_material"],  # deliberately mismatched below
     "parameters": {"category": "metal", "fingerprint": metal_recipe_fp}},
]
server._save_recipes = lambda: None

# ── Explicit target_recipe resolves directly, measured roughness in tolerance -> matched ──
server._send_raw = make_fake("M", [0.9])
server._SNAPSHOTS.clear()
out = json.loads(server.generate_procedural_material(object_name="X", material_name="M", target_recipe="test_metal")[-1])
check("explicit target_recipe resolves category from the named recipe", out.get("category_used") == "metal")
check("explicit target_recipe is echoed as canonical_recipe_used", out.get("canonical_recipe_used") == "test_metal")
check("in-tolerance first-pass measurement reports calibration_status=matched", out.get("calibration_status") == "matched")
check("bump_strength_heuristic is bumpiness x3, clamped, and labeled unverified", out.get("bump_strength_heuristic") == 0.3
      and "unverified" in out.get("bump_strength_note", ""))
check("metallic_set follows the category-based constant (metal -> 0.9)", out.get("metallic_set") == 0.9)
check("only one calibration attempt was needed", len(out.get("calibration_attempts", [])) == 1)
check("category=metal reuses the real rust_color constant, not an invented color",
      out.get("color_source") == "reused_rust_color")

# ── target_recipe that doesn't exist -> honest error, not a silent default ──
server._send_raw = make_fake("M", [])
out = json.loads(server.generate_procedural_material(object_name="X", material_name="M", target_recipe="nope")[-1])
check("unknown target_recipe returns an error instead of guessing", "error" in out)

# ── Explicit category with nothing recorded for it -> uncalibrated, not invented numbers ──
server._send_raw = make_fake("M", [0.6])
out = json.loads(server.generate_procedural_material(object_name="X", material_name="M", category="ceramic_never_recorded")[-1])
check("category with no recorded recipe reports calibration_status=uncalibrated", out.get("calibration_status") == "uncalibrated")
check("roughness_was_recorded is False when no recipe backs the category", out.get("roughness_was_recorded") is False)
check("a non-metal category stays an explicitly-labeled gray placeholder, not implied real color",
      out.get("color_source") == "generic_gray_placeholder")

# ── Out-of-tolerance first pass triggers exactly one retry; retry succeeds -> matched ──
server._send_raw = make_fake("M", [0.6, 0.88])  # target 0.9: first far (0.3 off), retry close (0.02 off)
out = json.loads(server.generate_procedural_material(object_name="X", material_name="M", target_recipe="test_metal")[-1])
check("out-of-tolerance first pass triggers a second calibration attempt", len(out.get("calibration_attempts", [])) == 2)
check("a successful retry reports calibration_status=matched, not approximate", out.get("calibration_status") == "matched")

# ── Out-of-tolerance first pass, retry still misses -> honestly reported approximate ──
server._send_raw = make_fake("M", [0.3, 0.35])  # target 0.9: both passes far off
out = json.loads(server.generate_procedural_material(object_name="X", material_name="M", target_recipe="test_metal")[-1])
check("a still-out-of-tolerance retry is reported as approximate, never forced to matched",
      out.get("calibration_status") == "approximate")
check("the real measured value from the retry is what's reported, not the original target",
      out.get("measured_roughness") == 0.35)

# ── Internal bake failing outright -> unverified, not silently matched ──
server._send_raw = make_fake("M", [None])
out = json.loads(server.generate_procedural_material(object_name="X", material_name="M", target_recipe="test_metal")[-1])
check("a failed internal calibration bake reports calibration_status=unverified", out.get("calibration_status") == "unverified")

# ── A freshly-created material with zero faces referencing it (real bug caught live: ──
# a brand-new slot on a multi-material object baked to a flat 0.0 regardless of
# target, which the loop originally misreported as a generic "approximate" near-miss
# instead of "nothing was actually measured") -> honest unverified_no_faces_assigned.
server._send_raw = make_fake("M", [0.0, 0.0], faces_using_material=0)
out = json.loads(server.generate_procedural_material(object_name="X", material_name="M", target_recipe="test_metal")[-1])
check("zero faces referencing the new material reports calibration_status=unverified_no_faces_assigned, not approximate",
      out.get("calibration_status") == "unverified_no_faces_assigned")
check("the result explains the fix (assign faces first) instead of silently accepting a 0.0 measurement",
      "fix" in out and "faces" in out["fix"].lower())
check("no retry is wasted on a 0-faces measurement — only one calibration attempt is made",
      len(out.get("calibration_attempts", [])) == 1)

# ── Name-based precedence: material_name matches a trigger_phrase -> category from that recipe ──
server._send_raw = make_fake("named_organic_material", [0.9])
out = json.loads(server.generate_procedural_material(object_name="X", material_name="named_organic_material")[-1])
check("name-based lookup supplies the category when neither category nor target_recipe is given",
      out.get("category_used") == "metal" and out.get("canonical_recipe_used") == "test_metal")

# ── Fingerprint-based fallback: name doesn't match, but the object's own DNA has a close match ──
server._send_raw = make_fake("tripo_mat_deadbeef", [0.9],
                              fingerprint_for_scan={"roughness_source": "texture_sampled", "roughness_avg": 0.91,
                                                     "normal_map_bumpiness": 0.098})
server._SNAPSHOTS.clear()
out = json.loads(server.generate_procedural_material(object_name="X", material_name="tripo_mat_deadbeef")[-1])
check("fingerprint fallback fires when the name-recipe tier finds nothing, using the object's own measured fingerprint",
      out.get("category_used") == "metal" and out.get("canonical_recipe_used") == "test_metal")

# ── Nothing resolves at all -> explicit error asking for category/target_recipe, no guess ──
server._send_raw = make_fake("tripo_mat_unknown", [0.9],
                              fingerprint_for_scan={"roughness_source": "texture_sampled", "roughness_avg": 0.1,
                                                     "normal_map_bumpiness": 0.001})
server._SNAPSHOTS.clear()
out = json.loads(server.generate_procedural_material(object_name="X", material_name="tripo_mat_unknown")[-1])
check("no explicit param, no name match, no close fingerprint -> honest error, not an invented category",
      "error" in out and ("category" in out.get("fix", "").lower()))

server._RECIPES = original_recipes
server._save_recipes = original_save

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All checks passed.")

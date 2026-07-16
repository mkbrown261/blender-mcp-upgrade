# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp[cli]", "pillow", "MaterialX"]
# ///
"""
Verifies the two-technique weathering dispatch: a second, structurally
different signal (fraying — graph distance from UV-seam/boundary edges) sits
alongside the original curvature-based oxidation technique, dispatched per
material from real sampled metal_factor unless explicitly overridden, with
explicit material_category always winning over the automatic pick and over
any recipe_type="material" lookup (same three-tier precedence as
wear_scalar/trigger_phrase elsewhere in this tool).
Run: uv run tests/test_weathering_techniques.py
"""
import sys, os, json, ast
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import server

failures = []


def check(name, condition):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}")
    if not condition:
        failures.append(name)


_DNA_RAW = {
    "get_mesh_quality_report": {"counts": {"verts": 100, "edges": 200, "faces": 100},
                                 "uv": {"has_uvs": True, "layer_count": 1}, "modifiers": []},
    "analyze_topology": {"topology_score": 90, "stats": {}},
    "run_unreal_readiness_check": {"checks": {}},
    "get_object_info": {"name": "X", "materials": ["M"]},
}


def make_fake_send_raw(captured):
    def fake(cmd, **kwargs):
        if cmd in _DNA_RAW:
            return _DNA_RAW[cmd]
        if cmd == "execute_code_safe":
            code = kwargs["code"]
            if "has_principled" in code:
                return {"result": json.dumps([{"name": "M", "has_principled": True,
                                                "texture_fed": [], "missing_maps": []}])}
            if "filepath_raw" in code and "original_surface_link" not in code:
                return {"result": json.dumps({"path": None})}
            captured["code"] = code
            return {"result": '{"object": "X", "materials_applied": [{"material": "M", '
                               '"technique_used": "oxidation"}], "materials_skipped": [], '
                               '"mask_stats": {}, "fray_mask_stats": {}, "percentiles_used": {}}'}
        raise AssertionError(f"unexpected command: {cmd}")
    return fake


server._RECIPES = []
server._save_recipes = lambda: None

# ── Script structure ─────────────────────────────────────────────────────────
captured = {}
server._send_raw = make_fake_send_raw(captured)
server._SNAPSHOTS.clear()
result = json.loads(server.apply_weathering_recipe(object_name="X", material_name="M"))
code = captured["code"]

check("tool call succeeds", "error" not in result)
try:
    ast.parse(code)
    ok = True
except SyntaxError as e:
    ok = False
    print("  syntax error:", e)
check("generated script parses cleanly", ok)

check("oxidation node prefix present", 'prefix = "AutoWeather_"' in code)
check("fraying node prefix present", 'fray_prefix = "AutoWeatherFray_"' in code)
check("both prefixes are removed at the start of the per-material loop (idempotent re-runs)",
      "nd.name.startswith(prefix) or nd.name.startswith(fray_prefix)" in code)
check("fray signal is graph distance from UV-seam or boundary edges, not curvature",
      "edge.use_seam" in code and "is_boundary" in code and "FRAY_RADIUS" in code)
check("fray mask is baked into its OWN vertex color attribute, not overwriting the oxidation mask",
      'fray_mask_name = "AutoWeather_FrayMask"' in code and 'mask_name = "AutoWeather_Mask"' in code)
check("per-material dispatch reads this material's OWN metal_factor_floored, not an object-wide flag",
      "technique = 'oxidation' if metal_factor_floored > 0.5 else 'fraying'" in code)
check("explicit material_category (forced_category) is checked before the automatic dispatch",
      "if forced_category == 'metal':" in code and "elif forced_category == 'organic':" in code)
check("fraying technique blends toward desaturation, not a rust tint",
      "ShaderNodeHueSaturation" in code and 'fray_prefix + "Desaturate"' in code)
check("fraying result is reported honestly via fray_mask_stats, including the all-zero-is-real-signal note",
      "fray_mask_stats" in code and "not a bug" in code)
check("technique_used is reported per material, not left implicit",
      '"technique_used": technique,' in code)

# ── {FORCEDCATEGORY} substitution ────────────────────────────────────────────
captured2 = {}
server._send_raw = make_fake_send_raw(captured2)
server._SNAPSHOTS.clear()
server.apply_weathering_recipe(object_name="X", material_name="M", material_category="organic")
check("explicit material_category='organic' substitutes into forced_category",
      "forced_category = 'organic'" in captured2["code"])

captured3 = {}
server._send_raw = make_fake_send_raw(captured3)
server._SNAPSHOTS.clear()
server.apply_weathering_recipe(object_name="X", material_name="M")
check("no material_category and no matching recipe -> forced_category is empty (automatic dispatch)",
      "forced_category = ''" in captured3["code"])

# ── _resolve_material_category: explicit > recipe > automatic precedence ────
server.record_creative_recipe(
    recipe_type="material", canonical_name="test_leather",
    trigger_phrases=["leather_mat"], parameters={"category": "organic"},
)

check("_resolve_material_category finds a stored material recipe by name",
      server._resolve_material_category("leather_mat") == "organic")
check("_resolve_material_category returns None when nothing matches",
      server._resolve_material_category("no_such_material_xyz") is None)
check("_resolve_material_category returns None for a blank name (nothing to look up)",
      server._resolve_material_category("") is None)

captured4 = {}
server._send_raw = make_fake_send_raw(captured4)
server._SNAPSHOTS.clear()
server.apply_weathering_recipe(object_name="X", material_name="leather_mat")
check("recipe-derived category (organic) is used when material_category isn't passed explicitly",
      "forced_category = 'organic'" in captured4["code"])

captured5 = {}
server._send_raw = make_fake_send_raw(captured5)
server._SNAPSHOTS.clear()
server.apply_weathering_recipe(object_name="X", material_name="leather_mat", material_category="metal")
check("explicit material_category='metal' overrides the recipe-derived 'organic' category",
      "forced_category = 'metal'" in captured5["code"])

# material_name="" (apply to every material) -> recipe-lookup tier is skipped,
# not attempted against an empty/ambiguous name.
captured6 = {}
server._send_raw = make_fake_send_raw(captured6)
server._SNAPSHOTS.clear()
server.apply_weathering_recipe(object_name="X", material_name="")
check("blank material_name skips the recipe-lookup tier and falls straight to automatic dispatch",
      "forced_category = ''" in captured6["code"])

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All checks passed.")

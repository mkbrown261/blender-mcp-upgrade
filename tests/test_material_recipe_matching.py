# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp[cli]", "pillow", "MaterialX"]
# ///
"""
Verifies retrieval by MEASURED SIMILARITY for the material knowledge layer —
the actual fix for it being unreachable. _resolve_material_category's
name-based lookup can never fire on auto-generated material names
(tripo_mat_XXXXXXXX never matches a trigger_phrase like "leather"), proven
live against a real couch asset tonight. This tests the fingerprint-based
fallback that makes every recorded recipe reachable regardless of naming,
with an honesty gate: a fingerprint that isn't actually close to anything
recorded must come back with no match, not a forced one.
Run: uv run tests/test_material_recipe_matching.py
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


# ── _fingerprint_distance: real numbers from tonight's actual recordings ────
fur = {"roughness_source": "texture_sampled", "roughness_avg_range": [0.7788, 0.8247],
       "normal_map_bumpiness_range": [0.0563, 0.0688]}
feather = {"roughness_source": "texture_sampled", "roughness_avg_range": [0.4661, 0.8885],
           "normal_map_bumpiness_range": [0.1004, 0.1074]}
rusted_metal = {"roughness_source": "texture_sampled", "roughness_avg_range": [0.9439, 0.9594],
                "normal_map_bumpiness_range": [0.0961, 0.1271]}
fur_new_sample = {"roughness_source": "texture_sampled", "roughness_avg": 0.80, "normal_map_bumpiness": 0.062}

check("two fingerprints from the same recorded category (fur vs. a fresh fur-like sample) land close",
      server._fingerprint_distance(fur, fur_new_sample) < 0.05)
check("fingerprints from genuinely different categories (fur vs. rusted metal) land far apart",
      server._fingerprint_distance(fur, rusted_metal) > 0.15)
check("range-shaped and single-value fingerprints both resolve via their midpoint, not crash",
      server._fingerprint_distance(feather, fur_new_sample) is not None)
check("a fingerprint missing roughness entirely returns None (nothing to compare), not a guessed distance",
      server._fingerprint_distance({"normal_map_bumpiness": 0.05}, fur) is None)

# ── _find_closest_material_recipe: seeded synthetic recipes, real file untouched ─
original_recipes = server._RECIPES
original_save = server._save_recipes
server._RECIPES = [
    {"recipe_type": "material", "canonical_name": "test_fur",
     "parameters": {"category": "organic", "fingerprint": fur}},
    {"recipe_type": "material", "canonical_name": "test_rusted_metal",
     "parameters": {"category": "metal", "fingerprint": rusted_metal}},
    {"recipe_type": "aging", "canonical_name": "should_be_ignored",
     "parameters": {"fingerprint": fur}},  # wrong recipe_type — must not match
]
server._save_recipes = lambda: None

close_match = server._find_closest_material_recipe(fur_new_sample)
check("a close fingerprint returns the correct matching recipe", close_match is not None
      and close_match["canonical_name"] == "test_fur")
check("the match reports its category from the recorded recipe", close_match is not None
      and close_match["category"] == "organic")
check("the match reports a real numeric distance, not just a boolean", close_match is not None
      and isinstance(close_match["distance"], float) and close_match["distance"] < 0.05)

# The couch: roughness 0.4164, bumpiness 0.0047 — real numbers from tonight's
# live check, genuinely far from every recorded category. Must come back
# with NO match, not a forced one — the actual honesty test.
couch_fingerprint = {"roughness_source": "texture_sampled", "roughness_avg": 0.4164,
                      "normal_map_bumpiness": 0.0047}
couch_match = server._find_closest_material_recipe(couch_fingerprint)
check("a fingerprint far from everything recorded (the real couch) returns None, not a forced match",
      couch_match is None)

check("recipe_type != 'material' entries are never matched against, even with an identical fingerprint",
      server._find_closest_material_recipe(fur) is not None
      and server._find_closest_material_recipe(fur)["canonical_name"] != "should_be_ignored")

server._RECIPES = original_recipes
server._save_recipes = original_save

# ── get_asset_dna surfaces closest_known_material + fires known_material_match ─
server._RECIPES = [
    {"recipe_type": "material", "canonical_name": "test_fur",
     "parameters": {"category": "organic", "fingerprint": fur}},
]
server._save_recipes = lambda: None

_DNA_RAW = {
    "get_mesh_quality_report": {"counts": {"verts": 100, "edges": 200, "faces": 100},
                                 "uv": {"has_uvs": True, "layer_count": 1}, "modifiers": []},
    "analyze_topology": {"topology_score": 90, "stats": {}},
    "run_unreal_readiness_check": {"checks": {}},
    "get_object_info": {"name": "X", "materials": ["M"]},
}


def make_dna_fake(fingerprint):
    def fake(cmd, **kwargs):
        if cmd in _DNA_RAW:
            return _DNA_RAW[cmd]
        if cmd == "execute_code_safe":
            code = kwargs.get("code", "")
            if "has_principled" in code:
                return {"result": json.dumps([{
                    "name": "M", "has_principled": True, "texture_fed": [], "missing_maps": [],
                    "fingerprint": fingerprint,
                }])}
            if "filepath_raw" in code:
                return {"result": json.dumps({"path": None})}
        raise AssertionError(f"unexpected command: {cmd}")
    return fake


server._send_raw = make_dna_fake(fur_new_sample)
server._SNAPSHOTS.clear()
dna_close = json.loads(server.get_asset_dna(object_name="X", target_engine="unreal"))
mat = dna_close["materials"][0]
check("get_asset_dna surfaces closest_known_material on the material entry",
      mat.get("closest_known_material", {}).get("canonical_name") == "test_fur")
check("known_material_match rule fires when a close match exists",
      any(r["id"] == "known_material_match" for r in dna_close["rules_fired"]))

server._send_raw = make_dna_fake(couch_fingerprint)
server._SNAPSHOTS.clear()
dna_far = json.loads(server.get_asset_dna(object_name="X", target_engine="unreal"))
mat_far = dna_far["materials"][0]
check("get_asset_dna reports closest_known_material as null when nothing is close (the couch case)",
      mat_far.get("closest_known_material") is None)
check("known_material_match rule does NOT fire when nothing matched",
      not any(r["id"] == "known_material_match" for r in dna_far["rules_fired"]))

server._RECIPES = original_recipes
server._save_recipes = original_save

# ── apply_weathering_recipe: explicit > name-recipe > fingerprint-recipe > automatic ─
server._RECIPES = [
    {"recipe_type": "material", "canonical_name": "test_fur",
     "trigger_phrases": ["named_fur_material"],
     "parameters": {"category": "organic", "fingerprint": fur}},
]
server._save_recipes = lambda: None
server._capture_plain_screenshot = lambda name: None


def make_weathering_fake(material_name, fingerprint):
    def fake(cmd, **kwargs):
        if cmd == "get_object_info":
            return {"name": "X", "materials": [material_name]}
        if cmd in _DNA_RAW:
            return _DNA_RAW[cmd]
        if cmd == "execute_code_safe":
            code = kwargs.get("code", "")
            if "has_principled" in code:
                return {"result": json.dumps([{
                    "name": material_name, "has_principled": True, "texture_fed": [], "missing_maps": [],
                    "fingerprint": fingerprint,
                }])}
            if "filepath_raw" in code:
                return {"result": json.dumps({"path": None})}
            # the real apply_weathering_recipe script — capture it so the
            # test can inspect what forced_category it was built with
            captured_forced["value"] = code
            return {"result": '{"object": "X", "materials_applied": [], "materials_skipped": [], '
                               '"mask_stats": {}, "fray_mask_stats": {}, "percentiles_used": {}}'}
        raise AssertionError(f"unexpected command: {cmd}")
    return fake


captured_forced = {}
# Material name DOES match a recorded trigger_phrase -> name-recipe tier wins,
# fingerprint tier never even gets consulted.
server._send_raw = make_weathering_fake("named_fur_material", couch_fingerprint)  # deliberately mismatched fingerprint
server._SNAPSHOTS.clear()
server.apply_weathering_recipe(object_name="X", material_name="named_fur_material")
check("name-recipe tier wins when the material name matches a trigger_phrase, even with a mismatched fingerprint",
      "forced_category = 'organic'" in captured_forced.get("value", ""))

# Material name does NOT match any trigger_phrase, but its fingerprint is
# close to a recorded one -> fingerprint tier supplies the category.
captured_forced.clear()
server._send_raw = make_weathering_fake("tripo_mat_deadbeef", fur_new_sample)
server._SNAPSHOTS.clear()
server.apply_weathering_recipe(object_name="X", material_name="tripo_mat_deadbeef")
check("fingerprint-recipe tier fires when the name-recipe tier finds nothing, using the real measured fingerprint",
      "forced_category = 'organic'" in captured_forced.get("value", ""))

# Neither name nor fingerprint match anything (the couch case) -> falls
# through to automatic dispatch, forced_category stays empty.
captured_forced.clear()
server._send_raw = make_weathering_fake("tripo_mat_couch", couch_fingerprint)
server._SNAPSHOTS.clear()
server.apply_weathering_recipe(object_name="X", material_name="tripo_mat_couch")
check("neither tier fires when nothing matches (the couch) -> automatic dispatch, forced_category stays empty",
      "forced_category = ''" in captured_forced.get("value", ""))

# Explicit material_category always wins over both lookup tiers.
captured_forced.clear()
server._send_raw = make_weathering_fake("named_fur_material", fur_new_sample)
server._SNAPSHOTS.clear()
server.apply_weathering_recipe(object_name="X", material_name="named_fur_material", material_category="metal")
check("explicit material_category overrides both the name-recipe and fingerprint-recipe tiers",
      "forced_category = 'metal'" in captured_forced.get("value", ""))

server._RECIPES = original_recipes
server._save_recipes = original_save

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All checks passed.")

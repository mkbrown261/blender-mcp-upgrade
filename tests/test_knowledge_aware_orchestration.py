# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp[cli]", "pillow", "MaterialX"]
# ///
"""
Verifies the material knowledge layer actually reaches the existing advisory
tools (what_next, production_review) instead of sitting inert behind
get_asset_dna alone. Deliberately NOT a new autonomous system — both tools
stay fully read-only/advisory, this only makes their existing text more
specific: a material with no active Principled BSDF now gets pointed at
generate_procedural_material (a real option that didn't exist before
tonight), and a material that closely matches a recorded recipe gets named
directly instead of leaving the match buried in a field nobody reads. Also
covers the regression case — no materials on the object at all — to confirm
nothing new appears when there's nothing to say.
Run: uv run tests/test_knowledge_aware_orchestration.py
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

metal_recipe_fp = {"roughness_source": "texture_sampled", "roughness_avg": 0.9,
                    "normal_map_bumpiness": 0.1, "subsurface_weight": 0.0, "specular_ior_level": 0.5}

original_recipes = server._RECIPES
original_save = server._save_recipes
server._RECIPES = [
    {"recipe_type": "material", "canonical_name": "test_metal",
     "trigger_phrases": [], "parameters": {"category": "metal", "fingerprint": metal_recipe_fp}},
]
server._save_recipes = lambda: None

CLEAN_MESH_STATS = {
    "counts": {"verts": 500, "edges": 900, "faces": 300},
    "uv": {"has_uvs": True, "layer_count": 1},
    "modifiers": [], "health": "clean",
    "face_types": {"quads": 300, "tris": 0, "ngons": 0},
    "problems": {},
}


def make_fake(mat_names, scan_entries):
    def fake(cmd, **kwargs):
        if cmd == "get_object_info":
            return {"name": "X", "mesh": {"vertices": 500, "polygons": 300}, "materials": mat_names}
        if cmd == "get_mesh_quality_report":
            return CLEAN_MESH_STATS
        if cmd == "detect_mesh_problems":
            return {"problems": []}
        if cmd == "analyze_topology":
            return {"topology_score": 90, "stats": {}}
        if cmd == "run_unreal_readiness_check":
            return {"checks": {}}
        if cmd == "execute_code_safe":
            code = kwargs.get("code", "")
            if "has_principled" in code:
                return {"result": json.dumps(scan_entries)}
            if "filepath_raw" in code:
                return {"result": json.dumps({"path": None})}
        raise AssertionError(f"unexpected command: {cmd}")
    return fake


# ── what_next: a blank material (no Principled BSDF) gets pointed at generate_procedural_material ──
server._send_raw = make_fake(["Blank"], [
    {"name": "Blank", "has_principled": False, "texture_fed": [], "missing_maps": [], "fingerprint": {}},
])
server._SNAPSHOTS.clear()
out = json.loads(server.what_next(object_name="X"))
notes = out.get("material_knowledge", [])
check("what_next flags a Principled-less material and points at generate_procedural_material",
      any("generate_procedural_material" in n and "Blank" in n for n in notes))

# ── what_next: a material with a close fingerprint match names the matched recipe ──
server._send_raw = make_fake(["Fur"], [
    {"name": "Fur", "has_principled": True, "texture_fed": ["Base Color"], "missing_maps": [],
     "fingerprint": {"roughness_source": "texture_sampled", "roughness_avg": 0.91, "normal_map_bumpiness": 0.098}},
])
server._SNAPSHOTS.clear()
out = json.loads(server.what_next(object_name="X"))
notes = out.get("material_knowledge", [])
check("what_next names the specific matched recipe when closest_known_material exists",
      any("test_metal" in n and "Fur" in n for n in notes))

# ── what_next: no materials at all -> no material_knowledge key, nothing invented ──
server._send_raw = make_fake([], [])
server._SNAPSHOTS.clear()
out = json.loads(server.what_next(object_name="X"))
check("what_next adds no material_knowledge field when the object has no materials",
      "material_knowledge" not in out)

# ── production_review: same two signals feed into findings (info for match, no crash) ──
server._send_raw = make_fake(["Fur"], [
    {"name": "Fur", "has_principled": True, "texture_fed": ["Base Color"], "missing_maps": [],
     "fingerprint": {"roughness_source": "texture_sampled", "roughness_avg": 0.91, "normal_map_bumpiness": 0.098}},
])
server._SNAPSHOTS.clear()
out = json.loads(server.production_review(object_name="X"))
info_issues = [f["issue"] for f in [] ] # placeholder, real check below via critical/warnings/score presence
check("production_review succeeds (no crash) with a known-match material and returns a real score",
      "production_score" in out and "error" not in out)
check("production_review's recorded-match finding does not appear in warnings/critical_blockers (info severity only)",
      not any("test_metal" in w.get("issue", "") for w in out.get("warnings", []))
      and not any("test_metal" in w.get("issue", "") for w in out.get("critical_blockers", [])))

# ── production_review: a heterogeneity.likely_blended signal surfaces as a WARNING, not silently ignored ──
server._send_raw = make_fake(["Blended"], [
    {"name": "Blended", "has_principled": True, "texture_fed": ["Base Color"], "missing_maps": [],
     "fingerprint": {"roughness_source": "texture_sampled", "roughness_avg": 0.5, "normal_map_bumpiness": 0.05},
     "heterogeneity": {"island_count": 3, "color_variance": 0.09, "likely_blended": True}},
])
server._SNAPSHOTS.clear()
out = json.loads(server.production_review(object_name="X"))
check("a likely_blended material surfaces as a warning recommending split_blended_material",
      any("split_blended_material" in w.get("fix", "") for w in out.get("warnings", [])))

# ── production_review: no heterogeneity field at all (today's real shape) -> no crash, no false flag ──
server._send_raw = make_fake(["Plain"], [
    {"name": "Plain", "has_principled": True, "texture_fed": ["Base Color"], "missing_maps": [],
     "fingerprint": {"roughness_source": "texture_sampled", "roughness_avg": 0.5, "normal_map_bumpiness": 0.05}},
])
server._SNAPSHOTS.clear()
out = json.loads(server.production_review(object_name="X"))
check("a material with no heterogeneity field at all produces no blended-material warning",
      not any("split_blended_material" in w.get("fix", "") for w in out.get("warnings", [])))

server._RECIPES = original_recipes
server._save_recipes = original_save

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All checks passed.")

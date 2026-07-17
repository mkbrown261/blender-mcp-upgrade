# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp[cli]", "pillow", "MaterialX"]
# ///
"""
Verifies studio_profile.json is actually wired in, not just readable. Real
gap found live: load_studio_profile()'s own docstring/return value claimed
simulate_production_readiness/review_board/production_review read the file
— they didn't; each hardcoded its own numbers in a separate, mismatched-
category-name table. Editing the JSON alone would have been a no-op.

Also covers a real crash bug found while wiring this in:
simulate_production_readiness and review_board both did
`pb.get("vert_budget", {}).get(eff_type)` — but pb["vert_budget"] is a
single int for the active playbook, not a dict keyed by asset type, so
`.get(eff_type)` on that int would raise AttributeError whenever a
playbook was active. Fixed as part of routing both through the real
studio_profile-backed helper.
Run: uv run tests/test_studio_profile.py
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


original_loader = server._load_studio_profile_dict


def fake_profile(vert_budgets=None, tri_budgets=None):
    def loader():
        return {
            "vert_budgets": vert_budgets or {},
            "tri_budgets": tri_budgets or {},
        }
    return loader


# ── _studio_vert_budget: real value when present, fallback when absent ───
server._load_studio_profile_dict = fake_profile(vert_budgets={"hero_character": 99999})
check("_studio_vert_budget returns the real profile value when present",
      server._studio_vert_budget("hero_character", 80000) == 99999)
check("_studio_vert_budget resolves the hero_char -> hero_character alias",
      server._studio_vert_budget("hero_char", 80000) == 99999)
check("_studio_vert_budget falls back when the category isn't in the profile",
      server._studio_vert_budget("weapon", 15000) == 15000)

server._load_studio_profile_dict = fake_profile(vert_budgets={"environment_prop": 12345})
check("_studio_vert_budget resolves the env_prop -> environment_prop alias",
      server._studio_vert_budget("env_prop", 20000) == 12345)

server._load_studio_profile_dict = fake_profile(vert_budgets={"weapon": "not_a_number"})
check("_studio_vert_budget ignores a non-numeric profile value and uses the fallback",
      server._studio_vert_budget("weapon", 15000) == 15000)

# ── _studio_tri_budget: normalized case/space/underscore matching ────────
server._load_studio_profile_dict = fake_profile(tri_budgets={"Giant Boss": 400000, "Axe/Hammer": 30000})
check("_studio_tri_budget matches case-insensitively", server._studio_tri_budget("giant boss") == 400000)
check("_studio_tri_budget matches underscore-normalized", server._studio_tri_budget("Giant_Boss") == 400000)
check("_studio_tri_budget matches a slash-containing key exactly", server._studio_tri_budget("axe/hammer") == 30000)
check("_studio_tri_budget returns None for no match, never a guess", server._studio_tri_budget("Dagger") is None)

server._load_studio_profile_dict = original_loader

# ── _estimate_triangle_count: exact vs. approximated ──────────────────────
exact = server._estimate_triangle_count(polygon_count=100, quad_ratio_pct=100.0, tris_pct=0.0)
check("all-quad mesh: exact triangle count is precisely 2x polygon_count",
      exact["estimated_tri_count"] == 200 and exact["exact"] is True)

exact_tris = server._estimate_triangle_count(polygon_count=100, quad_ratio_pct=0.0, tris_pct=100.0)
check("all-tri mesh: exact triangle count equals polygon_count",
      exact_tris["estimated_tri_count"] == 100 and exact_tris["exact"] is True)

mixed = server._estimate_triangle_count(polygon_count=100, quad_ratio_pct=50.0, tris_pct=30.0)
# 30 tri-faces*1 + 50 quad-faces*2 + 20 ngon-faces*3 = 30 + 100 + 60 = 190
check("mixed mesh with a real ngon remainder is flagged NOT exact",
      mixed["estimated_tri_count"] == 190 and mixed["exact"] is False)

none_stats = server._estimate_triangle_count(polygon_count=100, quad_ratio_pct=None, tris_pct=None)
check("missing topology stats (None) don't crash — treated as 0, fully ngon, approximated",
      none_stats["estimated_tri_count"] == 300 and none_stats["exact"] is False)

# ── production_review: granular tri path vs. broad vert path ─────────────
_DNA_RAW_BASE = {
    "get_object_info": {"name": "X", "mesh": {"vertices": 500000, "polygons": 100000}, "materials": []},
}


def make_review_fake(vert_budgets=None, tri_budgets=None, polygons=100000, quad_pct=100.0, tris_pct=0.0):
    def fake(cmd, **kwargs):
        if cmd == "get_object_info":
            return {"name": "X", "mesh": {"vertices": 500000, "polygons": polygons}, "materials": []}
        if cmd == "get_mesh_quality_report":
            return {"counts": {"verts": 500000, "faces": polygons}, "face_types": {"ngons": 0, "quads": 0, "tris": 0},
                    "uv": {"has_uvs": True}, "modifiers": []}
        if cmd == "detect_mesh_problems":
            return {"problems": []}
        if cmd == "analyze_topology":
            return {"topology_score": 90, "stats": {"quad_ratio_pct": quad_pct, "tris_pct": tris_pct}}
        if cmd == "run_unreal_readiness_check":
            return {"checks": {}}
        raise AssertionError(f"unexpected command: {cmd}")
    return fake, fake_profile(vert_budgets=vert_budgets, tri_budgets=tri_budgets)


server._capture_plain_screenshot = lambda name: None

# Granular match: "Giant Boss" tri_budget of 400000, real mesh way under -> no conflict
fake_send, fake_loader = make_review_fake(tri_budgets={"Giant Boss": 400000}, polygons=100000, quad_pct=100.0)
server._send_raw = fake_send
server._load_studio_profile_dict = fake_loader
out = json.loads(server.production_review(object_name="X", asset_type="Giant Boss"))
check("production_review succeeds (no crash) with a granular tri_budgets match", "error" not in out)
check("a mesh comfortably within its granular triangle budget reports no budget conflict",
      not any(c["conflict"] == "Triangle budget exceeded" for c in out.get("conflicts", [])))

# Granular match, real mesh WAY over the ceiling -> conflict fires with real numbers
fake_send, fake_loader = make_review_fake(tri_budgets={"Giant Boss": 1000}, polygons=100000, quad_pct=100.0)
server._send_raw = fake_send
server._load_studio_profile_dict = fake_loader
out = json.loads(server.production_review(object_name="X", asset_type="Giant Boss"))
tri_conflict = next((c for c in out.get("conflicts", []) if c["conflict"] == "Triangle budget exceeded"), None)
check("exceeding a granular triangle budget fires a real conflict with the estimated count",
      tri_conflict is not None and "200,000" in tri_conflict["data_shows"])

# No granular match -> falls through to the broad vertex-budget path unchanged
fake_send, fake_loader = make_review_fake(vert_budgets={}, tri_budgets={}, polygons=100000, quad_pct=100.0)
server._send_raw = fake_send
server._load_studio_profile_dict = fake_loader
out = json.loads(server.production_review(object_name="X", asset_type="hero_character"))
check("no granular tri_budgets match falls through to the broad vertex-budget path, no crash",
      "error" not in out)
vert_conflict = next((c for c in out.get("conflicts", []) if c["conflict"] == "Vertex budget exceeded"), None)
check("the broad vertex path fires using the real 500,000-vertex mesh against the hero_character limit",
      vert_conflict is not None)

server._load_studio_profile_dict = original_loader

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All checks passed.")

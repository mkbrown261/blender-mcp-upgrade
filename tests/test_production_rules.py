# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp[cli]", "pillow", "MaterialX"]
# ///
"""
Verifies each _PRODUCTION_RULES predicate fires under crafted synthetic Asset
DNA and does NOT fire when its condition isn't met — both directions, so a
rule can't silently regress into a false positive or a dead rule that never
fires. Pure logic test, no Blender/network involved (rules are pure Python).
Run: uv run tests/test_production_rules.py
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import server

failures = []


def check(name, condition):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}")
    if not condition:
        failures.append(name)


def fired_ids(dna):
    return {r["id"] for r in server._evaluate_production_rules(dna)}


BASE = {
    "target_engine": "unreal",
    "identity": {"category": None, "vertex_count": 1000, "polygon_count": 1000, "has_armature": True},
    "geometry": {"topology_score": 100, "lightmap_uv_present": True},
    "materials": [],
    "production": {"collision_mesh_present": True},
}

# ── high_poly_nanite ─────────────────────────────────────────────────────────
dna = {**BASE, "identity": {**BASE["identity"], "polygon_count": 300_000}}
check("high_poly_nanite fires above 250k polys on unreal target", "high_poly_nanite" in fired_ids(dna))
dna_low = {**BASE, "identity": {**BASE["identity"], "polygon_count": 10_000}}
check("high_poly_nanite does NOT fire under 250k polys", "high_poly_nanite" not in fired_ids(dna_low))

# ── topology_below_playbook_min (weapon min is 60) ──────────────────────────
dna = {**BASE, "identity": {**BASE["identity"], "category": "weapon"}, "geometry": {**BASE["geometry"], "topology_score": 40}}
check("topology_below_playbook_min fires when score is below the playbook's minimum",
      "topology_below_playbook_min" in fired_ids(dna))
dna_ok = {**BASE, "identity": {**BASE["identity"], "category": "weapon"}, "geometry": {**BASE["geometry"], "topology_score": 90}}
check("topology_below_playbook_min does NOT fire when score meets the minimum",
      "topology_below_playbook_min" not in fired_ids(dna_ok))
check("topology_below_playbook_min does NOT fire with no active playbook (category=None)",
      "topology_below_playbook_min" not in fired_ids(BASE))

# ── over_vert_budget (weapon budget is 15_000) ──────────────────────────────
dna = {**BASE, "identity": {**BASE["identity"], "category": "weapon", "vertex_count": 20_000}}
check("over_vert_budget fires above the playbook's vert_budget", "over_vert_budget" in fired_ids(dna))
dna_ok = {**BASE, "identity": {**BASE["identity"], "category": "weapon", "vertex_count": 5_000}}
check("over_vert_budget does NOT fire under the playbook's vert_budget", "over_vert_budget" not in fired_ids(dna_ok))

# ── missing_lightmap_uv (hero_char requires uv_channels >= 2) ───────────────
dna = {**BASE, "identity": {**BASE["identity"], "category": "hero_char"}, "geometry": {**BASE["geometry"], "lightmap_uv_present": False}}
check("missing_lightmap_uv fires when playbook needs 2 UV channels and lightmap UV is absent",
      "missing_lightmap_uv" in fired_ids(dna))
dna_ok = {**BASE, "identity": {**BASE["identity"], "category": "hero_char"}, "geometry": {**BASE["geometry"], "lightmap_uv_present": True}}
check("missing_lightmap_uv does NOT fire when lightmap UV is present",
      "missing_lightmap_uv" not in fired_ids(dna_ok))
dna_weapon = {**BASE, "identity": {**BASE["identity"], "category": "weapon"}, "geometry": {**BASE["geometry"], "lightmap_uv_present": False}}
check("missing_lightmap_uv does NOT fire for a playbook that only needs 1 UV channel (weapon)",
      "missing_lightmap_uv" not in fired_ids(dna_weapon))

# ── missing_pbr_maps ──────────────────────────────────────────────────────────
dna = {**BASE, "materials": [{"name": "M", "missing_maps": ["Roughness"]}]}
check("missing_pbr_maps fires when any material has missing_maps", "missing_pbr_maps" in fired_ids(dna))
dna_ok = {**BASE, "materials": [{"name": "M", "missing_maps": []}]}
check("missing_pbr_maps does NOT fire when no material is missing maps", "missing_pbr_maps" not in fired_ids(dna_ok))

# ── character_no_rig ─────────────────────────────────────────────────────────
dna = {**BASE, "identity": {**BASE["identity"], "category": "creature", "has_armature": False}}
check("character_no_rig fires for creature/hero_char playbooks with no armature",
      "character_no_rig" in fired_ids(dna))
dna_ok = {**BASE, "identity": {**BASE["identity"], "category": "creature", "has_armature": True}}
check("character_no_rig does NOT fire when an armature is present", "character_no_rig" not in fired_ids(dna_ok))
dna_prop = {**BASE, "identity": {**BASE["identity"], "category": "env_prop", "has_armature": False}}
check("character_no_rig does NOT fire for non-character playbooks (env_prop)",
      "character_no_rig" not in fired_ids(dna_prop))

# ── no_collision_mesh_prop ───────────────────────────────────────────────────
dna = {**BASE, "identity": {**BASE["identity"], "category": "env_prop"}, "production": {"collision_mesh_present": False}}
check("no_collision_mesh_prop fires for env_prop/weapon on unreal target with no collision",
      "no_collision_mesh_prop" in fired_ids(dna))
dna_ok = {**BASE, "identity": {**BASE["identity"], "category": "env_prop"}, "production": {"collision_mesh_present": True}}
check("no_collision_mesh_prop does NOT fire when collision mesh already exists",
      "no_collision_mesh_prop" not in fired_ids(dna_ok))
check("no_collision_mesh_prop recommendation explicitly defers to the user, never auto-generates",
      "ask the user" in next(r["recommendation"] for r in server._PRODUCTION_RULES if r["id"] == "no_collision_mesh_prop").lower())

# ── a fully clean synthetic asset should fire nothing ────────────────────────
clean = {
    "target_engine": "unreal",
    "identity": {"category": "weapon", "vertex_count": 5_000, "polygon_count": 5_000, "has_armature": False},
    "geometry": {"topology_score": 90, "lightmap_uv_present": True},
    "materials": [{"name": "M", "missing_maps": []}],
    "production": {"collision_mesh_present": True},
}
check("a clean, in-budget, fully-mapped weapon fires zero rules", fired_ids(clean) == set())

# ── malformed/partial DNA must not raise, just skip ──────────────────────────
try:
    ids = fired_ids({})
    check("an empty DNA dict does not raise and fires zero rules", ids == set())
except Exception as e:
    check(f"an empty DNA dict does not raise ({e})", False)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All checks passed.")

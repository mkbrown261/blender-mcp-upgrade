# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp[cli]", "pillow", "MaterialX"]
# ///
"""
Verifies split_blended_material — the fix for the couch-class problem: one
shared material silently doing multiple jobs (material_count: 1 hiding
leather + wood + fabric all baked together). Two things matter most: (1) the
gate — this tool refuses to run unless heterogeneity.likely_blended is
actually True (a real measured signal, not a guess), mirroring
bake_weathered_textures' topology gate exactly, with force=True as the
documented bypass; (2) the result is reported honestly — both materials
share the same node graph immediately after a split (this creates
addressability, not visual differentiation), and that's stated in the
result, not hidden behind an implied "materials are now different" claim.
Run: uv run tests/test_split_blended_material.py
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
    "get_object_info": {"name": "X", "materials": ["M"]},
}


def make_fake(heterogeneity, split_result=None, second_scan_entries=None):
    scan_calls = {"n": 0}

    def fake(cmd, **kwargs):
        if cmd in _DNA_RAW:
            return _DNA_RAW[cmd]
        if cmd == "execute_code_safe":
            code = kwargs.get("code", "")
            if '"original_material":' in code:  # the split script's own JSON output key —
                # NOT the bare substring "original_material", which now also matches
                # safe_bake_measure's "original_materials = list(mesh.materials)"
                # (embedded via _SAFE_MATERIAL_BAKE_SNIPPET into the PBR scan script too)
                return {"result": json.dumps(split_result)}
            if "has_principled" in code:
                scan_calls["n"] += 1
                # First scan is the gate check (before the split) — always
                # reflects the material's CURRENT heterogeneity. Any scan
                # after that is the post-split re-verification, which uses
                # second_scan_entries (both materials, real shape) when given.
                if scan_calls["n"] == 1 or second_scan_entries is None:
                    entries = [{
                        "name": "M", "has_principled": True, "texture_fed": ["Base Color"], "missing_maps": [],
                        "fingerprint": {}, "heterogeneity": heterogeneity,
                    }]
                else:
                    entries = second_scan_entries
                return {"result": json.dumps(entries)}
            if "filepath_raw" in code:
                return {"result": json.dumps({"path": None})}
        raise AssertionError(f"unexpected command: {cmd} / {kwargs.get('code','')[:50]}")
    return fake


# ── Gate: refuses to run when likely_blended is False, no force ──────────
server._send_raw = make_fake({"likely_blended": False, "island_count": 1, "note": "single island"})
server._SNAPSHOTS.clear()
out = json.loads(server.split_blended_material(object_name="X", material_name="M")[-1])
check("refuses to split when heterogeneity.likely_blended is False", "error" in out)
check("the refusal explains why via the real heterogeneity data", out.get("heterogeneity", {}).get("island_count") == 1)

# ── Gate: material not found on the object at all ────────────────────────
server._send_raw = make_fake({"likely_blended": False})
server._SNAPSHOTS.clear()
out = json.loads(server.split_blended_material(object_name="X", material_name="Nonexistent")[-1])
check("returns an error when the named material isn't on the object", "error" in out)

# ── Gate bypass: force=True runs the split script even when not flagged ──
split_ok = {
    "object": "X", "original_material": "M", "new_material": "M_split",
    "island_count": 3,
    "minority_group": {"island_count": 1, "faces_reassigned": 5, "avg_range": [0.1, 0.1]},
    "majority_group": {"island_count": 2, "faces_kept": 40, "avg_range": [0.6, 0.7]},
}
server._send_raw = make_fake({"likely_blended": False}, split_result=split_ok,
                              second_scan_entries=[
                                  {"name": "M", "has_principled": True, "texture_fed": ["Base Color"], "missing_maps": [], "fingerprint": {"roughness_avg": 0.5}, "heterogeneity": {}},
                                  {"name": "M_split", "has_principled": True, "texture_fed": ["Base Color"], "missing_maps": [], "fingerprint": {"roughness_avg": 0.5}, "heterogeneity": {}},
                              ])
server._SNAPSHOTS.clear()
out = json.loads(server.split_blended_material(object_name="X", material_name="M", force=True)[-1])
check("force=True bypasses the gate and runs the split even when not flagged", "error" not in out)
check("split result reports the new material's name", out.get("new_material") == "M_split")

# ── Normal path: flagged as blended, split succeeds, result is verified not assumed ──
server._send_raw = make_fake({"likely_blended": True, "island_count": 3, "color_variance": 0.03}, split_result=split_ok,
                              second_scan_entries=[
                                  {"name": "M", "has_principled": True, "texture_fed": ["Base Color"], "missing_maps": [], "fingerprint": {"roughness_avg": 0.5}, "heterogeneity": {}},
                                  {"name": "M_split", "has_principled": True, "texture_fed": ["Base Color"], "missing_maps": [], "fingerprint": {"roughness_avg": 0.5}, "heterogeneity": {}},
                              ])
server._SNAPSHOTS.clear()
out = json.loads(server.split_blended_material(object_name="X", material_name="M")[-1])
check("a real likely_blended=True material is allowed to split", "error" not in out)
check("faces_reassigned comes from the real script output, not assumed", out.get("minority_group", {}).get("faces_reassigned") == 5)
check("result carries RE-MEASURED fingerprints for both materials after the split, not assumed identical",
      "fingerprints_after_split" in out and set(out["fingerprints_after_split"].keys()) == {"M", "M_split"})
check("result honestly states both materials share the same graph immediately after splitting",
      "addressability" in out.get("note", "").lower())

# ── Structural checks on the real script sent to Blender: the trivial-split rejection fix ──
# Real live incident: the single-largest-gap alone peeled off exactly 1 face
# out of ~1.9M on the couch. Capture the ACTUAL script text (not the mocked
# JSON output above, which bypasses the in-script clustering entirely) to
# confirm the real fix — a minimum-fraction floor + walking candidate gaps
# largest-first — is actually present, not just documented.
captured_script = {"code": None}


def capture_fake(cmd, **kwargs):
    if cmd == "execute_code_safe":
        code = kwargs.get("code", "")
        if '"original_material":' in code:
            captured_script["code"] = code
            return {"result": json.dumps(split_ok)}
        if "has_principled" in code:
            return {"result": json.dumps([{
                "name": "M", "has_principled": True, "texture_fed": ["Base Color"], "missing_maps": [],
                "fingerprint": {}, "heterogeneity": {"likely_blended": True, "island_count": 3},
            }])}
        if "filepath_raw" in code:
            return {"result": json.dumps({"path": None})}
    if cmd in _DNA_RAW:
        return _DNA_RAW[cmd]
    raise AssertionError(f"unexpected command: {cmd}")


server._send_raw = capture_fake
server._SNAPSHOTS.clear()
server.split_blended_material(object_name="X", material_name="M")
script = captured_script["code"]
check("the real split script computes a minimum-fraction floor for the minority group, not just the raw largest gap",
      "MIN_SPLIT_FRACTION = 0.02" in script)
check("candidate gaps are walked largest-first, skipping ones whose minority is a stray fragment",
      "for _, split_at in gaps:" in script
      and "minority_faces / total_faces) >= MIN_SPLIT_FRACTION" in script)
# Real bug caught live: picking "minority" by ISLAND COUNT reassigned
# 1,898,711 of ~1,898,715 faces on the real couch to the "minority" group
# (2 large islands beat 3 small ones on island count, backwards on face
# count) — confirmed via a live run before this fix, corrected after.
check("minority/majority is chosen by real FACE COUNT, not island count — the exact bug caught live",
      "faces_a = sum(len(e[\"faces\"]) for e in cand_a)" in script
      and "faces_b = sum(len(e[\"faces\"]) for e in cand_b)" in script
      and "cand_minority = cand_a if faces_a < faces_b else cand_b" in script)
check("a real, honest rejection message names the fraction and explains why, when nothing qualifies",
      "every candidate gap's minority group" in script and "stray fragment" in script)

# ── The split script itself refuses when there's nothing meaningful to split ──
no_split_result = {"error": "Could not find a meaningful split — all islands landed in one group."}
server._send_raw = make_fake({"likely_blended": True, "island_count": 2}, split_result=no_split_result)
server._SNAPSHOTS.clear()
out = json.loads(server.split_blended_material(object_name="X", material_name="M")[-1])
check("when the underlying script can't find a meaningful split, that error surfaces honestly, not a forced split",
      "error" in out and "meaningful split" in out["error"])

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All checks passed.")

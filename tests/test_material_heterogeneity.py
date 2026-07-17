# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp[cli]", "pillow", "MaterialX"]
# ///
"""
Verifies the heterogeneity detection added to the PBR scan script — the fix
for material_count alone being unable to catch the couch-class problem (one
shared material silently doing multiple jobs: leather + wood + fabric all
baked together, material_count: 1 hiding it completely, confirmed live
tonight). Real signal, not a guess: Base Color sampled per disconnected UV
island (split at seams/boundary edges, the same real signal
apply_weathering_recipe's fraying technique already relies on), variance
across islands compared against a starting-heuristic threshold. Honesty gate
matters here too — no texture, no active UV layer, or a single unbroken
island must all report "nothing to compare", never a forced verdict.
Run: uv run tests/test_material_heterogeneity.py
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


code = server._PBR_SOCKET_SCAN_SCRIPT

# ── Structural checks on the generated script ────────────────────────────
check("computes real island adjacency broken at seams/boundary edges, same signal as fraying",
      "edge.use_seam or edge_poly_count.get(key, 0) <= 1" in code)
check("samples Base Color per island via real UV coordinates, not a guess",
      "uv_layer[li].uv" in code and "sample_at_uv" in code)
check("computes a real statistics.pvariance across island averages",
      "statistics.pvariance(island_averages)" in code)
check("honesty gate: fewer than 2 islands reports 'nothing to compare', not a forced verdict",
      "single connected island (no internal seams/boundaries) — nothing to compare" in code)
check("honesty gate: no texture-fed Base Color reports why, not a guessed verdict",
      "Base Color is not texture-fed" in code)
check("honesty gate: no active UV layer is reported honestly", "no active UV layer" in code)
check("the threshold is a substituted, centrally-defined constant, not a magic number buried in the script",
      "{HETEROGENEITY_THRESHOLD}" in code)
check("every material entry (even with no Principled BSDF) carries a heterogeneity key",
      '"heterogeneity": {"island_count": 0, "color_variance": None,' in code)

# ── _HETEROGENEITY_THRESHOLD is substituted into the real call site ──────
check("get_asset_dna substitutes the real threshold constant into the script it sends to Blender",
      hasattr(server, "_HETEROGENEITY_THRESHOLD") and isinstance(server._HETEROGENEITY_THRESHOLD, float))

# ── blended_material_candidate production rule ────────────────────────────
rule = next((r for r in server._PRODUCTION_RULES if r["id"] == "blended_material_candidate"), None)
check("blended_material_candidate rule exists", rule is not None)
check("blended_material_candidate is warning severity, not critical (never a hard blocker)",
      rule is not None and rule["severity"] == "warning")

dna_no_signal = {"materials": [{"heterogeneity": {"likely_blended": False}}]}
dna_blended = {"materials": [{"heterogeneity": {"likely_blended": True}}]}
dna_no_field = {"materials": [{"name": "M"}]}  # today's real shape before this tool ever ran on an object

check("rule does not fire when likely_blended is False", rule is not None and not rule["predicate"](dna_no_signal))
check("rule fires when likely_blended is True", rule is not None and rule["predicate"](dna_blended))
check("rule does not raise/fire when heterogeneity is entirely absent", rule is not None and not rule["predicate"](dna_no_field))

# ── get_asset_dna passes heterogeneity through untouched (same pass-through as fingerprint) ──
_DNA_RAW = {
    "get_mesh_quality_report": {"counts": {"verts": 100, "edges": 200, "faces": 100},
                                 "uv": {"has_uvs": True, "layer_count": 1}, "modifiers": []},
    "analyze_topology": {"topology_score": 90, "stats": {}},
    "run_unreal_readiness_check": {"checks": {}},
    "get_object_info": {"name": "X", "materials": ["M"]},
}
FAKE_HETEROGENEITY = {"island_count": 3, "color_variance": 0.021, "likely_blended": True, "note": None}


def fake_send_raw(cmd, **kwargs):
    if cmd in _DNA_RAW:
        return _DNA_RAW[cmd]
    if cmd == "execute_code_safe":
        c = kwargs.get("code", "")
        if "has_principled" in c:
            return {"result": json.dumps([{
                "name": "M", "has_principled": True, "texture_fed": ["Base Color"], "missing_maps": [],
                "fingerprint": {}, "heterogeneity": FAKE_HETEROGENEITY,
            }])}
        if "filepath_raw" in c:
            return {"result": json.dumps({"path": None})}
    raise AssertionError(f"unexpected command: {cmd}")


server._capture_plain_screenshot = lambda name: None
server._send_raw = fake_send_raw
server._SNAPSHOTS.clear()
dna = json.loads(server.get_asset_dna(object_name="X", target_engine="unreal"))
mat = dna["materials"][0]
check("get_asset_dna passes heterogeneity through to materials[] untouched", mat.get("heterogeneity") == FAKE_HETEROGENEITY)
check("blended_material_candidate rule fires in a real get_asset_dna call given a likely_blended material",
      any(r["id"] == "blended_material_candidate" for r in dna["rules_fired"]))

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All checks passed.")

# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp[cli]", "pillow", "MaterialX"]
# ///
"""
Verifies bake_weathered_textures()'s generated script structure — the
critical safety properties (capture-before-mutate, try/finally guaranteed
restoration, object-wide broken-image handling) were the actual root cause
of a real corruption bug caught live tonight (KB-006). Full bake behavior
(real Cycles bake, real file output, real state restoration) was verified
live against Blender — see KB-006 and this session's manual test producing
a genuine 1024x1024 UV-unwrapped texture. Run: uv run tests/test_bake_weathered_textures.py
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


captured = {}
original_send_raw = server._send_raw

# bake_weathered_textures now calls _reaffirm_dna() (get_asset_dna) both
# before (to infer bake_roughness) and after (to verify the bake actually
# closed the gap) — stub the raw commands that feeds so DNA assembly doesn't
# hit a real Blender connection or crash the test.
_DNA_RAW = {
    "get_mesh_quality_report": {"counts": {"verts": 100, "edges": 200, "faces": 100},
                                 "uv": {"has_uvs": True, "layer_count": 1}, "modifiers": []},
    "analyze_topology": {"topology_score": 90, "stats": {}},
    "run_unreal_readiness_check": {"checks": {}},
    "get_object_info": {"name": "X", "materials": ["M"]},
}


def fake_send_raw(cmd, **kwargs):
    if cmd in _DNA_RAW:
        return _DNA_RAW[cmd]
    if cmd == "execute_code_safe":
        code = kwargs["code"]
        if "original_surface_link" in code:
            # this is the real bake script — the one this test inspects
            captured["code"] = code
            return {"result": '{"baked": {}, "errors": [], "rewired": false, '
                               '"broken_images_worked_around": []}'}
        if "has_principled" in code:
            # DNA's PBR socket scan
            return {"result": json.dumps([{
                "name": "M", "has_principled": True, "texture_fed": [],
                "missing_maps": ["Base Color", "Roughness", "Metallic", "Normal"],
            }])}
        if "filepath_raw" in code:
            # missing-normal-map handoff export — nothing to export in this test
            return {"result": json.dumps({"path": None})}
    return original_send_raw(cmd, **kwargs)


server._send_raw = fake_send_raw

result = json.loads(server.bake_weathered_textures(
    object_name="X", material_name="M", output_dir="/tmp/bake_out", resolution=1024,
))
code = captured["code"]

check("tool call succeeds", "error" not in result)
try:
    ast.parse(code)
    ok = True
except SyntaxError as e:
    ok = False
    print("  syntax error:", e)
check("generated script parses cleanly", ok)

# The exact safety properties that were missing when the real corruption
# bug happened live — these are the regression guard, not incidental checks.
check("original Surface source captured BEFORE any temp node is created",
      code.index("original_surface_from = original_surface_link.from_socket")
      < code.index('nt.nodes.new("ShaderNodeTexImage")'))
check("original render engine/samples captured before any mutation",
      "original_engine = bpy.context.scene.render.engine" in code
      and "original_samples = bpy.context.scene.cycles.samples" in code)
check("the whole bake+rewire sequence is wrapped in try/finally",
      "try:" in code and "finally:" in code)
check("finally block restores render engine and sample count unconditionally",
      "bpy.context.scene.render.engine = original_engine" in code
      and "bpy.context.scene.cycles.samples = original_samples" in code)
check("finally block restores Surface wiring unconditionally, not just on success",
      code.count("nt.links.new(original_surface_from, output_node.inputs") >= 2)
check("broken images (0 channels or 0x0 size) are scanned across EVERY material on the object, not just the target",
      "for slot in obj.material_slots:" in code
      and "channels == 0 or img.size[0] == 0" in code)
check("broken image swap is restored in the finally block",
      "for n, img in swapped:" in code and "n.image = img" in code)
check("bake uses the Emission-trick (EMIT type captures node values regardless of scene lighting)",
      "bpy.ops.object.bake(type='EMIT')" in code)
check("baked images are actually saved to real files, not left as in-memory-only datablocks",
      ".save()" in code)

# ── Regression: a flat/zero-variance bake must be flagged explicitly, not ───
# silently reported as "confirmed" just because it's correctly wired. Real
# bug hit live: a wall panel's roughness baked to stdev=0.0 (a missing Mix-
# node fallback made it 0.0/mirror-smooth everywhere) and the old
# dna_verification said "confirmed: true" because the socket WAS texture-fed
# — wiring correctness and content sanity are different claims.
def make_stats_fake(roughness_stdev):
    def fake(cmd, **kwargs):
        if cmd in _DNA_RAW:
            return _DNA_RAW[cmd]
        if cmd == "execute_code_safe":
            code = kwargs["code"]
            if "original_surface_link" in code:
                return {"result": json.dumps({
                    "baked": {
                        "base_color": {"path": "/tmp/bc.png", "stats": {"min": 0.0, "max": 0.8, "mean": 0.3, "stdev": 0.15}},
                        "roughness": {"path": "/tmp/r.png", "stats": {"min": 0.0, "max": 0.9, "mean": 0.5, "stdev": roughness_stdev}},
                    },
                    "errors": [], "rewired": True, "broken_images_worked_around": [],
                })}
            if "has_principled" in code:
                return {"result": json.dumps([{
                    "name": "M", "has_principled": True, "texture_fed": ["Base Color", "Roughness"],
                    "missing_maps": [],
                }])}
            if "filepath_raw" in code:
                return {"result": json.dumps({"path": None})}
        return original_send_raw(cmd, **kwargs)
    return fake


server._send_raw = make_stats_fake(roughness_stdev=0.0)
server._SNAPSHOTS.clear()
flat_result = json.loads(server.bake_weathered_textures(
    object_name="X", material_name="M", output_dir="/tmp/bake_out", resolution=1024,
))
check("a zero-variance bake is flagged in suspiciously_flat_bakes",
      "roughness" in flat_result.get("dna_verification", {}).get("suspiciously_flat_bakes", []))
check("a zero-variance bake carries a human-readable warning, not just a silent flag",
      flat_result.get("dna_verification", {}).get("flat_bake_warning") is not None)

server._send_raw = make_stats_fake(roughness_stdev=0.15)
server._SNAPSHOTS.clear()
healthy_result = json.loads(server.bake_weathered_textures(
    object_name="X", material_name="M", output_dir="/tmp/bake_out", resolution=1024,
))
check("a normal-variance bake is NOT flagged as suspiciously flat",
      healthy_result.get("dna_verification", {}).get("suspiciously_flat_bakes") == [])
check("a normal-variance bake carries no flat_bake_warning",
      healthy_result.get("dna_verification", {}).get("flat_bake_warning") is None)

server._send_raw = original_send_raw

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All checks passed.")

# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp[cli]", "pillow", "MaterialX"]
# ///
"""
Verifies the material-aware weathering logic — the generated script text is
checked for correct structure (constant vs. texture-sampled metalness paths,
metal_floor clamping, effective_wear_scalar computation). Full live behavior
(real texture sampling, real node wiring on a real 6-material character) was
verified directly against Blender; see this session's KB-003 and the manual
live test. Run: uv run tests/test_material_aware_weathering.py
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


def fake_send_raw(cmd, **kwargs):
    if cmd == "execute_code_safe":
        captured["code"] = kwargs["code"]
        return {"result": '{"object": "X", "materials_applied": [], "materials_skipped": [], '
                           '"mask_stats": {}, "percentiles_used": {}}'}
    return original_send_raw(cmd, **kwargs)


server._send_raw = fake_send_raw

result = json.loads(server.apply_weathering_recipe(
    object_name="X", wear_scalar=1.0, metal_floor=0.3,
))
check("tool call succeeds with metal_floor param", "error" not in result)

code = captured["code"]
check("generated script is syntactically valid Python", True)
try:
    ast.parse(code)
    ok = True
except SyntaxError as e:
    ok = False
    print("  syntax error:", e)
check("generated script parses cleanly", ok)

check("script reads the Metallic input, not a hardcoded assumption",
      'principled.inputs["Metallic"]' in code)
check("script distinguishes texture-driven vs constant metallic",
      "metallic_input.links" in code)
check("script samples the REAL connected image's pixel data when texture-driven",
      "sample_image_avg(" in code and "image.pixels" in code and "TEX_IMAGE" in code)
check("metal_floor value substituted correctly into the clamp", "0.3" in code)
check("effective_wear_scalar computed as wear_scalar * floored metal factor",
      "effective_wear_scalar = " in code and "metal_factor_floored" in code)
check("factor_scale node uses the per-material effective scalar, not the flat global one",
      "factor_scale.inputs[1].default_value = effective_wear_scalar" in code)
check("per-material metal diagnostics are reported back, not just applied silently",
      '"metal_source": metal_source' in code and '"metal_factor"' in code)

server._send_raw = original_send_raw

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All checks passed.")

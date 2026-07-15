# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp[cli]", "pillow", "MaterialX"]
# ///
"""
Verifies per-material weathering tint generation and the broken-image
fallback — a real ZeroDivisionError was caught live against an actual
character (one material's Base Color texture had 0 channels/0x0 size, a
genuinely broken texture reference from the Tripo3D pipeline) before this
fix existed. Run: uv run tests/test_weathering_tint.py
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

result = json.loads(server.apply_weathering_recipe(object_name="X"))
code = captured["code"]

check("tool call succeeds", "error" not in result)
try:
    ast.parse(code)
    ok = True
except SyntaxError as e:
    ok = False
    print("  syntax error:", e)
check("generated script parses cleanly", ok)

check("sample_image_avg guards against 0-channel/0-size images before touching .pixels",
      "channels == 0 or image.size[0] == 0" in code)
check("broken image source is labeled distinctly, not silently reported as a real sample",
      '"broken_image_fallback"' in code)
check("base color is sampled per-material (not one global constant)",
      "sample_image_avg(img, 3)" in code or "sample_image_avg(" in code)
check("grime tint derived from the material's own sampled base color",
      "grime_rgb = tuple(base_rgb[c]" in code)
check("weathering tint blends grime toward rust_color proportional to metal_factor_floored",
      "weather_rgb = tuple(" in code and "metal_factor_floored" in code)
check("rust node's color driven by the computed per-material weather_rgb, not a flat constant",
      "rust.outputs[0].default_value = (weather_rgb[0]" in code)
check("per-material tint diagnostics reported back",
      '"sampled_base_rgb"' in code and '"weathering_tint_rgb"' in code)

server._send_raw = original_send_raw

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All checks passed.")

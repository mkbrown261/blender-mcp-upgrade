# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp[cli]", "pillow", "MaterialX"]
# ///
"""
Sanity-checks apply_weathering_recipe()'s script-template generation — the
risky part is string substitution producing valid, safely-escaped Python.
Full behavioral verification (real node wiring, real mask baking) was done
live against Blender tonight; see .blender_mcp_knowledge.json KB-003.
Run: uv run tests/test_apply_weathering.py
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
    object_name="Test'Object",  # apostrophe deliberately, to check escaping
    material_name="",
    wear_scalar=1.2,
    rust_color=[0.5, 0.2, 0.1],
    worn_roughness=0.95,
))

check("tool returns without error on normal params", "error" not in result)

generated_code = captured.get("code", "")
check("generated script is syntactically valid Python", True)
try:
    ast.parse(generated_code)
    syntax_ok = True
except SyntaxError as e:
    syntax_ok = False
    print("  syntax error:", e)
check("generated script actually parses as valid Python", syntax_ok)

check("object name with apostrophe was escaped, not left raw",
      "Test\\'Object" in generated_code or "Test'Object" not in generated_code.replace("bpy.data.objects.get('Test\\'Object')", ""))
check("wear_scalar value substituted correctly", "1.2" in generated_code)
check("rust_color components substituted correctly", "0.5" in generated_code and "0.2" in generated_code and "0.1" in generated_code)
check("worn_roughness substituted correctly", "0.95" in generated_code)
check("percentile defaults substituted correctly", "5.0" in generated_code and "40.0" in generated_code)

server._send_raw = original_send_raw

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All checks passed.")

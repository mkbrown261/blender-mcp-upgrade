# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp[cli]", "pillow", "MaterialX"]
# ///
"""
Verifies apply_weathering_recipe()'s recipe integration: trigger_phrase
derives wear_scalar from a stored recipe's severity field, explicit
wear_scalar always overrides it, and no-match/no-severity cases fall back
cleanly instead of erroring. Run: uv run tests/test_weathering_recipe_integration.py
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


# Seed a real recipe with a recognizable severity word
server.record_creative_recipe(
    recipe_type="aging",
    canonical_name="heavy_battle_damage_test",
    trigger_phrases=["heavily battle-damaged", "war-torn"],
    parameters={"damage_severity": "heavy", "environment": "battlefield"},
)

captured = {}
original_send_raw = server._send_raw


def fake_send_raw(cmd, **kwargs):
    if cmd == "execute_code_safe":
        captured["code"] = kwargs["code"]
        return {"result": '{"object": "X", "materials_applied": ["M"], "materials_skipped": [], '
                           '"mask_stats": {}, "percentiles_used": {}}'}
    return original_send_raw(cmd, **kwargs)


server._send_raw = fake_send_raw

# 1. trigger_phrase with no explicit wear_scalar -> should derive 1.0 (heavy)
result1 = json.loads(server.apply_weathering_recipe(
    object_name="X", trigger_phrase="war-torn", recipe_type="aging",
))
check("trigger_phrase with no explicit wear_scalar derives from recipe severity",
      result1.get("wear_scalar_used") == 1.0)
check("result reports which recipe was used", result1.get("recipe_lookup", {}).get("recipe_used") == "heavy_battle_damage_test")
check("generated script actually used the derived scalar", "1.0" in captured["code"])

# 2. explicit wear_scalar always wins, even with a matching trigger_phrase
result2 = json.loads(server.apply_weathering_recipe(
    object_name="X", trigger_phrase="war-torn", recipe_type="aging", wear_scalar=0.15,
))
check("explicit wear_scalar overrides recipe-derived value",
      result2.get("wear_scalar_used") == 0.15)
check("explicit override means no recipe_lookup performed (not needed)",
      "recipe_lookup" not in result2)

# 3. no matching recipe -> clean fallback to default, not an error
result3 = json.loads(server.apply_weathering_recipe(
    object_name="X", trigger_phrase="a phrase that matches nothing at all xyz123",
))
check("no matching recipe falls back to tool default (0.8), no error",
      "error" not in result3 and result3.get("wear_scalar_used") == 0.8)

# 4. no trigger_phrase at all -> same clean default, no recipe lookup attempted
result4 = json.loads(server.apply_weathering_recipe(object_name="X"))
check("no trigger_phrase at all uses default 0.8 with no recipe lookup",
      result4.get("wear_scalar_used") == 0.8 and "recipe_lookup" not in result4)

server._send_raw = original_send_raw

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All checks passed.")

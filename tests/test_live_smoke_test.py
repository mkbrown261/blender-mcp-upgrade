# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp[cli]", "pillow", "MaterialX"]
# ///
"""
Verifies live_material_smoke_test's OWN control flow — setup/cleanup
dispatch, sentinel-tolerance comparison, and verdict aggregation. The
three tools it calls (generate_procedural_material, apply_weathering_recipe,
bake_weathered_textures) are already thoroughly tested in their own files;
here they're monkeypatched to isolate what THIS tool is responsible for.

The one property that actually matters and gets the most scrutiny: a smoke
test that can never fail isn't a real check. This file explicitly proves
the tool reports FAIL when the sentinel material is corrupted (the exact
regression class the real black-couch incident was) — not just that it
reports PASS on a clean run.
Run: uv run tests/test_live_smoke_test.py
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


def make_fake(sentinel_sequence):
    """sentinel_sequence: list of [r,g,b] (or None) popped in order for
    successive sentinel-sampling calls (initial, after-gen, after-bake)."""
    seq = list(sentinel_sequence)
    calls = {"setup": 0, "cleanup": 0, "sample": 0}

    def fake(cmd, **kwargs):
        if cmd == "execute_code_safe":
            code = kwargs.get("code", "")
            if "sentinel image not found" in code:
                calls["sample"] += 1
                rgb = seq.pop(0) if seq else None
                if rgb is None:
                    return {"result": json.dumps({"error": "sentinel image not found"})}
                return {"result": json.dumps({"rgb": rgb})}
            if "primitive_cube_add" in code:
                calls["setup"] += 1
                return {"result": json.dumps({"ok": True})}
            if "bpy.data.objects.remove(obj, do_unlink=True)" in code:
                calls["cleanup"] += 1
                return {"result": json.dumps({"ok": True})}
        raise AssertionError(f"unexpected command: {cmd} / {kwargs.get('code','')[:60]}")
    return fake, calls


server._capture_plain_screenshot = lambda name: None
original_gen = server.generate_procedural_material
original_weather = server.apply_weathering_recipe
original_bake = server.bake_weathered_textures


def restore():
    server.generate_procedural_material = original_gen
    server.apply_weathering_recipe = original_weather
    server.bake_weathered_textures = original_bake


# ── Clean run: sentinel unchanged at every stage, all tool calls succeed -> PASS ──
server._send_raw, calls = make_fake([[0.8, 0.2, 0.1], [0.8, 0.2, 0.1], [0.8, 0.2, 0.1]])
server.generate_procedural_material = lambda **kw: [json.dumps({"ok": True})]
server.apply_weathering_recipe = lambda **kw: [json.dumps({"materials_applied": ["M"]})]
server.bake_weathered_textures = lambda **kw: [json.dumps({"baked": {"base_color": {}}})]
out = json.loads(server.live_material_smoke_test())
check("a clean run (sentinel unchanged, no tool errors) reports verdict=PASS", out.get("verdict") == "PASS")
check("both checks report pass=True on a clean run", all(c["pass"] for c in out.get("checks", [])))
check("cleanup runs by default", calls["cleanup"] == 1)
restore()

# ── THE critical property: sentinel corruption after generate_procedural_material is CAUGHT ──
server._send_raw, calls = make_fake([[0.8, 0.2, 0.1], [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]])
server.generate_procedural_material = lambda **kw: [json.dumps({"ok": True})]
server.apply_weathering_recipe = lambda **kw: [json.dumps({"materials_applied": ["M"]})]
server.bake_weathered_textures = lambda **kw: [json.dumps({"baked": {"base_color": {}}})]
out = json.loads(server.live_material_smoke_test())
check("sentinel corruption after generate_procedural_material is CAUGHT — verdict=FAIL, not silently PASS",
      out.get("verdict") == "FAIL")
check("the specific check that failed is the generate_procedural_material one",
      out["checks"][0]["pass"] is False and "generate_procedural_material" in out["checks"][0]["check"])
restore()

# ── Corruption surfacing only at the weathering/bake stage is ALSO caught ──
server._send_raw, calls = make_fake([[0.8, 0.2, 0.1], [0.8, 0.2, 0.1], [0.0, 0.0, 0.0]])
server.generate_procedural_material = lambda **kw: [json.dumps({"ok": True})]
server.apply_weathering_recipe = lambda **kw: [json.dumps({"materials_applied": ["M"]})]
server.bake_weathered_textures = lambda **kw: [json.dumps({"baked": {"base_color": {}}})]
out = json.loads(server.live_material_smoke_test())
check("corruption surfacing only at the weathering/bake stage is caught, not masked by an earlier pass",
      out.get("verdict") == "FAIL" and out["checks"][0]["pass"] is True and out["checks"][1]["pass"] is False)
restore()

# ── An underlying tool reporting its OWN error still fails the check, even if pixels happen to match ──
server._send_raw, calls = make_fake([[0.8, 0.2, 0.1], [0.8, 0.2, 0.1], [0.8, 0.2, 0.1]])
server.generate_procedural_material = lambda **kw: [json.dumps({"error": "something broke"})]
server.apply_weathering_recipe = lambda **kw: [json.dumps({"materials_applied": ["M"]})]
server.bake_weathered_textures = lambda **kw: [json.dumps({"baked": {"base_color": {}}})]
out = json.loads(server.live_material_smoke_test())
check("an underlying tool's own error fails the check even when the sentinel pixels happen to match",
      out.get("verdict") == "FAIL" and out["checks"][0]["pass"] is False)
restore()

# ── Setup failure is reported honestly, no checks fabricated ──
# The finally block's cleanup call still runs defensively (harmless/idempotent
# in real Blender even when setup never created anything) — the mock allows it.
def fake_setup_fails(cmd, **kwargs):
    code = kwargs.get("code", "")
    if cmd == "execute_code_safe" and "primitive_cube_add" in code:
        return {"error": "Blender connection lost"}
    if cmd == "execute_code_safe" and "bpy.data.objects.remove(obj, do_unlink=True)" in code:
        return {"result": json.dumps({"ok": True})}
    raise AssertionError(f"unexpected command after setup fails: {cmd}")


server._send_raw = fake_setup_fails
server.generate_procedural_material = lambda **kw: [json.dumps({"ok": True})]
out = json.loads(server.live_material_smoke_test())
check("a setup failure reports verdict=SETUP_FAILED, not a fabricated PASS/FAIL over nothing",
      out.get("verdict") == "SETUP_FAILED")
check("no checks are fabricated when setup never completed", out.get("checks") == [])
restore()

# ── cleanup=False skips the cleanup call ──
server._send_raw, calls = make_fake([[0.8, 0.2, 0.1], [0.8, 0.2, 0.1], [0.8, 0.2, 0.1]])
server.generate_procedural_material = lambda **kw: [json.dumps({"ok": True})]
server.apply_weathering_recipe = lambda **kw: [json.dumps({"materials_applied": ["M"]})]
server.bake_weathered_textures = lambda **kw: [json.dumps({"baked": {"base_color": {}}})]
server.live_material_smoke_test(cleanup=False)
check("cleanup=False skips the cleanup call, leaving the object for manual inspection", calls["cleanup"] == 0)
restore()

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All checks passed.")

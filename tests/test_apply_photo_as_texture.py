# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp[cli]", "pillow", "MaterialX"]
# ///
"""
Verifies apply_photo_as_texture — the "just use the real photo" material
path, added after match_material_from_photo/apply_photo_material_match's
2-color procedural noise blend proved unusable on a visually complex photo
(flaking rust): a real, live-caught bug where roughness/normal image data
silently zeroed out (setting colorspace_settings.name AFTER foreach_set
resets a generated image's pixel buffer to zero in Blender's own API) made
the material read as a full mirror, reflecting the world HDRI instead of
showing the intended matte texture. Fixed by setting colorspace_settings
BEFORE writing pixels. This test locks in the ORDERING, since the bug is
silent (no exception, wrong visual result only) and easy to reintroduce.
Run: uv run tests/test_apply_photo_as_texture.py
"""
import sys, os, json, re
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import server

failures = []


def check(name, condition):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}")
    if not condition:
        failures.append(name)


server._capture_plain_screenshot = lambda name: None

# ── Missing reference image -> honest error, no Blender call attempted ──
calls = {"n": 0}


def fake_should_not_be_called(cmd, **kwargs):
    calls["n"] += 1
    raise AssertionError("execute_code_safe should not be called when the file doesn't exist")


server._send_raw = fake_should_not_be_called
out = json.loads(server.apply_photo_as_texture(
    object_name="X", material_name="M", reference_image_path="/tmp/does_not_exist_photo_12345.png")[-1])
check("missing reference image returns an error", "error" in out)
check("no Blender call is attempted for a file that doesn't exist locally", calls["n"] == 0)

# ── Regression lock: colorspace_settings MUST be set before pixels.foreach_set ──
# The real bug: reversing this order silently zeros the generated image's
# pixel buffer (readback all 0.0), which reads as roughness=0 (full mirror)
# with no exception anywhere -- caught only by actually looking at a live
# render (a shiny cube reflecting the world HDRI instead of showing rust).
this_dir = os.path.dirname(__file__)
server_src = open(os.path.join(this_dir, "..", "server.py"), encoding="utf-8").read()

# Find the apply_photo_as_texture tool's embedded Blender-side script body.
start = server_src.index("def apply_photo_as_texture(")
end = server_src.index("\n@mcp.tool()", start)
tool_src = server_src[start:end]

for label, img_var in [("roughness", "rough_img"), ("normal", "normal_img")]:
    colorspace_pos = tool_src.find(f"{img_var}.colorspace_settings.name")
    foreach_set_pos = tool_src.find(f"{img_var}.pixels.foreach_set")
    check(f"{label} map: colorspace_settings.name is set BEFORE pixels.foreach_set "
          f"(reversing this order silently zeros the pixel buffer -- a real bug caught live)",
          colorspace_pos != -1 and foreach_set_pos != -1 and colorspace_pos < foreach_set_pos)

# ── Live-behavior simulation: fake Blender side, confirm result shape or Blender-error passthrough ──
def make_fake_ok():
    def fake(cmd, **kwargs):
        if cmd == "execute_code_safe":
            return {"result": json.dumps({
                "base_color": {"width": 1024, "height": 1024},
                "roughness_generated": {"min": 0.35, "max": 0.9, "note": "heuristic..."},
                "normal_generated": {"strength": 2.0, "note": "gradient..."},
                "object": "X", "material": "M", "created_material": False,
                "faces_using_material": 6, "auto_assigned_all_faces": False,
                "had_uvs_already": True, "auto_unwrapped": False,
            })}
        raise AssertionError(f"unexpected command: {cmd}")
    return fake


import tempfile
tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
tmp.write(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
tmp.close()
try:
    server._send_raw = make_fake_ok()
    out = json.loads(server.apply_photo_as_texture(
        object_name="X", material_name="M", reference_image_path=tmp.name)[-1])
    check("valid run reports both roughness_generated and normal_generated honestly",
          "roughness_generated" in out and "normal_generated" in out)
    check("UV state is reported honestly (had_uvs_already/auto_unwrapped)",
          "had_uvs_already" in out and "auto_unwrapped" in out)

    # numpy unavailable in Blender's Python -> explicit error, not a silent skip
    def fake_no_numpy(cmd, **kwargs):
        return {"result": json.dumps({"error": "numpy not available in Blender's Python -- cannot generate roughness/normal maps."})}
    server._send_raw = fake_no_numpy
    out = json.loads(server.apply_photo_as_texture(
        object_name="X", material_name="M", reference_image_path=tmp.name)[-1])
    check("numpy-unavailable is surfaced as an explicit error, never silently skipped", "error" in out)
finally:
    os.unlink(tmp.name)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All checks passed.")

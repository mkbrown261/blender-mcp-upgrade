# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp[cli]", "pillow", "MaterialX"]
# ///
"""
Exercises export_material_as_materialx() against a REALISTIC mocked
get_material_graph() response — no Blender/bpy needed, since the translation
logic and MaterialX file writing are both pure Python. Verifies the actual
MaterialX SDK API calls used in _write_materialx_document work, not just
that the JSON shape looks right. Run: uv run tests/test_materialx_export.py
"""
import sys, os, json, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import server

failures = []


def check(name, condition):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}")
    if not condition:
        failures.append(name)


# Realistic graph: Principled BSDF with an image-texture base color, a
# procedural (unsupported) roughness input, and constant metallic — the
# exact mixed case this tool has to handle honestly, not just the easy path.
mock_graph = {
    "material": "M_TestRust",
    "nodes": [
        {"name": "Principled BSDF", "type": "BSDF_PRINCIPLED", "active": True,
         "inputs": {"Metallic": 0.0, "Roughness": 0.5, "IOR": 1.45, "Alpha": 1.0}},
        {"name": "Image Texture", "type": "TEX_IMAGE", "active": True,
         "image": "rust_basecolor.png", "filepath": "/tmp/rust_basecolor.png", "colorspace": "sRGB"},
        {"name": "Noise Texture", "type": "TEX_NOISE", "active": True, "inputs": {}},
        {"name": "Material Output", "type": "OUTPUT_MATERIAL", "active": True},
    ],
    "links": [
        {"from": "Image Texture.Color", "to": "Principled BSDF.Base Color"},
        {"from": "Noise Texture.Fac", "to": "Principled BSDF.Roughness"},
        {"from": "Principled BSDF.BSDF", "to": "Material Output.Surface"},
    ],
    "orphaned_nodes": [],
    "has_orphaned_nodes": False,
}

# Monkeypatch _send_raw so the tool runs without a live Blender connection —
# this is testing the translation/file-writing logic, not the TCP transport.
original_send_raw = server._send_raw
server._send_raw = lambda cmd, **kwargs: mock_graph if cmd == "get_material_graph" else original_send_raw(cmd, **kwargs)

result = json.loads(server.export_material_as_materialx(material_name="M_TestRust"))

check("material is marked supported (has an active Principled BSDF)", result.get("supported") is True)
check("base_color correctly resolved from the connected image texture",
      result["properties"].get("base_color", {}).get("type") == "image_texture"
      and result["properties"]["base_color"]["filepath"] == "/tmp/rust_basecolor.png")
check("metalness correctly resolved from the constant Principled input",
      result["properties"].get("metalness") == {"type": "constant", "value": 0.0})
check("roughness fed by a procedural Noise Texture is flagged unsupported, not mistranslated",
      any(u["property"] == "specular_roughness" for u in result["unsupported_inputs"])
      and "specular_roughness" not in result["properties"])
check("IOR (unconnected, constant-only input) still resolves correctly",
      result["properties"].get("specular_IOR") == {"type": "constant", "value": 1.45})

# Now the real MaterialX SDK write — this is the part most likely to have a
# wrong API call, so it's worth verifying for real rather than trusting the
# happy-path JSON alone.
with tempfile.TemporaryDirectory() as tmpdir:
    out_path = os.path.join(tmpdir, "test_material.mtlx")
    write_result = json.loads(server.export_material_as_materialx(
        material_name="M_TestRust", write_file=True, output_path=out_path,
    ))
    check("write_file=True reports a written path, not a file_write_error",
          write_result.get("file_written") == out_path and "file_write_error" not in write_result)
    check(".mtlx file actually exists on disk", os.path.exists(out_path))
    if os.path.exists(out_path):
        content = open(out_path).read()
        check("written .mtlx contains a real open_pbr_surface node", "open_pbr_surface" in content)
        check("written .mtlx references the source image filepath",
              "rust_basecolor.png" in content)
        check("written .mtlx contains the metalness constant value",
              "metalness" in content)
        print()
        print("--- actual .mtlx content ---")
        print(content)

# Unsupported case: no Principled BSDF at all
no_principled_graph = {
    "material": "M_Procedural",
    "nodes": [{"name": "Noise Texture", "type": "TEX_NOISE", "active": True}],
    "links": [], "orphaned_nodes": [], "has_orphaned_nodes": False,
}
server._send_raw = lambda cmd, **kwargs: no_principled_graph if cmd == "get_material_graph" else original_send_raw(cmd, **kwargs)
result2 = json.loads(server.export_material_as_materialx(material_name="M_Procedural"))
check("material with no Principled BSDF is honestly flagged unsupported, not guessed at",
      result2.get("supported") is False and "Principled BSDF" in result2.get("reason", ""))

server._send_raw = original_send_raw

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All checks passed.")

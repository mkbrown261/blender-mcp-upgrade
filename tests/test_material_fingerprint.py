# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp[cli]", "pillow", "MaterialX"]
# ///
"""
Verifies the richer material fingerprint added to get_asset_dna()'s PBR
socket scan — real measured signals beyond the single metal_factor scalar
used for technique dispatch: roughness (texture-sampled or constant),
subsurface_weight, specular_ior_level, and normal_map_present/bumpiness
(pixel-variance of the connected normal map). Every value is measured from
real node graph state, never guessed — same discipline as metal_factor
itself. This is the raw material for the material knowledge layer.
Run: uv run tests/test_material_fingerprint.py
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

# ── Structural checks on the generated Blender-side script ──────────────────
check("script computes a real measured average+stdev via strided sampling, not a guess",
      "def sample_avg_stdev(image, max_samples=1500):" in code
      and "statistics.pstdev(vals)" in code)
check("roughness fingerprint distinguishes texture-sampled from constant, same as metal_factor",
      'fp["roughness_source"] = "texture_sampled"' in code
      and 'fp["roughness_source"] = "constant"' in code)
check("subsurface_weight lookup is version-safe (Blender 4.x vs 3.x socket name)",
      'get_input(principled, ["Subsurface Weight", "Subsurface"])' in code)
check("specular_ior_level lookup is version-safe (Blender 4.x vs 3.x socket name)",
      'get_input(principled, ["Specular IOR Level", "Specular"])' in code)
check("normal map bumpiness is real measured pixel variance (stdev), not presence-only",
      'fp["normal_map_bumpiness"] = stdev' in code)
check("normal map fingerprint only claims presence when a real connected TEX_IMAGE was found",
      "normal_sock.links[0].from_node.type == 'NORMAL_MAP'" in code
      and "color_in.links[0].from_node.type == 'TEX_IMAGE'" in code)
check("a material with no principled BSDF still gets an (empty) fingerprint key, not a missing one",
      'entry = {"name": m.name, "has_principled": False, "texture_fed": [], "missing_maps": [],' in code
      and '"fingerprint": {}}' in code)

# ── Fingerprint data survives untouched through get_asset_dna's assembly ────
_DNA_RAW = {
    "get_mesh_quality_report": {"counts": {"verts": 100, "edges": 200, "faces": 100},
                                 "uv": {"has_uvs": True, "layer_count": 1}, "modifiers": []},
    "analyze_topology": {"topology_score": 90, "stats": {}},
    "run_unreal_readiness_check": {"checks": {}},
    "get_object_info": {"name": "X", "materials": ["M"]},
}

FAKE_FINGERPRINT = {
    "roughness_source": "texture_sampled", "roughness_avg": 0.62,
    "subsurface_weight": 0.0, "specular_ior_level": 0.5,
    "normal_map_present": True, "normal_map_bumpiness": 0.081,
}


def fake_send_raw(cmd, **kwargs):
    if cmd in _DNA_RAW:
        return _DNA_RAW[cmd]
    if cmd == "execute_code_safe":
        c = kwargs.get("code", "")
        if "has_principled" in c:
            return {"result": json.dumps([{
                "name": "M", "has_principled": True, "texture_fed": ["Base Color"],
                "missing_maps": ["Roughness", "Metallic", "Normal"],
                "fingerprint": FAKE_FINGERPRINT,
            }])}
        if "filepath_raw" in c:
            return {"result": json.dumps({"path": None})}
    raise AssertionError(f"unexpected command: {cmd}")


server._send_raw = fake_send_raw
server._SNAPSHOTS.clear()
dna = json.loads(server.get_asset_dna(object_name="X", target_engine="unreal"))
mat = dna["materials"][0]

check("get_asset_dna passes the fingerprint dict through to materials[] untouched",
      mat.get("fingerprint") == FAKE_FINGERPRINT)
check("fingerprint sits alongside missing_maps/texture_fed, doesn't replace them",
      mat.get("missing_maps") == ["Roughness", "Metallic", "Normal"]
      and mat.get("texture_fed") == ["Base Color"])

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All checks passed.")

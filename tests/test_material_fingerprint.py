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

# ── Regression: Normal's missing_maps check must walk through the Normal Map ─
# node, not check for a direct TEX_IMAGE link like the other 3 sockets. Real
# bug caught live: a proper Blender normal map is NEVER wired directly to
# Principled.Normal (it always goes TEX_IMAGE -> Normal Map node ->
# Principled.Normal), so the old "same check for all 4 sockets" logic
# reported Normal as missing on EVERY correctly-wired material, including
# ones that had a real, connected normal map — confirmed live on 3/3 pieces
# of a real character where fingerprint.normal_map_present=true contradicted
# missing_maps=["Normal"] from the same scan.
check("Normal's texture-fed check is NOT the same direct-TEX_IMAGE check used for the other 3 sockets",
      code.count("is_textured = bool(sock and sock.links and sock.links[0].from_node.type == 'TEX_IMAGE')") == 1)
check("Normal's texture-fed check walks through the Normal Map node to find the real source texture",
      "sock.links[0].from_node.type == 'NORMAL_MAP'" in code
      and 'sock.links[0].from_node.inputs.get("Color")' in code
      and 'sock.links[0].from_node.inputs["Color"].links[0].from_node.type == \'TEX_IMAGE\'' in code)

# ── Structural checks on the generated Blender-side script ──────────────────
check("script computes a real measured average+stdev via strided sampling, not a guess",
      "def sample_avg_stdev(image, max_samples=1500):" in code
      and "statistics.pstdev(vals)" in code)
check("roughness fingerprint distinguishes texture-sampled from constant, same as metal_factor",
      'fp["roughness_source"] = "texture_sampled"' in code
      and 'fp["roughness_source"] = "constant"' in code)
# Real bug caught live: a Roughness socket linked to a procedural node chain
# (e.g. everything generate_procedural_material builds) fell into the same
# branch as a truly-unlinked constant, reporting the socket's stale/
# meaningless default_value as if it were real — a genuinely measured 0.9789
# roughness read back as a fabricated 0.5 "constant" through this exact path.
check("a Roughness socket linked to anything OTHER than TEX_IMAGE is measured via a real "
      "face-scoped bake, not reported as a fabricated 'constant' using its stale default_value",
      'elif rough_sock and rough_sock.links:' in code
      and 'fp["roughness_source"] = "procedural_measured" if measured_rough is not None' in code)
check("the procedural-roughness bake reuses the shared safe_bake_measure helper, not a "
      "reimplemented unscoped bake",
      "safe_bake_measure(obj, m.node_tree, rough_slot_index, rough_output_node," in code)
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
      and '"fingerprint": {}, "heterogeneity":' in code)

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

# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp[cli]", "pillow", "MaterialX"]
# ///
"""
Verifies get_asset_dna() assembles a canonical, measured-only spec from the
existing analysis commands (get_mesh_quality_report, analyze_topology,
run_unreal_readiness_check, get_object_info) plus one new PBR-socket scan
script, that identity.category is only ever pulled from real session state
(session_update(active_playbook=...)) — never invented — and that the raw
fetch cache is invalidated correctly: by every mutating tool eagerly, by a
TTL as a safety net, and bypassable via force_refresh. Also verifies the
missing-normal-map auto-handoff exports the Base Color texture and attaches
export_path/next_step, only when Normal is actually the missing map.
Run: uv run tests/test_asset_dna.py
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


RAW = {
    "get_mesh_quality_report": {
        "counts": {"verts": 12000, "edges": 24000, "faces": 12000},
        "face_types": {"tris": 500, "quads": 11500, "ngons": 0},
        "problems": {"non_manifold_edges": 0, "boundary_edges": 0},
        "uv": {"has_uvs": True, "layer_count": 1},
        "modifiers": [{"type": "ARMATURE"}],
        "health": "clean",
    },
    "analyze_topology": {
        "topology_score": 55,
        "stats": {
            "quad_ratio_pct": 95.8, "tris_pct": 4.2, "ngons": 0,
            "non_manifold_edges": 0, "boundary_edges": 0,
        },
    },
    "run_unreal_readiness_check": {
        "checks": {
            "lightmap_uv":     {"pass": False, "severity": "warning"},
            "lod_naming":      {"pass": False, "severity": "info"},
            "collision_mesh":  {"pass": False, "severity": "info"},
        }
    },
    "get_object_info": {
        "name": "X", "materials": ["MatA"],
    },
}


def make_fake_send_raw(missing_maps=("Roughness", "Metallic", "Normal")):
    """cmd=='execute_code_safe' is used for two different scripts (the PBR
    socket scan and the texture export) — distinguish by a marker unique to
    each generated script's source text, the same way the real Blender addon
    only ever sees one script per call."""
    def fake_send_raw(cmd, **kwargs):
        if cmd in RAW:
            return RAW[cmd]
        if cmd == "execute_code_safe":
            code = kwargs.get("code", "")
            if "filepath_raw" in code:
                return {"result": json.dumps({"path": "/tmp/MatA_Base_Color.png", "size": [4096, 4096]})}
            materials = [{
                "name": "MatA", "has_principled": True,
                "texture_fed": ["Base Color"],
                "missing_maps": list(missing_maps),
            }]
            return {"result": json.dumps(materials)}
        raise AssertionError(f"unexpected command: {cmd}")
    return fake_send_raw


fake_send_raw = make_fake_send_raw()
server._send_raw = fake_send_raw
server._SNAPSHOTS.clear()
server._SESSION["active_playbook"] = "hero_char"

result = json.loads(server.get_asset_dna(object_name="X", target_engine="unreal"))

check("tool call succeeds", "error" not in result)
check("identity.category comes from session state, not invention",
      result.get("identity", {}).get("category") == "hero_char")
check("identity has real measured vertex_count", result.get("identity", {}).get("vertex_count") == 12000)
check("identity has_armature reflects modifier scan", result.get("identity", {}).get("has_armature") is True)
check("geometry.topology_score is the real measured value", result.get("geometry", {}).get("topology_score") == 55)
check("geometry.lightmap_uv_present reflects the real UE5 check", result.get("geometry", {}).get("lightmap_uv_present") is False)
check("materials list carries missing_maps computed from real node-graph scan",
      result.get("materials", [{}])[0].get("missing_maps") == ["Roughness", "Metallic", "Normal"])
check("production.collision_mesh_present reflects real check", result.get("production", {}).get("collision_mesh_present") is False)
check("rules_fired is present and non-empty given this synthetic asset (below topology min, no lightmap UV, missing maps)",
      len(result.get("rules_fired", [])) > 0)

no_dna_ids = {"category", "genre", "subcategory", "confidence"}
check("no invented category/genre/confidence fields exist anywhere in identity",
      not (no_dna_ids - {"category"}) & set(result.get("identity", {}).keys())
      or all(k != "confidence" for k in result.get("identity", {}).keys()))

missing_pbr = next((r for r in result["rules_fired"] if r["id"] == "missing_pbr_maps"), None)
check("missing_pbr_maps rule fired", missing_pbr is not None)
check("missing-normal-map handoff attaches a real export_path for the affected material",
      missing_pbr is not None
      and missing_pbr.get("handoff", {}).get("MatA", {}).get("export_path") == "/tmp/MatA_Base_Color.png")
check("handoff next_step points to the NormalMap-Online tool",
      missing_pbr is not None
      and "cpetry.github.io/NormalMap-Online" in missing_pbr.get("handoff", {}).get("MatA", {}).get("next_step", ""))

# ── Test cache reuse: second call must not re-issue ANY raw fetches, ────────
# including the handoff export (it's cached alongside the raw bundle, not
# re-run on every cache hit).
calls = {"n": 0}


def counting_send_raw(cmd, **kwargs):
    calls["n"] += 1
    return fake_send_raw(cmd, **kwargs)


server._send_raw = counting_send_raw
result_cached = json.loads(server.get_asset_dna(object_name="X", target_engine="unreal"))
check("second call for the same object reuses the _SNAPSHOTS cache instead of re-fetching",
      calls["n"] == 0)
check("cached call still carries the handoff block (not lost on cache hit)",
      any(r["id"] == "missing_pbr_maps" and r.get("handoff") for r in result_cached["rules_fired"]))

# ── force_refresh=True bypasses the cache ────────────────────────────────────
calls["n"] = 0
server.get_asset_dna(object_name="X", target_engine="unreal", force_refresh=True)
check("force_refresh=True re-issues raw fetches even with a warm cache", calls["n"] > 0)

# ── TTL expiry: an old cache entry is treated as stale ───────────────────────
server._send_raw = fake_send_raw
server._SNAPSHOTS.clear()
server.get_asset_dna(object_name="X", target_engine="unreal")
old_entry = server._SNAPSHOTS["X"]["_dna_raw"]
server._SNAPSHOTS["X"]["_dna_raw"] = (old_entry[0] - 10_000,) + old_entry[1:]  # force it stale
calls["n"] = 0
server._send_raw = counting_send_raw
server.get_asset_dna(object_name="X", target_engine="unreal")
check("a cache entry older than the TTL is treated as stale and re-fetched", calls["n"] > 0)

# ── Mutating tools invalidate the cache for their object ─────────────────────
server._send_raw = fake_send_raw
server._SNAPSHOTS.clear()
server.get_asset_dna(object_name="X", target_engine="unreal")
check("cache is warm before the mutating call", "_dna_raw" in server._SNAPSHOTS.get("X", {}))
server._invalidate_dna_cache("X")
check("_invalidate_dna_cache(object_name) drops that object's cached DNA",
      "_dna_raw" not in server._SNAPSHOTS.get("X", {}))

server._SNAPSHOTS.clear()
server.get_asset_dna(object_name="X", target_engine="unreal")
server.get_asset_dna(object_name="Y", target_engine="unreal")
server._invalidate_dna_cache()  # no object_name -> clears everything
check("_invalidate_dna_cache() with no argument clears every object's cache",
      "_dna_raw" not in server._SNAPSHOTS.get("X", {}) and "_dna_raw" not in server._SNAPSHOTS.get("Y", {}))

# ── No active playbook -> category stays None, not guessed ──────────────────
server._SESSION["active_playbook"] = None
server._SNAPSHOTS.clear()
server._send_raw = fake_send_raw
result_no_playbook = json.loads(server.get_asset_dna(object_name="X", target_engine="unreal"))
check("with no active playbook, category is None rather than guessed",
      result_no_playbook.get("identity", {}).get("category") is None)

# ── No handoff attached when Normal is NOT the missing map ──────────────────
server._SNAPSHOTS.clear()
server._send_raw = make_fake_send_raw(missing_maps=("Roughness",))
result_no_normal_gap = json.loads(server.get_asset_dna(object_name="X", target_engine="unreal"))
missing_pbr_2 = next((r for r in result_no_normal_gap["rules_fired"] if r["id"] == "missing_pbr_maps"), None)
check("missing_pbr_maps still fires when only Roughness is missing", missing_pbr_2 is not None)
check("no handoff is attached when Normal specifically isn't the gap",
      missing_pbr_2 is not None and "handoff" not in missing_pbr_2)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All checks passed.")

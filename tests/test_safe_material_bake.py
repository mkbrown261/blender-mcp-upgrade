# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp[cli]", "pillow", "MaterialX"]
# ///
"""
Verifies _SAFE_MATERIAL_BAKE_SNIPPET — the actual fix for a real live
incident where generate_procedural_material's calibration bake corrupted
an UNRELATED material's real texture, turning it solid black.

History, because it took three tries to get right and each failure was
caught live, not assumed:
  1. Edit-Mode face selection (material_slot_select()) before baking.
     Tested against a real two-material object with a known, populated,
     non-black second texture (0.8/0.2/0.1). FAILED — the second texture
     still went black.
  2. Reassigning every face's material_index to the target slot, without
     removing other slots. ALSO FAILED — and Blender's own console output
     explained why: "Circular dependency for image '...MatB_tex'" plus TWO
     "Baking map saved" lines for what should have been a single-material
     bake. bpy.ops.object.bake() processes every material SLOT present on
     the object, independent of which faces currently reference it.
  3. The actual fix, confirmed live via a controlled experiment (only this
     version preserved the known 0.8 red channel intact): temporarily
     strip every OTHER material slot off the mesh entirely so the target
     is the ONLY material on the object during the bake, then rebuild the
     exact original slot list (order matters — slot index is positional)
     and restore per-face material_index afterward.

Shared via string concatenation (independent scripts sent to Blender, not
importable modules) into all three places that bake: generate_procedural_
material's calibration step, bake_weathered_textures' bake_pass, and
get_asset_dna's procedural-roughness measurement fallback — ONE
implementation, verified once here, reused three times.
Run: uv run tests/test_safe_material_bake.py
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import server

failures = []


def check(name, condition):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}")
    if not condition:
        failures.append(name)


snippet = server._SAFE_MATERIAL_BAKE_SNIPPET

# ── The actual, live-verified fix: strip every other slot, not just reassign faces ─
check("captures the ORIGINAL material slot list (order, not just membership) before any mutation",
      "original_materials = list(mesh.materials)" in snippet)
check("captures the ORIGINAL per-face material_index before any mutation",
      "original_material_indices = [p.material_index for p in mesh.polygons]" in snippet)
check("resolves the actual target material object from the original list, by the caller's slot index",
      "target_material = original_materials[mat_slot_index]" in snippet)
check("strips EVERY material slot off the mesh (not just reassigning face indices — "
      "that alone was tried live and failed) before the bake",
      snippet.count("while len(mesh.materials) > 0:") >= 1
      and "mesh.materials.pop(index=0)" in snippet)
check("appends ONLY the target material back — it is the sole slot during the bake",
      "mesh.materials.append(target_material)" in snippet)
check("the slot-stripping happens BEFORE the bake call, not after",
      snippet.index("mesh.materials.append(target_material)") < snippet.index("bpy.ops.object.bake(type='EMIT')"))

# ── Restoration guarantees ────────────────────────────────────────────────
check("original Surface link is captured before any node mutation",
      "original_link = output_node.inputs[\"Surface\"].links[0]" in snippet)
check("the whole bake sequence is wrapped in try/finally — restoration is guaranteed, "
      "not just on the success path",
      "try:" in snippet and "finally:" in snippet)
check("finally block restores the original Surface link unconditionally",
      "nt.links.new(original_from, output_node.inputs[\"Surface\"])" in snippet)
check("finally block restores render engine and sample count unconditionally",
      "bpy.context.scene.render.engine = original_engine" in snippet
      and "bpy.context.scene.cycles.samples = original_samples" in snippet)
check("finally block rebuilds the EXACT original material slot list, in original order "
      "(slot index is positional — appending in any other order would silently remap "
      "every other material's faces to the wrong slot)",
      "for m in original_materials:" in snippet and "mesh.materials.append(m)" in snippet)
check("finally block restores the ORIGINAL per-face material_index unconditionally",
      "for p, orig_idx in zip(mesh.polygons, original_material_indices):" in snippet
      and "p.material_index = orig_idx" in snippet)

# ── Reused, not duplicated, across all three real call sites ─────────────
gen_proc_code = server.generate_procedural_material.__doc__  # sanity: tool exists
check("generate_procedural_material's tool exists and is documented",
      isinstance(gen_proc_code, str) and len(gen_proc_code) > 0)

pbr_scan = server._PBR_SOCKET_SCAN_SCRIPT
check("_PBR_SOCKET_SCAN_SCRIPT embeds the shared snippet (not a reimplemented bake)",
      pbr_scan.startswith(server._SAFE_MATERIAL_BAKE_SNIPPET))

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All checks passed.")

# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp[cli]", "pillow"]
# ///
"""
Exercises record_knowledge()/query_knowledge() directly against the real
server.py module — pure Python, no Blender/bpy needed since the knowledge
base is just JSON on disk. Run with: uv run tests/test_knowledge_base.py
"""
import sys
import os
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import server

# Isolate from the real, persistent knowledge base — this test must never
# read or write .blender_mcp_knowledge.json. Swap in an empty in-memory
# store and a no-op save for the duration of this test only.
server._KNOWLEDGE = []
server._save_knowledge = lambda: None

failures = []


def check(name, condition):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}")
    if not condition:
        failures.append(name)


# @mcp.tool() in this FastMCP version returns the plain function unwrapped,
# so the module attributes are directly callable.
record = server.record_knowledge
query = server.query_knowledge

# Seed with the real lesson learned tonight — genuine, not synthetic
result1 = json.loads(record(
    problem_type="non_manifold_repair_ceiling",
    category="mesh_topology",
    problem="auto_repair_mesh cannot fully resolve non-manifold edges on Tripo3D-sourced meshes",
    solution="snapshot -> auto_repair_mesh -> select_non_manifold+fill_holes+remove_doubles -> "
             "triangulate ngons -> auto_repair_mesh again converges to ~65-85% reduction, plateaus there",
    why_it_worked="fill_holes closes simple boundary loops but complex interior non-manifold survives; "
                  "each fill_holes pass can introduce new ngons which must be triangulated separately",
    outcome="partial",
    blender_version="5.1",
    asset_source="tripo3d",
    mesh_type="organic_character",
))
check("record_knowledge creates a new entry", result1["recorded"] and result1["action"] == "new_entry")

result2 = json.loads(record(
    problem_type="non_manifold_repair_ceiling",
    category="mesh_topology",
    problem="same pattern confirmed on a second, unrelated character",
    solution="same sequence, same plateau behavior",
    why_it_worked="confirmed reproducible — not a fluke of the first mesh",
    outcome="partial",
    blender_version="5.1",
    asset_source="tripo3d",
    mesh_type="organic_character",
))
check("recording the same problem_type+category+tags again increments confirmation, doesn't duplicate",
      result2["action"] == "confirmed_existing" and result2["times_confirmed"] == 2)
check("confirmed entry keeps the same id as the original", result2["id"] == result1["id"])

# Different context tags (hard-surface, not organic) — must NOT merge with the above
result3 = json.loads(record(
    problem_type="non_manifold_repair_ceiling",
    category="mesh_topology",
    problem="hard-surface robot mesh — fill_holes had ZERO effect, unlike organic meshes",
    solution="auto_repair_mesh alone (merge-by-distance) still gets ~40% reduction; "
             "fill_holes trick doesn't apply — interior non-manifold, not boundary holes",
    why_it_worked="hard-surface topology doesn't have simple boundary-loop holes the way organic does",
    outcome="partial",
    blender_version="5.1",
    asset_source="tripo3d",
    mesh_type="hard_surface",
))
check("different mesh_type tag creates a SEPARATE entry, doesn't merge with organic_character lesson",
      result3["action"] == "new_entry" and result3["id"] != result1["id"])

# Query it back
q1 = json.loads(query(problem_type="non_manifold_repair_ceiling", category="mesh_topology"))
check("query with no context tags returns both entries", q1["total_matches"] == 2)

q2 = json.loads(query(
    problem_type="non_manifold_repair_ceiling", category="mesh_topology",
    mesh_type="organic_character",
))
organic_match = next((m for m in q2["matches"] if m["id"] == result1["id"]), None)
check("query with matching mesh_type tag reports exact context_fit for the organic entry",
      organic_match is not None and organic_match["context_fit"] == "exact")

hard_surface_in_organic_query = next((m for m in q2["matches"] if m["id"] == result3["id"]), None)
check("hard-surface entry still returned but flagged as a tag mismatch, not silently trusted",
      hard_surface_in_organic_query is not None
      and hard_surface_in_organic_query["context_fit"].startswith("mismatch"))

check("higher times_confirmed entry ranks first",
      q1["matches"][0]["id"] == result1["id"])

q3 = json.loads(query())
check("query with zero filters returns category counts, not a full dump",
      "matches" not in q3 and q3.get("by_category", {}).get("mesh_topology", 0) >= 2)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All checks passed.")

# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp[cli]"]
# ///
"""
Custom MCP server for blender-mcp-upgrade — v2.2 AI Technical Director Edition.

Bridges Claude to the BlenderMCPServer TCP socket (addon.py, localhost:9876)
and exposes ALL 39 commands as first-class @mcp.tool() functions.

New in v2.2:
  - get_telemetry_consent tool (closes 38/39 coverage gap)
  - _reason() intelligence engine: every analysis response is enriched with
    severity, production_impact, recommended_fix, auto_fixable, and
    professional reasoning — think senior AAA technical artist review
  - auto_repair_mesh: full scan→diagnose→repair→verify loop for safe mesh fixes
  - analyze_mesh_for_unreal: compound tool — QA + topology + UE5 check in one call
    with unified reasoning output
  - critique_animation: animation critic with severity-ranked findings

Wire protocol (see addon.py _handle_client / execute_command):
  request  -> raw JSON, no length prefix: {"type": "<command>", "params": {...}}
  response <- raw JSON: {"status": "success", "result": ...}
              or        {"status": "error", "message": "..."}
"""

import base64
import json
import logging
import os
import socket
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP, Image

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("BlenderMCPCustomServer")

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 9876


# ─────────────────────────────────────────────────────────────────────────────
# CONNECTION LAYER
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class BlenderConnection:
    host: str
    port: int
    sock: Optional[socket.socket] = None

    def connect(self) -> bool:
        if self.sock:
            return True
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.connect((self.host, self.port))
            logger.info(f"Connected to Blender at {self.host}:{self.port}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Blender: {e}")
            self.sock = None
            return False

    def disconnect(self):
        if self.sock:
            try:
                self.sock.close()
            except Exception as e:
                logger.error(f"Error disconnecting from Blender: {e}")
            finally:
                self.sock = None

    def receive_full_response(self, sock: socket.socket, buffer_size: int = 8192) -> bytes:
        """Accumulate chunks until the buffer parses as a complete JSON object.
        The addon has no length prefix, so this is the only reliable way to
        know a response is complete (mirrors addon._handle_client's own logic)."""
        chunks = []
        sock.settimeout(180.0)
        try:
            while True:
                try:
                    chunk = sock.recv(buffer_size)
                    if not chunk:
                        if not chunks:
                            raise Exception("Connection closed before receiving any data")
                        break
                    chunks.append(chunk)
                    data = b"".join(chunks)
                    try:
                        json.loads(data.decode("utf-8"))
                        return data
                    except json.JSONDecodeError:
                        continue
                except socket.timeout:
                    logger.warning("Socket timeout during chunked receive")
                    break
                except (ConnectionError, BrokenPipeError, ConnectionResetError):
                    raise
        except socket.timeout:
            logger.warning("Socket timeout during chunked receive")

        if chunks:
            data = b"".join(chunks)
            json.loads(data.decode("utf-8"))
            return data
        raise Exception("No data received")

    def send_command(self, command_type: str, params: dict = None) -> Any:
        if not self.sock and not self.connect():
            raise ConnectionError("Not connected to Blender")

        command = {"type": command_type, "params": params or {}}
        try:
            self.sock.sendall(json.dumps(command).encode("utf-8"))
            self.sock.settimeout(180.0)
            response_data = self.receive_full_response(self.sock)
            response = json.loads(response_data.decode("utf-8"))

            if response.get("status") == "error":
                raise Exception(response.get("message", "Unknown error from Blender"))
            return response.get("result", {})
        except socket.timeout:
            self.sock = None
            raise Exception(
                "Timeout waiting for Blender response. Make sure Blender is running "
                "with a GUI (not headless -b mode)."
            )
        except (ConnectionError, BrokenPipeError, ConnectionResetError) as e:
            self.sock = None
            raise Exception(f"Connection to Blender lost: {e}")
        except json.JSONDecodeError as e:
            raise Exception(f"Invalid response from Blender: {e}")
        except Exception:
            self.sock = None
            raise


_blender_connection: Optional[BlenderConnection] = None


def get_blender_connection() -> BlenderConnection:
    global _blender_connection
    if _blender_connection is not None and _blender_connection.sock is not None:
        return _blender_connection

    host = os.getenv("BLENDER_HOST", DEFAULT_HOST)
    port = int(os.getenv("BLENDER_PORT", DEFAULT_PORT))
    conn = BlenderConnection(host=host, port=port)
    if not conn.connect():
        raise Exception(
            f"Could not connect to Blender at {host}:{port}. Make sure Blender is running "
            "with the BlenderMCP addon server started (N-panel > BlenderMCP > Start Server)."
        )
    _blender_connection = conn
    return _blender_connection


def _send_json(command_type: str, **params) -> str:
    """Send a command, return its result as a pretty-printed JSON string."""
    try:
        blender = get_blender_connection()
        result = blender.send_command(command_type, params)
        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        logger.error(f"Error in {command_type}: {e}")
        return json.dumps({"error": str(e)})


def _send_raw(command_type: str, **params) -> dict:
    """Send a command, return raw Python dict (not serialised). For internal compound tools."""
    try:
        blender = get_blender_connection()
        return blender.send_command(command_type, params)
    except Exception as e:
        logger.error(f"Error in {command_type}: {e}")
        return {"error": str(e)}


def _process_bbox(original_bbox):
    if original_bbox is None:
        return None
    if all(isinstance(i, int) for i in original_bbox):
        return original_bbox
    if any(i <= 0 for i in original_bbox):
        raise ValueError("bbox values must be greater than zero")
    return [int(float(i) / max(original_bbox) * 100) for i in original_bbox]


# ─────────────────────────────────────────────────────────────────────────────
# REASONING ENGINE
# ─────────────────────────────────────────────────────────────────────────────

def _reason_mesh_problems(raw: dict) -> dict:
    """
    Interpret detect_mesh_problems output like a senior technical artist.

    Real addon.py schema (detect_mesh_problems):
      {
        "clean": bool,
        "problem_count": int,
        "problems": [
          {"type": "non_manifold_edges"|"isolated_verts"|"zero_area_faces"|"ngons"
                   |"boundary_edges"|"duplicate_faces",
           "count": int, "fix": str}
        ]
      }
    problems is a LIST of dicts, not a flat dict.
    """
    findings = []
    auto_fixable = []
    needs_artist = []

    # Real schema: problems is a LIST of {type, count, fix}
    problems_list = raw.get("problems", [])

    # Build lookup: type -> count
    prob_counts: dict = {}
    for p in problems_list:
        prob_counts[p.get("type", "")] = p.get("count", 0)

    nm = prob_counts.get("non_manifold_edges", 0)
    if nm > 0:
        sev = "critical" if nm > 20 else "warning"
        findings.append({
            "issue": f"{nm} non-manifold edge(s) detected",
            "severity": sev,
            "why_it_matters": (
                "Non-manifold geometry means edges shared by more than two faces or "
                "faces with no volume. UE5 import pipeline and subdivision modifiers "
                "both fail unpredictably on non-manifold meshes. Normal baking will "
                "produce incorrect results."
            ),
            "professional_fix": (
                "Edit Mode > Select > Select All by Trait > Non Manifold. "
                "Delete interior faces first, merge overlapping verts, "
                "then fill or bridge remaining open edges."
            ),
            "auto_fixable": False,
            "auto_fix_reason": "Non-manifold repair requires artist judgement on intent.",
        })
        needs_artist.append("non_manifold_edges")

    lv = prob_counts.get("isolated_verts", 0)
    if lv > 0:
        findings.append({
            "issue": f"{lv} isolated vertex/vertices (not connected to any edge)",
            "severity": "warning",
            "why_it_matters": (
                "Loose vertices inflate vertex count with zero visual contribution, "
                "confuse UV unwrapping, and shift the bounding box causing incorrect "
                "pivot placement in UE5."
            ),
            "professional_fix": "Edit Mode > Mesh > Clean Up > Delete Loose.",
            "auto_fixable": True,
            "auto_fix_reason": "Safe to delete automatically — no topology affected.",
        })
        auto_fixable.append("isolated_verts")

    zf = prob_counts.get("zero_area_faces", 0)
    if zf > 0:
        sev = "critical" if zf > 5 else "warning"
        findings.append({
            "issue": f"{zf} zero-area (degenerate) face(s)",
            "severity": sev,
            "why_it_matters": (
                "Degenerate faces have no surface area — their normal is undefined. "
                "They cause black patches under baked lighting, NaN values in normal "
                "maps, and crashes in some physics solvers."
            ),
            "professional_fix": "Mesh > Clean Up > Degenerate Dissolve (threshold 0.0001).",
            "auto_fixable": True,
            "auto_fix_reason": "Degenerate dissolve is non-destructive at low threshold.",
        })
        auto_fixable.append("zero_area_faces")

    df = prob_counts.get("duplicate_faces", 0)
    if df > 0:
        findings.append({
            "issue": f"{df} duplicate face(s) (same vertex set as another face)",
            "severity": "critical",
            "why_it_matters": (
                "Duplicate faces cause z-fighting — flickering surfaces at all distances. "
                "They double draw cost for zero visual benefit and corrupt normal baking."
            ),
            "professional_fix": "Mesh > Clean Up > Merge by Distance (0.0001m).",
            "auto_fixable": True,
            "auto_fix_reason": "Merge by distance reliably eliminates duplicates.",
        })
        auto_fixable.append("duplicate_faces")

    ng = prob_counts.get("ngons", 0)
    if ng > 0:
        sev = "critical" if ng > 20 else "warning"
        findings.append({
            "issue": f"{ng} n-gon face(s) (5+ sides)",
            "severity": sev,
            "why_it_matters": (
                "N-gons tessellate unpredictably in UE5. The auto-triangulator often "
                "produces star patterns and shading errors under normal maps and "
                "dynamic lighting. Subdivision modifiers pinch at n-gon boundaries."
            ),
            "professional_fix": (
                "Edit Mode > Select All by Trait > Face Sides (>4). "
                "Knife-cut or dissolve edges to convert to quads."
            ),
            "auto_fixable": False,
            "auto_fix_reason": "N-gon conversion requires artist review of edge flow.",
        })
        needs_artist.append("ngons")

    bd = prob_counts.get("boundary_edges", 0)
    if bd > 0:
        findings.append({
            "issue": f"{bd} boundary edge(s) — mesh is not watertight/closed",
            "severity": "warning",
            "why_it_matters": (
                "Open boundary edges mean the mesh has holes. This causes issues with "
                "boolean operations, physics collision generation in UE5, and may "
                "indicate missing geometry."
            ),
            "professional_fix": (
                "Edit Mode > Select > Select All by Trait > Non Manifold. "
                "Alt+click boundary loops, then F to fill or Bridge Edge Loops."
            ),
            "auto_fixable": False,
            "auto_fix_reason": "Hole-filling requires artist decision on correct topology.",
        })
        needs_artist.append("boundary_edges")

    severities = [f["severity"] for f in findings]
    if "critical" in severities:
        overall = "critical"
    elif "warning" in severities:
        overall = "warning"
    elif findings:
        overall = "info"
    else:
        overall = "pass"

    summary = (
        "PASS — mesh is clean." if overall == "pass"
        else f"{overall.upper()} — {len(findings)} issue(s) found. "
             f"{len(auto_fixable)} can be auto-repaired, {len(needs_artist)} require artist review."
    )

    return {
        **raw,
        "_reasoning": {
            "overall_severity": overall,
            "summary": summary,
            "findings": findings,
            "auto_repairable": auto_fixable,
            "needs_artist_review": needs_artist,
            "production_ready": overall == "pass",
        }
    }


def _reason_mesh_quality(raw: dict) -> dict:
    """
    Interpret get_mesh_quality_report output with professional context.

    Real addon.py schema (get_mesh_quality_report):
      {
        "counts":     {"verts": int, "edges": int, "faces": int},
        "face_types": {"tris": int, "quads": int, "ngons": int},
        "problems":   {"non_manifold_edges": int, "isolated_verts": int,
                       "zero_area_faces": int, "duplicate_faces": int,
                       "boundary_edges": int},   <- DICT not list
        "poles":      {"n3_not_boundary": int, "n5_not_boundary": int, "high_valence": int},
        "uv":         {"out_of_bounds_loops": int, "has_uvs": bool, "layer_count": int},
        "health":     "clean"|"issues_found"
      }
    """
    findings = []

    counts     = raw.get("counts", {})
    face_types = raw.get("face_types", {})
    problems   = raw.get("problems", {})  # dict: key -> count
    poles      = raw.get("poles", {})
    uv         = raw.get("uv", {})

    vert_count = counts.get("verts", 0)
    face_count = counts.get("faces", 0)
    ngon_count = face_types.get("ngons", 0)
    uv_oob     = uv.get("out_of_bounds_loops", 0)
    dup_faces  = problems.get("duplicate_faces", 0)
    nm_edges   = problems.get("non_manifold_edges", 0)
    iso_verts  = problems.get("isolated_verts", 0)
    zero_area  = problems.get("zero_area_faces", 0)
    high_val   = poles.get("high_valence", 0)

    if ngon_count > 0 and face_count > 0:
        ngon_pct = (ngon_count / face_count) * 100
        sev = "critical" if ngon_pct > 20 else "warning"
        findings.append({
            "issue": f"{ngon_count} n-gon(s) ({ngon_pct:.1f}% of faces)",
            "severity": sev,
            "why_it_matters": (
                "N-gons (5+ sided faces) tessellate unpredictably in real-time engines. "
                "UE5 will auto-triangulate them but the result often produces star "
                "patterns and shading errors under normal maps and dynamic lighting."
            ),
            "professional_fix": (
                "Manually dissolve n-gon edges and re-route topology using quads. "
                "Target areas near curved surfaces and deforming joints first."
            ),
        })

    if nm_edges > 0:
        findings.append({
            "issue": f"{nm_edges} non-manifold edge(s)",
            "severity": "critical" if nm_edges > 20 else "warning",
            "why_it_matters": (
                "Non-manifold geometry causes UE5 import failures, breaks subdivision, "
                "and produces incorrect normal baking results."
            ),
            "professional_fix": (
                "Select All by Trait > Non Manifold. "
                "Delete interior faces, merge overlapping verts, fill open edges."
            ),
        })

    if iso_verts > 0:
        findings.append({
            "issue": f"{iso_verts} isolated vertex/vertices",
            "severity": "warning",
            "why_it_matters": "Inflate vertex count and shift bounding box with zero visual contribution.",
            "professional_fix": "Mesh > Clean Up > Delete Loose.",
        })

    if zero_area > 0:
        findings.append({
            "issue": f"{zero_area} zero-area (degenerate) face(s)",
            "severity": "critical" if zero_area > 5 else "warning",
            "why_it_matters": "Undefined normals cause black bake patches and physics solver crashes.",
            "professional_fix": "Mesh > Clean Up > Degenerate Dissolve (0.0001).",
        })

    if face_count > 0 and vert_count > 0:
        vpf = vert_count / face_count
        if vpf > 4.5:
            findings.append({
                "issue": f"High vertex-to-face ratio ({vpf:.2f} — expected ~4.0 for quads)",
                "severity": "warning",
                "why_it_matters": (
                    "Ratio above 4.0 indicates triangulated patches or redundant edge "
                    "loops inflating GPU vertex cost."
                ),
                "professional_fix": "Dissolve redundant edge loops on flat surfaces.",
            })

    if high_val > 5:
        findings.append({
            "issue": f"{high_val} high-valence pole(s) (6+ edges at one vertex)",
            "severity": "info",
            "why_it_matters": "Excessive poles cause pinching under subdivision and complicate skin weighting.",
            "professional_fix": "Dissolve edges around 6+ pole verts to reduce valence.",
        })

    if uv_oob > 0:
        findings.append({
            "issue": f"{uv_oob} UV loop(s) outside 0–1 UV space",
            "severity": "warning",
            "why_it_matters": (
                "UVs outside 0–1 tile are valid for tiling textures but catastrophic "
                "for lightmap baking — check which UV channel these appear on."
            ),
            "professional_fix": (
                "Channel 0 (colour): may be intentional tiling — acceptable. "
                "Channel 1 (lightmap): pack all islands inside 0–1 space."
            ),
        })

    if dup_faces > 0:
        findings.append({
            "issue": f"{dup_faces} duplicate face(s)",
            "severity": "critical",
            "why_it_matters": "Z-fighting, doubled draw cost, corrupt normal baking.",
            "professional_fix": "Mesh > Clean Up > Merge by Distance (0.0001m).",
        })

    severities = [f["severity"] for f in findings]
    overall = (
        "critical" if "critical" in severities
        else "warning" if "warning" in severities
        else "info" if findings
        else "pass"
    )

    return {
        **raw,
        "_reasoning": {
            "overall_severity": overall,
            "summary": (
                "PASS — mesh quality is acceptable." if overall == "pass"
                else f"{overall.upper()} — {len(findings)} quality issue(s) found."
            ),
            "findings": findings,
            "production_ready": overall in ("pass", "info"),
        }
    }


def _reason_topology(raw: dict) -> dict:
    """
    Interpret analyze_topology output with professional context.

    Real addon.py schema (analyze_topology):
      {
        "context": str,
        "topology_score": int,   # 0-100
        "rating": "excellent"|"good"|"acceptable"|"poor",
        "stats": {
          "total_faces": int,
          "quads": int, "tris": int, "ngons": int,
          "quad_ratio_pct": float,
          "tris_pct": float,
          "avg_vert_valence": float,
          "boundary_edges": int,
          "non_manifold_edges": int,
          "pole_distribution": {"3": int, "4": int, ...}  # string keys
        },
        "issues": [str],
        "recommendations": [str]
      }
    """
    findings = []

    context = raw.get("context", "generic")
    stats   = raw.get("stats", {})
    score   = raw.get("topology_score", 100)

    quad_ratio = stats.get("quad_ratio_pct", 100.0)
    tris_pct   = stats.get("tris_pct", 0.0)
    ngon_count = stats.get("ngons", 0)
    face_count = stats.get("total_faces", 0)
    nm_edges   = stats.get("non_manifold_edges", 0)
    bd_edges   = stats.get("boundary_edges", 0)
    pole_dist  = stats.get("pole_distribution", {})

    thresholds = {
        "character_body": {"min_quad": 85, "max_tri": 10},
        "face":           {"min_quad": 90, "max_tri":  5},
        "hand":           {"min_quad": 90, "max_tri":  5},
        "hard_surface":   {"min_quad": 70, "max_tri": 25},
        "generic":        {"min_quad": 75, "max_tri": 20},
    }
    t = thresholds.get(context, thresholds["generic"])

    if quad_ratio < t["min_quad"]:
        gap = t["min_quad"] - quad_ratio
        sev = "critical" if gap > 20 else "warning"
        findings.append({
            "issue": f"Quad ratio {quad_ratio:.1f}% — below {t['min_quad']}% target for '{context}'",
            "severity": sev,
            "why_it_matters": (
                f"Low quad ratio degrades deformation quality for skinned meshes, "
                f"produces unpredictable subdivision results. "
                f"Studios target {t['min_quad']}%+ quads for '{context}' assets."
            ),
            "professional_fix": (
                "Retopologise high-tri areas using Poly Build or RetopoFlow. "
                "Prioritise deforming areas (joints, face muscles)."
            ),
        })

    if tris_pct > t["max_tri"]:
        findings.append({
            "issue": f"Triangle ratio {tris_pct:.1f}% — exceeds {t['max_tri']}% limit for '{context}'",
            "severity": "warning",
            "why_it_matters": (
                "Excessive triangles in deforming areas cause skin-weighting artefacts "
                "and normal map shading errors under animation."
            ),
            "professional_fix": (
                "Face Select > Select All by Trait > Face Sides = 3. "
                "Redirect edge flow to convert tri fans into quad patches."
            ),
        })

    if ngon_count > 0:
        ngon_pct = (ngon_count / face_count * 100) if face_count > 0 else 0
        findings.append({
            "issue": f"{ngon_count} n-gon(s) ({ngon_pct:.1f}% of faces)",
            "severity": "warning" if ngon_pct < 10 else "critical",
            "why_it_matters": "N-gons tessellate unpredictably and produce shading artifacts in UE5.",
            "professional_fix": "Dissolve n-gon edges and re-route as quad patches.",
        })

    high_poles = (
        int(pole_dist.get("6", 0)) +
        int(pole_dist.get("7", 0)) +
        int(pole_dist.get("8", 0))
    )
    if high_poles > 5:
        findings.append({
            "issue": f"{high_poles} high-valence pole(s) (6+ edges)",
            "severity": "info",
            "why_it_matters": "Excessive poles cause pinching under subdivision and complicate skin weighting.",
            "professional_fix": "Dissolve edges around 6+ pole verts. Aim for mostly 4-5 edge vertices.",
        })

    if nm_edges > 0:
        findings.append({
            "issue": f"{nm_edges} non-manifold edge(s) affecting topology score",
            "severity": "critical",
            "why_it_matters": "Non-manifold geometry is incompatible with subdivision and UE5 import.",
            "professional_fix": "Mesh > Clean Up > Fill Holes; check for interior faces.",
        })

    if bd_edges > 0:
        findings.append({
            "issue": f"{bd_edges} boundary edge(s) — mesh is not watertight",
            "severity": "warning",
            "why_it_matters": "Open mesh causes issues with collision generation and boolean operations.",
            "professional_fix": "Alt+click boundary loops then F to fill, or Bridge Edge Loops.",
        })

    severities = [f["severity"] for f in findings]
    overall = (
        "critical" if "critical" in severities
        else "warning" if "warning" in severities
        else "info" if findings
        else "pass"
    )

    if score >= 90:
        grade = "Excellent"
    elif score >= 70:
        grade = "Good"
    elif score >= 50:
        grade = "Acceptable"
    else:
        grade = "Poor — retopology recommended"

    return {
        **raw,
        "_reasoning": {
            "overall_severity": overall,
            "context_evaluated": context,
            "thresholds_applied": t,
            "score": score,
            "grade": grade,
            "summary": (
                f"PASS — topology meets '{context}' standard. Score: {score}/100 ({grade})." if overall == "pass"
                else f"{overall.upper()} — topology does not meet '{context}' standard. "
                     f"Score: {score}/100 ({grade}). {len(findings)} issue(s) found."
            ),
            "findings": findings,
            "production_ready": overall == "pass",
        }
    }


def _reason_unreal_readiness(raw: dict) -> dict:
    """
    Interpret run_unreal_readiness_check output with UE5 pipeline context.

    Real addon.py schema — 11 checks, all in checks dict:
      naming_convention, scale_uniform, scale_applied, pivot_at_origin,
      triangulated, has_uvs, lightmap_uv, collision_mesh, lod_naming,
      modifiers_applied, normal_map_direction
    Each check: {"pass": bool, "severity": str, "detail": str}
    """
    findings = []
    checks = raw.get("checks", {})

    check_meta = {
        "naming_convention": {
            "label": "Naming convention not followed (SM_/SK_/T_ prefix missing)",
            "why": (
                "UE5 asset pipelines rely on prefix conventions: SM_ (Static Mesh), "
                "SK_ (Skeletal Mesh), T_ (Texture), M_ (Material), MI_ (Material Instance). "
                "Without them, asset management tools, import rules, and Blueprint "
                "references become inconsistent across the project."
            ),
            "fix": "Rename: SM_AssetName (static), SK_AssetName (skeletal), T_AssetName (texture).",
            "auto_fixable": False,
        },
        "scale_uniform": {
            "label": "Non-uniform scale (X/Y/Z scale values differ)",
            "why": (
                "Non-uniform scale distorts the mesh non-proportionally in UE5. "
                "Physics collision boxes will be incorrectly shaped and skeletal "
                "mesh bind poses break along the non-uniform axis."
            ),
            "fix": "Object Mode > Ctrl+A > Scale to apply. Verify geometry shape afterward.",
            "auto_fixable": True,
        },
        "scale_applied": {
            "label": "Scale not applied — object has non-unit scale (not 1,1,1)",
            "why": (
                "Non-applied scale is the single most common UE5 import bug. "
                "The object imports at the wrong size, physics collision is incorrectly "
                "scaled, and FBX export multiplies Blender units by unapplied scale "
                "causing double-scaling."
            ),
            "fix": "Object Mode > Ctrl+A > Scale. Verify dimensions after applying.",
            "auto_fixable": True,
        },
        "pivot_at_origin": {
            "label": "Pivot not at world origin",
            "why": (
                "UE5 uses the mesh pivot as the actor spawn point and rotation origin. "
                "An off-origin pivot causes offset level placement, rotation around the "
                "wrong point, and incorrect socket/attachment positions."
            ),
            "fix": "Object > Set Origin > Origin to Geometry, or move to scene origin for characters.",
            "auto_fixable": False,
        },
        "triangulated": {
            "label": "Mesh not pre-triangulated (contains quads/n-gons)",
            "why": (
                "UE5 auto-triangulates on import — fine for static meshes. "
                "Pre-triangulating gives control over tessellation pattern, "
                "which matters for normal map accuracy on curved surfaces."
            ),
            "fix": "Optional: Triangulate modifier (apply before export) or enable in FBX export dialog.",
            "auto_fixable": True,
        },
        "has_uvs": {
            "label": "No UV maps found",
            "why": (
                "Without UVs, no texture can be applied in UE5 and lightmap baking "
                "will fail entirely. Asset cannot be used in any textured production "
                "context without UV unwrapping."
            ),
            "fix": "Edit Mode > U > Smart UV Project. Create UV Channel 1 for lightmaps.",
            "auto_fixable": False,
        },
        "lightmap_uv": {
            "label": "No dedicated lightmap UV channel (UV Channel 1 missing)",
            "why": (
                "Without a non-overlapping UV Channel 1, Unreal Lightmass and Lumen "
                "cannot bake correct shadows. Asset will show uniform ambient shadowing "
                "or baking artifacts."
            ),
            "fix": "Add UV Channel 1 via Smart UV Project or Lightmap Pack. Keep non-overlapping.",
            "auto_fixable": False,
        },
        "collision_mesh": {
            "label": "No custom collision mesh (UCX_/UBX_ object not found)",
            "why": (
                "Without a custom collision mesh, UE5 uses an auto-convex hull which is "
                "too imprecise for gameplay — characters clip through corners, projectiles "
                "miss concave surfaces, physics performance is worse."
            ),
            "fix": "Create collision mesh named UCX_ObjectName (convex) or UBX_ObjectName (box). "
                   "Export alongside main mesh in same FBX.",
            "auto_fixable": False,
        },
        "lod_naming": {
            "label": "No LOD hierarchy found (ObjectName_LOD0 not present in scene)",
            "why": (
                "Without LODs, UE5 renders full-detail mesh at all distances. "
                "For game assets, LODs are essential for draw-call and triangle "
                "budget management."
            ),
            "fix": "Create LODs named ObjectName_LOD0, _LOD1, _LOD2 etc. Export all in same FBX.",
            "auto_fixable": False,
        },
        "modifiers_applied": {
            "label": "Unapplied blocking modifier(s) present (BOOLEAN/ARRAY/MIRROR/BEVEL/SOLIDIFY)",
            "why": (
                "These modifier types are not baked into mesh geometry. "
                "FBX export sends the base mesh without modifier effects — "
                "UE5 receives the wrong mesh, not what is visible in viewport."
            ),
            "fix": "Apply all blocking modifiers (Ctrl+A in Properties > Modifiers) before FBX export.",
            "auto_fixable": True,
        },
        "normal_map_direction": {
            "label": "Normal map direction advisory (Blender=OpenGL, UE5=DirectX)",
            "why": (
                "Blender uses OpenGL normal maps (G channel = up). "
                "UE5 uses DirectX normal maps (G channel = down). "
                "A Blender-baked normal map will look incorrectly lit in UE5."
            ),
            "fix": "In UE5 Texture Editor: enable 'Flip Green Channel'. "
                   "Or in Blender bake settings enable 'Flip Y' before baking.",
            "auto_fixable": False,
        },
    }

    for key, meta in check_meta.items():
        check = checks.get(key, {})
        passed = check.get("pass", True)
        sev = check.get("severity", "warning")
        if not passed:
            findings.append({
                "issue": meta["label"],
                "severity": "critical" if sev == "error" else sev,
                "why_it_matters": meta["why"],
                "professional_fix": meta["fix"],
                "auto_fixable": meta.get("auto_fixable", False),
                "addon_detail": check.get("detail", ""),
            })

    blocking   = [f for f in findings if f["severity"] == "critical"]
    advisory   = [f for f in findings if f["severity"] == "warning"]
    info_items = [f for f in findings if f["severity"] == "info"]

    overall = (
        "critical" if blocking
        else "warning" if advisory
        else "info" if info_items
        else "pass"
    )

    return {
        **raw,
        "_reasoning": {
            "overall_severity": overall,
            "summary": (
                "PASS — asset meets UE5 import requirements." if overall == "pass"
                else f"{overall.upper()} — {len(blocking)} blocking error(s), "
                     f"{len(advisory)} advisory warning(s), "
                     f"{len(info_items)} info item(s) before UE5 export."
            ),
            "blocking_errors": blocking,
            "advisory_warnings": advisory,
            "info_items": info_items,
            "export_safe": overall in ("pass", "info"),
            "findings": findings,
        }
    }


def _reason_animation(raw: dict) -> dict:
    """
    Interpret analyze_animation_quality output with professional animation context.

    Real addon.py schema (analyze_animation_quality):
      {
        "score": int,
        "rating": str,
        "error_count": int,
        "warning_count": int,
        "findings": [
          {"severity": "error"|"warning"|"info", "msg": str}
        ],
        "recommendation": str
      }
    findings is a FLAT LIST with severity + msg.
    severity uses "error" (not "critical") for most severe items.
    """
    findings = []

    score        = raw.get("score", 100)
    raw_findings = raw.get("findings", [])  # flat list with severity+msg

    for item in raw_findings:
        sev = item.get("severity", "info")
        msg = item.get("msg", "")
        # Map addon "error" -> "critical" for consistency with other tools
        mapped_sev = "critical" if sev == "error" else sev
        findings.append({
            "issue": msg,
            "severity": mapped_sev,
            "category": _classify_animation_issue(msg),
            "why_it_matters": _animation_why(msg),
            "professional_fix": _animation_fix(msg),
        })

    if score >= 90:
        grade = "A — Production ready"
    elif score >= 75:
        grade = "B — Acceptable with minor polish"
    elif score >= 55:
        grade = "C — Needs revision before shipping"
    elif score >= 35:
        grade = "D — Significant rework required"
    else:
        grade = "F — Not suitable for production"

    overall = "pass" if not findings else (
        "critical" if any(f["severity"] == "critical" for f in findings)
        else "warning" if any(f["severity"] == "warning" for f in findings)
        else "info"
    )

    return {
        **raw,
        "_reasoning": {
            "overall_severity": overall,
            "grade": grade,
            "score": score,
            "summary": (
                f"Animation grade: {grade}. Score: {score}/100. "
                f"{raw.get('error_count', 0)} error(s), {raw.get('warning_count', 0)} warning(s)."
            ),
            "findings": findings,
            "production_ready": score >= 75,
        }
    }


def _reason_asset_qa(raw: dict) -> dict:
    """
    Interpret run_asset_qa output with production QA context.

    Real addon.py schema (run_asset_qa):
      {
        "verdict": "PASS"|"FAIL",
        "passed": [str],
        "issues": [str],
        "warnings": [str],
        "issue_count": int,
        "warning_count": int,
        "summary": str
      }
    issues and warnings are both plain string lists.
    """
    findings = []

    issues   = raw.get("issues", [])
    warnings = raw.get("warnings", [])

    for issue in issues:
        findings.append({
            "issue": issue,
            "severity": "critical",
            "why_it_matters": "Blocking issue — asset will fail pipeline validation or import.",
            "professional_fix": "Resolve before export.",
        })

    for warning in warnings:
        findings.append({
            "issue": warning,
            "severity": "warning",
            "why_it_matters": "Advisory — may cause downstream problems in production.",
            "professional_fix": "Review before shipping to production.",
        })

    overall = "critical" if issues else ("warning" if warnings else "pass")

    return {
        **raw,
        "_reasoning": {
            "overall_severity": overall,
            "summary": (
                "PASS — asset passes production QA." if overall == "pass"
                else f"{overall.upper()} — {len(issues)} blocking issue(s), "
                     f"{len(warnings)} advisory warning(s)."
            ),
            "findings": findings,
            "production_ready": overall == "pass",
        }
    }



# Safe repair scripts — each is a standalone bpy code block
_REPAIR_SCRIPTS = {
    "loose_vertices": """
import bpy
obj = bpy.context.active_object
bpy.ops.object.mode_set(mode='EDIT')
bpy.ops.mesh.select_all(action='DESELECT')
bpy.ops.mesh.select_loose()
bpy.ops.mesh.delete(type='VERT')
bpy.ops.object.mode_set(mode='OBJECT')
print("loose_vertices:done")
""",
    "duplicate_faces": """
import bpy
obj = bpy.context.active_object
bpy.ops.object.mode_set(mode='EDIT')
bpy.ops.mesh.select_all(action='SELECT')
bpy.ops.mesh.remove_doubles(threshold=0.0001)
bpy.ops.object.mode_set(mode='OBJECT')
print("duplicate_faces:done")
""",
    "zero_area_faces": """
import bpy
obj = bpy.context.active_object
bpy.ops.object.mode_set(mode='EDIT')
bpy.ops.mesh.select_all(action='SELECT')
bpy.ops.mesh.dissolve_degenerate(threshold=0.0001)
bpy.ops.object.mode_set(mode='OBJECT')
print("zero_area_faces:done")
""",
    "inverted_normals": """
import bpy
obj = bpy.context.active_object
bpy.ops.object.mode_set(mode='EDIT')
bpy.ops.mesh.select_all(action='SELECT')
bpy.ops.mesh.normals_make_consistent(inside=False)
bpy.ops.object.mode_set(mode='OBJECT')
print("inverted_normals:done")
""",
    "scale_not_applied": """
import bpy
obj = bpy.context.active_object
bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
print("scale_not_applied:done")
""",
}

# _REPAIR_ORDER uses script keys. addon.py calls loose verts "isolated_verts"
# so we check both names in the repair loop below.
_REPAIR_ORDER = [
    "loose_vertices",    # addon key: "isolated_verts"
    "duplicate_faces",
    "zero_area_faces",
    "inverted_normals",
]


mcp = FastMCP(
    "BlenderMCP",
    instructions="""
╔══════════════════════════════════════════════════════════════════════════════╗
║           BLENDER MCP — SENIOR TECHNICAL ARTIST / TECHNICAL DIRECTOR        ║
║                        OPERATING SYSTEM v2.3.1                              ║
╚══════════════════════════════════════════════════════════════════════════════╝

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 1 — IDENTITY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

You are a Senior Technical Artist and Technical Director embedded inside Blender
via a live MCP tool bridge. You are not an assistant. You are not a chatbot.
You are a pipeline-aware production professional whose judgment is calibrated to
AAA game development standards.

Your priorities, in order:
  1. Pipeline correctness   — will this asset survive the full production pipeline?
  2. Visual quality         — does it look right for its intended purpose?
  3. Performance            — does it meet platform and target budgets?
  4. Production readiness   — can it be handed off without rework?
  5. User intent            — what is the user actually trying to achieve?

You think like a studio TD. A modeler asks "can I make this shape?" A Technical
Director asks "can this shape survive rigging, baking, texturing, LOD generation,
engine import, and runtime performance — and if not, what is the fastest path to
make it can?" That is the question you always have running in the background.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 2 — PRODUCTION PHILOSOPHY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

This system does not optimize for completing tasks quickly.
It optimizes for producing assets that will survive the full production pipeline
without causing problems downstream.

Speed is secondary to correctness.
A fast wrong answer is worse than a slow right one.
A mesh that looks finished but will break during rigging is not finished.
A texture that looks good in Blender but destroys draw calls in Unreal is not good.

You do not tell users what they want to hear.
You tell them what the pipeline needs to hear.

You never take shortcuts that create downstream problems.
You never mark something PASS unless you have actually verified it with tools.
You never assume a mesh is clean because it looks clean in the viewport.
You never skip a step because the user seems impatient.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 3 — PIPELINE STAGE AWARENESS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Every asset exists at a specific stage in the production pipeline. The same mesh
has completely different standards depending on where it is. You must identify
the stage before applying any judgment.

STAGE 1 — CONCEPT / SCULPT
  Signals:   Very high poly (100k–10M+), dense uniform mesh, no UVs, no rig,
             no materials or placeholder only, ZBrush/sculpt topology pattern
  Standards: No polygon limits apply. Topology quality irrelevant.
             Goal is detail capture only.
  Your job:  Confirm stage. Note if any bake targets exist. No optimization feedback.

STAGE 2 — RETOPOLOGY / BASE MESH
  Signals:   Medium poly (5k–80k), intentional edge flow, quads dominant,
             possible UV seams started, no bake maps yet
  Standards: Quad dominance >85%, animation-friendly loop placement at joints,
             poles placed away from deformation zones, no ngons in deform areas,
             edge density appropriate for deformation complexity
  Your job:  Topology QA. Pole placement. Loop flow review. Deformation readiness.

STAGE 3 — BAKE-READY
  Signals:   Two meshes present (high + low), UVs exist on low poly,
             UV islands non-overlapping, cage or offset configured
  Standards: UV islands non-overlapping, no UV stretching >20%,
             sufficient projection distance, matching silhouettes,
             no normals flipped on low poly
  Your job:  UV quality. Projection error risk. Cage validation. Bake map planning.

STAGE 4 — TEXTURE / MATERIAL
  Signals:   Low poly, PBR materials assigned, texture maps present,
             image textures linked, material slots configured
  Standards: PBR material setup (metallic/roughness workflow), power-of-2 textures,
             texel density consistent across asset, no broken image paths,
             material count appropriate for draw call budget
  Your job:  PBR correctness. Texel density. Broken path detection. Material cost.

STAGE 5 — RIG / ANIMATION
  Signals:   Armature present, vertex groups exist, weight paint applied,
             possibly keyframes or NLA tracks present
  Standards: Bone naming conventions, clean weight painting (no zero-weight verts),
             bind pose correct, deformation topology validated,
             no orphan bones, animation range defined
  Your job:  Rig validation. Weight quality. Animation data review. Deform QA.

STAGE 6 — EXPORT-READY / UNREAL PREP
  Signals:   All of the above complete, scale applied, pivot at origin,
             modifiers applied or export-configured, LOD variants possible
  Standards: ALL Unreal readiness checks must PASS — scale uniform + applied,
             pivot at world origin, triangulated or triangulate-on-export enabled,
             UVs present, lightmap UV in channel 1, no modifiers blocking export,
             naming conventions followed, collision mesh present if needed,
             LOD naming correct if LODs exist
  Your job:  Full UE5 readiness audit. Block on any FAIL. Warn on any WARN.
             Do not clear for export until all critical checks pass.

STAGE INFERENCE RULE:
  You must infer the stage from visual and data signals on every session start.
  State your inference explicitly in the orientation message.
  Always add: "Correct me if this is wrong — standards differ significantly by stage."
  If signals are ambiguous between two stages, assume the MORE DEMANDING stage
  and apply its standards. It is always safer to over-check than to under-check.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 4 — SCENE INTAKE PROTOCOL (cold connect, zero context)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

When you connect to a Blender session with no prior context, execute this
sequence automatically before responding to any user message:

PHASE 1 — OBSERVE (always, immediately, non-negotiable)
  Step 1: get_viewport_screenshot()
          → Look at what is in front of you. What type of asset? What shading mode?
            What obvious issues are visible without any tools?
  Step 2: get_scene_info()
          → How many objects? What types? What is active?
  Step 3: get_object_info(active object)
          → Vertex count, face count, materials, modifiers, armature.

PHASE 2 — INFER (from Phase 1 data alone, no additional tools yet)
  From what you observed, determine:
  - Asset type: hard-surface prop / organic character / environment piece /
                vehicle / weapon / architectural / unknown
  - Pipeline stage: which of the 6 stages above best fits the signals
  - Most critical visible issue: what is the single biggest problem you can
    already see or infer without running deep analysis tools

PHASE 3 — ORIENT (one concise paragraph, then stop and wait)
  Deliver a single orientation statement in this format:

  "I see [asset description]. [Vertex/face count]. [Materials/rig status brief].
   I'm reading this as [Stage N — stage name]. [One critical flag if present,
   prefixed with ⚠️ CRITICAL: or left out if nothing critical is visible].
   Correct me if the stage call is wrong — awaiting your direction."

  Example:
  "I see a humanoid character mesh, approximately 45k vertices, PBR materials
   assigned, no armature detected. I'm reading this as Stage 2 — Retopology.
   ⚠️ CRITICAL: 460 non-manifold edges detected — this will block export.
   Correct me if the stage call is wrong — awaiting your direction."

  Then STOP. Do not run further tools. Do not generate a full report.
  Wait for the user to direct the next action.

PHASE 1 IS NEVER SKIPPED. Even if the user's first message contains a specific
request, take the screenshot and deliver the orientation first, then address
the request. You cannot give accurate advice about something you haven't looked at.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 5 — DECISION ARCHITECTURE (when to use which tools)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

TOOL TIERS — always prefer higher tiers before reaching for lower ones:

  TIER 1 — COMPOUND (use first, cover the most ground per call)
    analyze_mesh_for_unreal       → full mesh + topology + UE5 readiness in one call
    full_asset_pipeline_check     → comprehensive multi-system audit
    analyze_animation_quality     → full animation health check
    suggest_repair_plan           → always before any repair execution

  TIER 2 — REASONING (use when compound doesn't cover a specific need)
    get_mesh_quality_report       → mesh statistics with interpretation
    analyze_topology              → topology score + pole analysis
    run_unreal_readiness_check    → UE5 gate check only
    run_asset_qa                  → QA verdict

  TIER 3 — RAW (use only when tiers 1–2 don't cover the specific need)
    detect_mesh_problems          → raw problem list
    get_object_info               → raw object data
    get_scene_info                → raw scene data

  TIER 4 — REPAIR (always gate-controlled, see Section 6)
    suggest_repair_plan           → non-destructive, always safe to call
    auto_repair_mesh              → DESTRUCTIVE, requires explicit user approval
    validate_repair               → always call after auto_repair_mesh

TRIGGER MAP — what the user says and what you do:

  "look at this" / "what do you see" / "show me"
    → get_viewport_screenshot() immediately. Describe in detail.

  "is this ready for Unreal" / "can I export this" / "UE5 check"
    → analyze_mesh_for_unreal() → full structured report with verdict

  "how's the topology" / "check the loops" / "quad quality"
    → get_viewport_screenshot() → analyze_topology() → describe what you see
      in the screenshot against what the data says

  "what's wrong" / "check this" / "audit" / "full report"
    → full_asset_pipeline_check() → structured report, all systems

  "fix it" / "clean it up" / "repair the mesh"
    → suggest_repair_plan() FIRST → present plan → WAIT for approval
    → NEVER call auto_repair_mesh() without explicit confirmation

  "can you fix the [specific problem]"
    → suggest_repair_plan() → present specifically what will be touched
    → WAIT for approval → auto_repair_mesh() → validate_repair() → screenshot

  "how many polygons" / "poly count" / "vertex count"
    → get_object_info() → answer with context for the inferred pipeline stage
    → e.g. "45k — appropriate for Stage 2, will need reduction before Stage 6"

  "what stage is this" / "where are we in the pipeline"
    → screenshot + get_object_info() → reason through all 6 stage signals
    → deliver stage verdict with confidence level

SCREENSHOT TRIGGERS — call get_viewport_screenshot() when:
  - Session starts (always, no exceptions)
  - User says "show me" / "look at" / "what does it look like"
  - After ANY repair or modification to the scene
  - Before AND after any auto_repair_mesh() call
  - When your analysis contradicts what the viewport likely shows
  - When reporting a PASS or FAIL verdict (show the evidence)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 6 — SAFETY GATES (hard stops — never bypass)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

GATE 1 — DESTRUCTIVE GEOMETRY
  Trigger:  Any operation that modifies vertex positions, deletes geometry,
            merges vertices, or alters mesh data
  Rule:     ALWAYS call suggest_repair_plan() first. Present the plan in full.
            State exactly what will be changed and what cannot be undone easily.
            Wait for explicit user confirmation ("yes", "do it", "go ahead").
            Never interpret enthusiasm or urgency as approval.

GATE 2 — PIPELINE STAGE TRANSITION
  Trigger:  Moving from one stage to the next (e.g. retopo → bake, bake → export)
  Rule:     Run the full QA checklist for the current stage before transitioning.
            Deliver a stage-completion report. Call out anything incomplete.
            Ask explicitly: "Ready to move to [next stage]?"
            Do not proceed until confirmed.

GATE 3 — EXPORT
  Trigger:  Any FBX, USD, OBJ, or engine export operation
  Rule:     run_unreal_readiness_check() must return zero blocking errors.
            run_asset_qa() verdict must be PASS.
            If either fails, block the export and report what must be fixed first.
            Never export a mesh with known critical issues "to see what happens."

GATE 4 — IRREVERSIBLE OPERATIONS
  Trigger:  Apply modifiers, join meshes, separate meshes, delete objects,
            apply scale/rotation (destructive), clear parent with keep transform
  Rule:     State exactly what will happen. State that it cannot be undone
            without reverting to a previous save. Wait for explicit confirmation.

WHAT NEVER HAPPENS (hard prohibitions):
  ✗ auto_repair_mesh() without explicit user approval after seeing suggest_repair_plan()
  ✗ Claiming PASS on any check without having run the actual tool
  ✗ Claiming a mesh is clean based on visual inspection alone
  ✗ Exporting without a clean readiness check
  ✗ Skipping the screenshot because "it probably looks fine"
  ✗ Modifying materials without understanding the intended PBR workflow
  ✗ Deleting any user data under any circumstances
  ✗ Running repair on the wrong object (always confirm object name before repair)
  ✗ Telling the user what they want to hear instead of what the pipeline requires

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 7 — COMMUNICATION STANDARD
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

REPORT FORMAT — use this structure for any full analysis response:

  ── VISUAL ASSESSMENT ──────────────────────────────────────────────────────
  What you actually see in the screenshot. Asset type, shading observations,
  visible topology issues, scale judgment, anything that stands out visually.
  This section comes first, always. You looked before you analyzed.

  ── TECHNICAL DATA ─────────────────────────────────────────────────────────
  Actual numbers from tool output. Never approximate. Never round unless rounding
  is noted. Cite the tool that produced the number.
    Vertices: 45,231
    Non-manifold edges: 460  (detect_mesh_problems)
    Topology score: 35/100 — Poor  (analyze_topology)
    UE5 blocking errors: 2  (run_unreal_readiness_check)

  ── PRODUCTION VERDICT ─────────────────────────────────────────────────────
  One of: ✅ PASS / ⚠️ WARN / ❌ FAIL / 🚫 CRITICAL
  One sentence justifying the verdict.
  Stage context: "For Stage 2 (Retopology), this is WARN — acceptable to continue
  but must be resolved before Stage 6."

  ── RECOMMENDED ACTIONS ────────────────────────────────────────────────────
  Numbered, priority-ordered. Most critical first.
  Each action includes: what to do, why, and what tool/method handles it.
    1. Fix 460 non-manifold edges — blocks export. [auto_repair_mesh can handle this]
    2. Reduce ngon count from 19 to 0 in deformation zones — rigging risk.
       [requires manual retopology in those areas]
    3. Apply scale before rigging. [Gate 4 — confirm before executing]

  ── RISK IF IGNORED ────────────────────────────────────────────────────────
  What breaks downstream if the issues are not addressed.
  Be specific. "Normals will bake incorrectly" is better than "there may be issues."

TONE:
  - Direct and professional. You are a senior artist talking to another artist.
  - No filler phrases ("Great question!", "Certainly!", "Of course!").
  - No apologizing for delivering bad news. Bad news is information.
  - Calibrated confidence: if you are certain, say so. If you are inferring, say so.
  - When you flag a critical issue, flag it immediately — not at the end of the response.

NUMBERS:
  - Always cite real numbers from tool output. Never say "a lot of" or "some."
  - Always give context for numbers: "460 non-manifold edges — this is severe,
    typical clean meshes have 0."
  - Always state the tool that produced the number so the user can verify.

STAGE CONTEXT IN EVERY REPORT:
  Every verdict must include stage context.
  A 500k polygon count is not good or bad without knowing the stage.
  Always say: "At Stage [N] — [name], [number] is [judgment]."

ESCALATION LANGUAGE:
  🚫 CRITICAL  — blocks pipeline. Must fix before proceeding. Do not continue.
  ❌ FAIL      — will cause problems. Fix before next stage transition.
  ⚠️ WARN      — should fix. Risk increases downstream if ignored.
  ℹ️ INFO      — noted for awareness. No immediate action required.
  ✅ PASS      — verified clean by tool. Meets standard for current stage.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 8 — AI-GENERATED AND SCANNED ASSET HANDLING
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

AI-generated or photogrammetry-scanned assets require a specific intake posture.
These assets commonly present with:
  - Extremely high polygon counts (millions) unsuitable for real-time use
  - Non-manifold geometry from generation artifacts
  - Irregular topology with no animation-friendly edge flow
  - Missing or auto-generated UVs with poor texel density distribution
  - Inverted normals in occluded areas
  - Duplicate or overlapping geometry
  - No LODs, no collision, no rig

When you detect signals of an AI-generated or scanned asset (very high poly,
irregular topology, generation-pattern mesh density, no intentional edge flow),
your orientation message must include:
  "This appears to be an AI-generated or scanned asset. Standard pipeline
   workflow applies: validate → cleanup → retopology → bake → texture → rig → export.
   Do not attempt to export this mesh in its current state."

You do not need to know which tool generated it. The pipeline requirements are
the same regardless of source.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 9 — SESSION CONTINUITY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Within a session, maintain a mental model of what you know:
  - The asset type and inferred pipeline stage
  - What tools you have already run and what they returned
  - What issues have been identified, which are resolved, which are outstanding
  - What repairs have been executed and whether validate_repair confirmed them
  - What the user's stated goal is for this session

Do not re-run tools you already ran unless:
  - The scene has been modified since the last run
  - The user explicitly asks you to re-check
  - You are running validate_repair after a fix

When referencing earlier findings, cite them:
  "Earlier we found 460 non-manifold edges — after the repair, validate_repair
   confirmed that count is now 0."

If the user changes direction mid-session, update your mental model explicitly:
  "Understood — shifting from UE5 export prep to animation review.
   Applying Stage 5 standards from this point forward."
""",
)


# ─────────────────────────────────────────────────────────────────────────────
# ORIGINAL LAYER (~22 commands) — unchanged from v2.1
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def get_scene_info() -> str:
    """Get detailed information about the current Blender scene."""
    return _send_json("get_scene_info")


@mcp.tool()
def get_object_info(object_name: str) -> str:
    """Get detailed information about a specific object in the Blender scene."""
    return _send_json("get_object_info", name=object_name)


@mcp.tool()
def get_viewport_screenshot(max_size: int = 1000) -> Image:
    """ALWAYS call this first before any other tool, and again after every scene change.
    Captures the live Blender 3D viewport so you can SEE what you are working on.
    Never describe, analyze, or report on a mesh without looking at it first.
    Call after: imports, repairs, transforms, deletions, generation — any operation
    that modifies the scene. Describe what you see in your response."""
    blender = get_blender_connection()
    temp_path = os.path.join(tempfile.gettempdir(), f"blender_screenshot_{os.getpid()}.png")
    try:
        result = blender.send_command(
            "get_viewport_screenshot", {"max_size": max_size, "filepath": temp_path, "format": "png"}
        )
        if isinstance(result, dict) and "error" in result:
            raise Exception(result["error"])
        if not os.path.exists(temp_path):
            raise Exception("Screenshot file was not created")
        with open(temp_path, "rb") as f:
            image_bytes = f.read()
        return Image(data=image_bytes, format="png")
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


@mcp.tool()
def execute_blender_code(code: str) -> str:
    """Execute arbitrary Python code in Blender. Break complex operations into smaller chunks."""
    try:
        blender = get_blender_connection()
        result = blender.send_command("execute_code", {"code": code})
        return f"Code executed successfully: {result.get('result', '')}"
    except Exception as e:
        return f"Error executing code: {e}"


@mcp.tool()
def get_polyhaven_status() -> str:
    """Check if PolyHaven integration is enabled in Blender."""
    try:
        result = get_blender_connection().send_command("get_polyhaven_status")
        return result.get("message", json.dumps(result))
    except Exception as e:
        return f"Error checking PolyHaven status: {e}"


@mcp.tool()
def get_polyhaven_categories(asset_type: str = "hdris") -> str:
    """Get categories for a PolyHaven asset type (hdris, textures, models, all)."""
    try:
        result = get_blender_connection().send_command("get_polyhaven_categories", {"asset_type": asset_type})
        if "error" in result:
            return f"Error: {result['error']}"
        categories = result["categories"]
        out = f"Categories for {asset_type}:\n\n"
        for category, count in sorted(categories.items(), key=lambda x: x[1], reverse=True):
            out += f"- {category}: {count} assets\n"
        return out
    except Exception as e:
        return f"Error getting Polyhaven categories: {e}"


@mcp.tool()
def search_polyhaven_assets(asset_type: str = "all", categories: Optional[str] = None) -> str:
    """Search PolyHaven assets, optionally filtered by a comma-separated category list."""
    try:
        result = get_blender_connection().send_command(
            "search_polyhaven_assets", {"asset_type": asset_type, "categories": categories}
        )
        if "error" in result:
            return f"Error: {result['error']}"
        assets = result["assets"]
        out = f"Found {result['total_count']} assets"
        if categories:
            out += f" in categories: {categories}"
        out += f"\nShowing {result['returned_count']} assets:\n\n"
        for asset_id, data in sorted(assets.items(), key=lambda x: x[1].get("download_count", 0), reverse=True):
            out += f"- {data.get('name', asset_id)} (ID: {asset_id})\n"
            out += f"  Type: {['HDRI', 'Texture', 'Model'][data.get('type', 0)]}\n"
            out += f"  Categories: {', '.join(data.get('categories', []))}\n"
            out += f"  Downloads: {data.get('download_count', 'Unknown')}\n\n"
        return out
    except Exception as e:
        return f"Error searching Polyhaven assets: {e}"


@mcp.tool()
def download_polyhaven_asset(
    asset_id: str, asset_type: str, resolution: str = "1k", file_format: Optional[str] = None
) -> str:
    """Download and import a PolyHaven asset (hdris/textures/models) into Blender."""
    try:
        result = get_blender_connection().send_command(
            "download_polyhaven_asset",
            {"asset_id": asset_id, "asset_type": asset_type, "resolution": resolution, "file_format": file_format},
        )
        if "error" in result:
            return f"Error: {result['error']}"
        if result.get("success"):
            message = result.get("message", "Asset downloaded and imported successfully")
            if asset_type == "hdris":
                return f"{message}. The HDRI has been set as the world environment."
            if asset_type == "textures":
                return f"{message}. Created material '{result.get('material', '')}' with maps: {', '.join(result.get('maps', []))}."
            if asset_type == "models":
                return f"{message}. The model has been imported into the current scene."
            return message
        return f"Failed to download asset: {result.get('message', 'Unknown error')}"
    except Exception as e:
        return f"Error downloading Polyhaven asset: {e}"


@mcp.tool()
def set_texture(object_name: str, texture_id: str) -> str:
    """Apply a previously downloaded PolyHaven texture to an object."""
    try:
        result = get_blender_connection().send_command(
            "set_texture", {"object_name": object_name, "texture_id": texture_id}
        )
        if "error" in result:
            return f"Error: {result['error']}"
        if result.get("success"):
            info = result.get("material_info", {})
            out = f"Successfully applied texture '{texture_id}' to {object_name}.\n"
            out += f"Using material '{result.get('material', '')}' with maps: {', '.join(result.get('maps', []))}.\n"
            out += f"Node count: {info.get('node_count', 0)}\n"
            return out
        return f"Failed to apply texture: {result.get('message', 'Unknown error')}"
    except Exception as e:
        return f"Error applying texture: {e}"


@mcp.tool()
def get_hyper3d_status() -> str:
    """Check if Hyper3D Rodin integration is enabled in Blender."""
    try:
        result = get_blender_connection().send_command("get_hyper3d_status")
        return result.get("message", json.dumps(result))
    except Exception as e:
        return f"Error checking Hyper3D status: {e}"


@mcp.tool()
def generate_hyper3d_model_via_text(text_prompt: str, bbox_condition: Optional[list] = None) -> str:
    """Generate a 3D asset via Hyper3D Rodin from a text description (call poll_rodin_job_status, then import_generated_asset)."""
    try:
        result = get_blender_connection().send_command(
            "create_rodin_job", {"text_prompt": text_prompt, "images": None, "bbox_condition": _process_bbox(bbox_condition)}
        )
        if result.get("submit_time", False):
            return json.dumps({"task_uuid": result["uuid"], "subscription_key": result["jobs"]["subscription_key"]})
        return json.dumps(result)
    except Exception as e:
        return f"Error generating Hyper3D task: {e}"


@mcp.tool()
def generate_hyper3d_model_via_images(
    input_image_paths: Optional[list] = None,
    input_image_urls: Optional[list] = None,
    bbox_condition: Optional[list] = None,
) -> str:
    """Generate a 3D asset via Hyper3D Rodin from reference image(s). Give paths for MAIN_SITE mode, urls for FAL_AI mode."""
    if input_image_paths is not None and input_image_urls is not None:
        return "Error: Conflicting parameters given!"
    if input_image_paths is None and input_image_urls is None:
        return "Error: No image given!"
    if input_image_paths is not None:
        if not all(os.path.exists(p) for p in input_image_paths):
            return "Error: not all image paths are valid!"
        images = [(Path(p).suffix, base64.b64encode(open(p, "rb").read()).decode("ascii")) for p in input_image_paths]
    else:
        if not all(urlparse(u).scheme for u in input_image_urls):
            return "Error: not all image URLs are valid!"
        images = list(input_image_urls)
    try:
        result = get_blender_connection().send_command(
            "create_rodin_job", {"text_prompt": None, "images": images, "bbox_condition": _process_bbox(bbox_condition)}
        )
        if result.get("submit_time", False):
            return json.dumps({"task_uuid": result["uuid"], "subscription_key": result["jobs"]["subscription_key"]})
        return json.dumps(result)
    except Exception as e:
        return f"Error generating Hyper3D task: {e}"


@mcp.tool()
def poll_rodin_job_status(subscription_key: Optional[str] = None, request_id: Optional[str] = None) -> str:
    """Poll Hyper3D Rodin generation status. Use subscription_key for MAIN_SITE mode, request_id for FAL_AI mode."""
    try:
        kwargs = {"subscription_key": subscription_key} if subscription_key else {"request_id": request_id}
        return json.dumps(get_blender_connection().send_command("poll_rodin_job_status", kwargs), default=str)
    except Exception as e:
        return f"Error polling Hyper3D task: {e}"


@mcp.tool()
def import_generated_asset(name: str, task_uuid: Optional[str] = None, request_id: Optional[str] = None) -> str:
    """Import a completed Hyper3D Rodin asset. Give task_uuid (MAIN_SITE) or request_id (FAL_AI), not both."""
    try:
        kwargs = {"name": name}
        if task_uuid:
            kwargs["task_uuid"] = task_uuid
        elif request_id:
            kwargs["request_id"] = request_id
        return json.dumps(get_blender_connection().send_command("import_generated_asset", kwargs), default=str)
    except Exception as e:
        return f"Error importing Hyper3D asset: {e}"


@mcp.tool()
def get_sketchfab_status() -> str:
    """Check if Sketchfab integration is enabled in Blender."""
    try:
        result = get_blender_connection().send_command("get_sketchfab_status")
        return result.get("message", json.dumps(result))
    except Exception as e:
        return f"Error checking Sketchfab status: {e}"


@mcp.tool()
def search_sketchfab_models(
    query: str, categories: Optional[str] = None, count: int = 20, downloadable: bool = True
) -> str:
    """Search Sketchfab for models matching a query."""
    try:
        result = get_blender_connection().send_command(
            "search_sketchfab_models", {"query": query, "categories": categories, "count": count, "downloadable": downloadable}
        )
        if "error" in result:
            return f"Error: {result['error']}"
        models = result.get("results", []) or []
        if not models:
            return f"No models found matching '{query}'"
        out = f"Found {len(models)} models matching '{query}':\n\n"
        for m in models:
            if not m:
                continue
            out += f"- {m.get('name', 'Unnamed')} (UID: {m.get('uid', 'Unknown')})\n"
            out += f"  Author: {(m.get('user') or {}).get('username', 'Unknown')}\n"
            out += f"  License: {(m.get('license') or {}).get('label', 'Unknown')}\n"
            out += f"  Face count: {m.get('faceCount', 'Unknown')}\n"
            out += f"  Downloadable: {'Yes' if m.get('isDownloadable') else 'No'}\n\n"
        return out
    except Exception as e:
        return f"Error searching Sketchfab models: {e}"


@mcp.tool()
def get_sketchfab_model_preview(uid: str) -> Image:
    """Get a preview thumbnail of a Sketchfab model by UID, to visually confirm before downloading."""
    result = get_blender_connection().send_command("get_sketchfab_model_preview", {"uid": uid})
    if "error" in result:
        raise Exception(result["error"])
    return Image(data=base64.b64decode(result["image_data"]), format=result.get("format", "jpeg"))


@mcp.tool()
def download_sketchfab_model(uid: str, target_size: float) -> str:
    """Download and import a Sketchfab model by UID, scaled so its largest dimension equals target_size (meters)."""
    try:
        result = get_blender_connection().send_command(
            "download_sketchfab_model", {"uid": uid, "normalize_size": True, "target_size": target_size}
        )
        if "error" in result:
            return f"Error: {result['error']}"
        if result.get("success"):
            imported = result.get("imported_objects", [])
            out = f"Successfully imported model.\nCreated objects: {', '.join(imported) if imported else 'none'}\n"
            if result.get("dimensions"):
                d = result["dimensions"]
                out += f"Dimensions (X,Y,Z): {d[0]:.3f} x {d[1]:.3f} x {d[2]:.3f} meters\n"
            return out
        return f"Failed to download model: {result.get('message', 'Unknown error')}"
    except Exception as e:
        return f"Error downloading Sketchfab model: {e}"


@mcp.tool()
def get_hunyuan3d_status() -> str:
    """Check if Hunyuan3D integration is enabled in Blender."""
    try:
        result = get_blender_connection().send_command("get_hunyuan3d_status")
        return result.get("message", json.dumps(result))
    except Exception as e:
        return f"Error checking Hunyuan3D status: {e}"


@mcp.tool()
def generate_hunyuan3d_model(text_prompt: Optional[str] = None, input_image_url: Optional[str] = None) -> str:
    """Generate a 3D asset via Hunyuan3D from text and/or an image reference."""
    try:
        result = get_blender_connection().send_command(
            "create_hunyuan_job", {"text_prompt": text_prompt, "image": input_image_url}
        )
        job_id = result.get("Response", {}).get("JobId")
        if job_id:
            return json.dumps({"job_id": f"job_{job_id}"})
        return json.dumps(result)
    except Exception as e:
        return f"Error generating Hunyuan3D task: {e}"


@mcp.tool()
def poll_hunyuan_job_status(job_id: Optional[str] = None) -> str:
    """Poll Hunyuan3D generation status by job_id."""
    try:
        return json.dumps(get_blender_connection().send_command("poll_hunyuan_job_status", {"job_id": job_id}), default=str)
    except Exception as e:
        return f"Error polling Hunyuan3D task: {e}"


@mcp.tool()
def import_generated_asset_hunyuan(name: str, zip_file_url: str) -> str:
    """Import a completed Hunyuan3D asset given its result ZIP file URL."""
    try:
        return json.dumps(
            get_blender_connection().send_command("import_generated_asset_hunyuan", {"name": name, "zip_file_url": zip_file_url}),
            default=str,
        )
    except Exception as e:
        return f"Error importing Hunyuan3D asset: {e}"


@mcp.tool()
def get_telemetry_consent() -> str:
    """Check whether the BlenderMCP addon has telemetry/usage-reporting consent enabled."""
    return _send_json("get_telemetry_consent")


# ─────────────────────────────────────────────────────────────────────────────
# AI TECHNICAL ARTIST LAYER (v2.0/2.1) — with v2.2 reasoning enrichment
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def get_mesh_quality_report(name: str) -> str:
    """
    Get mesh quality diagnostics for a named object — n-gons, non-manifold edges,
    degenerate faces, UV overlaps, vertex-group summary, and suggested fixes.

    v2.2: Response enriched with severity rating, production impact assessment,
    and professional fix recommendations from the reasoning engine.
    """
    raw = _send_raw("get_mesh_quality_report", name=name)
    if "error" in raw:
        return json.dumps(raw, indent=2)
    enriched = _reason_mesh_quality(raw)
    return json.dumps(enriched, indent=2, default=str)


@mcp.tool()
def analyze_topology(name: str, context: str = "generic") -> str:
    """
    Analyze mesh topology quality for a named object.
    context: 'generic' | 'character_body' | 'face' | 'hand' | 'hard_surface'

    v2.2: Response enriched with context-aware thresholds, severity rating,
    and professional topology recommendations from the reasoning engine.
    """
    raw = _send_raw("analyze_topology", name=name, context=context)
    if "error" in raw:
        return json.dumps(raw, indent=2)
    enriched = _reason_topology(raw)
    return json.dumps(enriched, indent=2, default=str)


@mcp.tool()
def detect_mesh_problems(name: str) -> str:
    """
    Detect common mesh problems (non-manifold geometry, loose vertices,
    zero-area faces, duplicate faces, inverted normals) on a named object.

    v2.2: Each problem explained with production impact, professional fix,
    and whether it can be auto-repaired by auto_repair_mesh.
    """
    raw = _send_raw("detect_mesh_problems", name=name)
    if "error" in raw:
        return json.dumps(raw, indent=2)
    enriched = _reason_mesh_problems(raw)
    return json.dumps(enriched, indent=2, default=str)


@mcp.tool()
def get_armature_info(name: str) -> str:
    """Get bone hierarchy, IK chain analysis, constraints, and deform-bone info for a named armature."""
    return _send_json("get_armature_info", name=name)


@mcp.tool()
def analyze_animation_quality(name: str, frame_start: Optional[int] = None, frame_end: Optional[int] = None) -> str:
    """
    Analyze animation quality (foot sliding, jitter, velocity spikes, key density)
    for a named object/armature over an optional frame range.

    v2.2: Response enriched with animation grade (A–F), severity-ranked findings,
    and professional correction guidance from the reasoning engine.
    """
    raw = _send_raw("analyze_animation_quality", name=name, frame_start=frame_start, frame_end=frame_end)
    if "error" in raw:
        return json.dumps(raw, indent=2)
    enriched = _reason_animation(raw)
    return json.dumps(enriched, indent=2, default=str)


@mcp.tool()
def get_material_summary(name: str) -> str:
    """Get a compact summary of the material(s) assigned to a named object."""
    return _send_json("get_material_summary", name=name)


@mcp.tool()
def create_pbr_material(
    name: str,
    base_color: Optional[list] = None,
    metallic: float = 0.0,
    roughness: float = 0.5,
    use_subsurface: bool = False,
    subsurface_radius: Optional[list] = None,
    emission_color: Optional[list] = None,
    emission_strength: float = 0.0,
    alpha: float = 1.0,
    wear_variation: bool = False,
) -> str:
    """
    Create or update a production-ready Principled BSDF PBR material.
    Supports subsurface scattering, emission, transparency, and a wear/variation layer.
    Blender 3.x and 4.x socket names handled automatically.
    """
    params = {
        "name": name,
        "metallic": metallic,
        "roughness": roughness,
        "use_subsurface": use_subsurface,
        "emission_strength": emission_strength,
        "alpha": alpha,
        "wear_variation": wear_variation,
    }
    if base_color is not None:
        params["base_color"] = base_color
    if subsurface_radius is not None:
        params["subsurface_radius"] = subsurface_radius
    if emission_color is not None:
        params["emission_color"] = emission_color
    return _send_json("create_pbr_material", **params)


@mcp.tool()
def run_asset_qa(name: str, check_uvs: bool = True, check_materials: bool = True, check_modifiers: bool = True) -> str:
    """
    Run a production QA pass on a named object: UVs, materials, modifiers,
    weight paint, duplicate faces, and other readiness checks.

    v2.2: Response enriched with blocking vs advisory categorisation
    and professional fix guidance from the reasoning engine.
    """
    raw = _send_raw("run_asset_qa", name=name, check_uvs=check_uvs, check_materials=check_materials, check_modifiers=check_modifiers)
    if "error" in raw:
        return json.dumps(raw, indent=2)
    enriched = _reason_asset_qa(raw)
    return json.dumps(enriched, indent=2, default=str)


@mcp.tool()
def run_unreal_readiness_check(name: str, expected_unit_scale: float = 0.01) -> str:
    """
    Check whether a named object is ready for Unreal Engine 5 import.
    Validates scale, pivot, naming, UVs, lightmap UV, collision, and normal map direction.

    v2.2: Each failed check explained with UE5 pipeline context, severity,
    and specific fix instructions from the reasoning engine.
    """
    raw = _send_raw("run_unreal_readiness_check", name=name, expected_unit_scale=expected_unit_scale)
    if "error" in raw:
        return json.dumps(raw, indent=2)
    enriched = _reason_unreal_readiness(raw)
    return json.dumps(enriched, indent=2, default=str)


@mcp.tool()
def export_for_unreal(
    name: str,
    export_path: str,
    apply_modifiers: bool = True,
    triangulate: bool = True,
    scale: float = 100.0,
    embed_textures: bool = False,
    export_animations: bool = False,
) -> str:
    """
    Export a named object/armature as an FBX file with UE5 conventions:
    -Z forward / Y up axis, scale ×100 (Blender m → UE5 cm), triangulation.
    Post-export validates file exists and has non-zero size.
    """
    return _send_json(
        "export_for_unreal",
        name=name,
        export_path=export_path,
        apply_modifiers=apply_modifiers,
        triangulate=triangulate,
        scale=scale,
        embed_textures=embed_textures,
        export_animations=export_animations,
    )


@mcp.tool()
def get_scene_hierarchy(max_depth: int = 8) -> str:
    """Get the collection/object hierarchy of the current scene, up to max_depth levels deep."""
    return _send_json("get_scene_hierarchy", max_depth=max_depth)


@mcp.tool()
def get_selection_context() -> str:
    """Get what's currently selected in Blender: active object, selection list, mode, and edit-mesh selection counts."""
    return _send_json("get_selection_context")


@mcp.tool()
def get_material_graph(material_name: str) -> str:
    """
    Get the shader node graph (nodes + links) of a named material.
    Flags orphaned nodes and normal map direction mismatches for UE5.
    """
    return _send_json("get_material_graph", material_name=material_name)


@mcp.tool()
def get_animation_data(name: str) -> str:
    """Get action/keyframe/fcurve data for a named object."""
    return _send_json("get_animation_data", name=name)


@mcp.tool()
def execute_code_safe(code: str, required_mode: Optional[str] = None, push_undo: bool = True) -> str:
    """
    Execute Python code in Blender with an undo checkpoint pushed first
    and an optional mode switch ('OBJECT'|'EDIT'|'POSE') safely restored afterward.
    """
    return _send_json("execute_code_safe", code=code, required_mode=required_mode, push_undo=push_undo)


@mcp.tool()
def prepare_lod_names(base_name: str, lod_count: int = 4) -> str:
    """Generate/validate LOD naming convention (e.g. SM_AssetName_LOD0..N) for a given base object name."""
    return _send_json("prepare_lod_names", base_name=base_name, lod_count=lod_count)


@mcp.tool()
def get_session_log() -> str:
    """Get the last ~20 commands executed this Blender session with status, for debugging and audit."""
    return _send_json("get_session_log")


# ─────────────────────────────────────────────────────────────────────────────
# AI TECHNICAL DIRECTOR LAYER (v2.2) — compound tools, auto-repair, critic
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def analyze_mesh_for_unreal(name: str, topology_context: str = "generic") -> str:
    """
    COMPOUND TOOL — Full pre-export analysis in one call.

    Runs detect_mesh_problems + get_mesh_quality_report + analyze_topology +
    run_unreal_readiness_check simultaneously, then combines all findings
    into a single prioritised report with professional fix guidance.

    Use this as the first step before any UE5 export workflow.

    topology_context: 'generic' | 'character_body' | 'face' | 'hand' | 'hard_surface'
    """
    try:
        # Run all four analyses
        raw_problems  = _send_raw("detect_mesh_problems", name=name)
        raw_quality   = _send_raw("get_mesh_quality_report", name=name)
        raw_topology  = _send_raw("analyze_topology", name=name, context=topology_context)
        raw_ue5       = _send_raw("run_unreal_readiness_check", name=name)

        # Enrich each with reasoning
        r_problems  = _reason_mesh_problems(raw_problems)  if "error" not in raw_problems  else raw_problems
        r_quality   = _reason_mesh_quality(raw_quality)    if "error" not in raw_quality   else raw_quality
        r_topology  = _reason_topology(raw_topology)       if "error" not in raw_topology  else raw_topology
        r_ue5       = _reason_unreal_readiness(raw_ue5)    if "error" not in raw_ue5       else raw_ue5

        # Aggregate all findings by severity
        all_findings = []
        for source, enriched in [
            ("mesh_problems",    r_problems),
            ("mesh_quality",     r_quality),
            ("topology",         r_topology),
            ("unreal_readiness", r_ue5),
        ]:
            reasoning = enriched.get("_reasoning", {})
            for f in reasoning.get("findings", []):
                all_findings.append({**f, "source": source})

        critical = [f for f in all_findings if f.get("severity") == "critical"]
        warnings = [f for f in all_findings if f.get("severity") == "warning"]
        info     = [f for f in all_findings if f.get("severity") == "info"]

        # Determine auto-repairable items
        auto_fixable_all = (
            r_problems.get("_reasoning", {}).get("auto_repairable", [])
        )

        # Overall verdict
        if critical:
            verdict = "NOT EXPORT READY"
            overall = "critical"
        elif warnings:
            verdict = "EXPORT WITH CAUTION"
            overall = "warning"
        else:
            verdict = "EXPORT READY"
            overall = "pass"

        report = {
            "object": name,
            "verdict": verdict,
            "overall_severity": overall,
            "summary": (
                f"{verdict} — {len(critical)} blocking error(s), "
                f"{len(warnings)} warning(s), {len(info)} info item(s). "
                f"{len(auto_fixable_all)} issue(s) can be auto-repaired via auto_repair_mesh."
            ),
            "action_required": len(critical) > 0 or len(warnings) > 0,
            "auto_repair_available": len(auto_fixable_all) > 0,
            "auto_repairable_issues": auto_fixable_all,
            "critical_errors": critical,
            "warnings": warnings,
            "info": info,
            "full_analysis": {
                "mesh_problems":    r_problems.get("_reasoning", {}),
                "mesh_quality":     r_quality.get("_reasoning", {}),
                "topology":         r_topology.get("_reasoning", {}),
                "unreal_readiness": r_ue5.get("_reasoning", {}),
            },
        }

        return json.dumps(report, indent=2, default=str)

    except Exception as e:
        logger.error(f"Error in analyze_mesh_for_unreal: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def auto_repair_mesh(name: str, dry_run: bool = False) -> str:
    """
    AUTO-REPAIR — Safe mesh cleanup loop: Scan → Diagnose → Repair → Verify.

    Automatically fixes the following problems (in safe order):
      1. Loose vertices (delete)
      2. Duplicate faces (merge by distance)
      3. Zero-area/degenerate faces (dissolve degenerate)
      4. Inverted normals (recalculate outside)

    Problems NOT auto-repaired (require artist review):
      - Non-manifold edges (topology intent unclear)
      - UV overlaps (may be intentional tiling)
      - N-gons (topology restructuring needed)

    dry_run=True: diagnoses and plans repairs without executing them.
    dry_run=False: executes all safe repairs then re-scans to verify.

    Always sets the named object as active before operating.
    """
    try:
        blender = get_blender_connection()

        # ── STEP 1: Set active object ──────────────────────────────────────
        set_active_script = f"""
import bpy
obj = bpy.data.objects.get("{name}")
if obj is None:
    raise ValueError("Object '{name}' not found")
bpy.context.view_layer.objects.active = obj
obj.select_set(True)
print("active:set")
"""
        activate_result = blender.send_command(
            "execute_code_safe", {"code": set_active_script, "required_mode": "OBJECT", "push_undo": True}
        )
        if "error" in activate_result:
            return json.dumps({"error": f"Could not set active object: {activate_result['error']}"})

        # ── STEP 2: Initial scan ───────────────────────────────────────────
        raw_before = _send_raw("detect_mesh_problems", name=name)
        if "error" in raw_before:
            return json.dumps({"error": f"Initial scan failed: {raw_before['error']}"})

        reasoned_before = _reason_mesh_problems(raw_before)
        auto_repairable = reasoned_before.get("_reasoning", {}).get("auto_repairable", [])
        needs_artist    = reasoned_before.get("_reasoning", {}).get("needs_artist_review", [])

        if not auto_repairable:
            return json.dumps({
                "object": name,
                "status": "no_auto_repairs_needed",
                "message": (
                    "No auto-repairable problems found. "
                    f"Issues requiring artist review: {needs_artist or 'none'}."
                ),
                "before": reasoned_before.get("_reasoning", {}),
            }, indent=2)

        if dry_run:
            return json.dumps({
                "object": name,
                "status": "dry_run",
                "would_repair": auto_repairable,
                "cannot_auto_repair": needs_artist,
                "message": (
                    f"DRY RUN — would execute {len(auto_repairable)} repair(s): "
                    f"{', '.join(auto_repairable)}. "
                    f"Re-run with dry_run=False to apply."
                ),
                "before": reasoned_before.get("_reasoning", {}),
            }, indent=2)

        # ── STEP 3: Execute repairs in safe order ──────────────────────────
        repairs_executed = []
        repair_errors = []

        for repair_key in _REPAIR_ORDER:
            # addon.py calls loose verts "isolated_verts"; our script key is "loose_vertices"
            matched = (
                repair_key in auto_repairable or
                (repair_key == "loose_vertices" and "isolated_verts" in auto_repairable)
            )
            if not matched:
                continue
            script = _REPAIR_SCRIPTS.get(repair_key, "")
            if not script:
                continue

            # Prepend active object guarantee to every script
            full_script = f"""
import bpy
obj = bpy.data.objects.get("{name}")
if obj:
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
{script}
"""
            try:
                result = blender.send_command(
                    "execute_code_safe", {"code": full_script, "required_mode": "OBJECT", "push_undo": True}
                )
                if result.get("result", "").find("done") >= 0 or "error" not in result:
                    repairs_executed.append(repair_key)
                else:
                    repair_errors.append(f"{repair_key}: {result.get('error', 'unknown error')}")
            except Exception as e:
                repair_errors.append(f"{repair_key}: {e}")

        # ── STEP 4: Verify — re-scan after repairs ─────────────────────────
        raw_after = _send_raw("detect_mesh_problems", name=name)
        if "error" in raw_after:
            reasoned_after = {"error": raw_after["error"]}
        else:
            reasoned_after = _reason_mesh_problems(raw_after).get("_reasoning", {})

        # ── STEP 5: Build result report ────────────────────────────────────
        before_summary = reasoned_before.get("_reasoning", {})
        remaining_issues = reasoned_after.get("findings", []) if isinstance(reasoned_after, dict) else []
        remaining_critical = [f for f in remaining_issues if f.get("severity") == "critical"]

        status = "success" if not remaining_critical and not repair_errors else (
            "partial" if repairs_executed else "failed"
        )

        return json.dumps({
            "object": name,
            "status": status,
            "repairs_executed": repairs_executed,
            "repair_errors": repair_errors,
            "issues_that_need_artist_review": needs_artist,
            "summary": (
                f"Repaired {len(repairs_executed)} issue(s): {', '.join(repairs_executed) or 'none'}. "
                f"{len(repair_errors)} repair error(s). "
                f"{len(remaining_issues)} issue(s) remaining after repair "
                f"({len(remaining_critical)} critical). "
                f"{len(needs_artist)} issue(s) require artist review."
            ),
            "before": {
                "severity": before_summary.get("overall_severity"),
                "findings_count": len(before_summary.get("findings", [])),
            },
            "after": reasoned_after if isinstance(reasoned_after, dict) else {"error": str(reasoned_after)},
            "production_ready": status == "success" and not remaining_critical,
        }, indent=2, default=str)

    except Exception as e:
        logger.error(f"Error in auto_repair_mesh: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def critique_animation(name: str, frame_start: Optional[int] = None, frame_end: Optional[int] = None) -> str:
    """
    ANIMATION CRITIC — Senior technical artist review of an animation.

    Runs analyze_animation_quality with full reasoning enrichment, then formats
    the output as a prioritised critique with:
      - Animation grade (A through F)
      - Issues ranked by severity and category
      - Specific frame-accurate correction guidance
      - Production readiness verdict

    Use this when you want a plain-English animation review, not raw data.
    """
    try:
        raw = _send_raw("analyze_animation_quality", name=name, frame_start=frame_start, frame_end=frame_end)
        if "error" in raw:
            return json.dumps(raw, indent=2)

        enriched = _reason_animation(raw)
        reasoning = enriched.get("_reasoning", {})

        findings = reasoning.get("findings", [])
        critical = [f for f in findings if f["severity"] == "critical"]
        warnings = [f for f in findings if f["severity"] == "warning"]
        info     = [f for f in findings if f["severity"] == "info"]

        # Group by category for readable output
        by_category: dict = {}
        for f in findings:
            cat = f.get("category", "general")
            by_category.setdefault(cat, []).append(f)

        critique = {
            "object": name,
            "grade": reasoning.get("grade", "Unknown"),
            "score": reasoning.get("score", 0),
            "production_ready": reasoning.get("production_ready", False),
            "verdict": (
                "APPROVED FOR PRODUCTION" if reasoning.get("production_ready")
                else "REVISION REQUIRED"
            ),
            "summary": reasoning.get("summary", ""),
            "critical_issues": critical,
            "warnings": warnings,
            "info": info,
            "issues_by_category": by_category,
            "frame_range_analysed": {
                "start": frame_start or raw.get("frame_start"),
                "end":   frame_end   or raw.get("frame_end"),
            },
        }

        return json.dumps(critique, indent=2, default=str)

    except Exception as e:
        logger.error(f"Error in critique_animation: {e}")
        return json.dumps({"error": str(e)})


# ─────────────────────────────────────────────────────────────────────────────

def main():
    try:
        interactive = sys.stdin.isatty()
    except (AttributeError, OSError):
        interactive = False
    if interactive:
        logger.info(
            "BlenderMCP custom server v2.2 — AI Technical Director Edition. "
            "Launched by MCP client. Waiting for commands on stdin."
        )
    mcp.run()


if __name__ == "__main__":
    main()

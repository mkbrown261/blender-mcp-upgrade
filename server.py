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


# ─────────────────────────────────────────────────────────────────────────────
# PIPELINE STAGE CLASSIFIER — internal helper
# ─────────────────────────────────────────────────────────────────────────────

def _classify_stage_from_signals(obj_info: dict, mesh_stats: dict) -> dict:
    """
    Infer the production pipeline stage from raw object and mesh data.

    Returns a structured verdict with:
      stage_number    : 1–6
      stage_name      : human-readable name
      confidence      : "high" | "medium" | "low"
      signals_detected: list of str — what evidence drove the decision
      standards       : list of str — what QA standards apply at this stage
      next_steps      : list of str — recommended actions to progress
      ambiguous       : bool — True if two stages are equally plausible
      alternate_stage : optional stage_number if ambiguous
    """
    signals_detected = []
    stage_scores = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0}

    # ── Extract signals from obj_info (get_object_info real schema) ───────────
    # materials = list of name strings ["mat_a", "mat_b", ...]
    # mesh data at obj_info["mesh"]["vertices"] / ["polygons"]
    # NO top-level vertex_count / face_count / armature / modifiers keys
    mat_list       = obj_info.get("materials", [])
    has_materials  = bool(mat_list)
    material_count = len(mat_list) if isinstance(mat_list, list) else 0
    mesh_block     = obj_info.get("mesh", {})
    vertex_count   = mesh_block.get("vertices", 0) or 0
    face_count     = mesh_block.get("polygons",  0) or 0

    # ── Extract signals from mesh_stats (get_mesh_quality_report real schema) ─
    # counts.verts / counts.edges / counts.faces
    # face_types.tris / quads / ngons
    # problems dict: non_manifold_edges / isolated_verts / zero_area_faces / duplicate_faces / boundary_edges
    # uv.has_uvs / uv.layer_count
    # modifiers: [{name, type, show_viewport}]
    # rigging.deform_modifiers: list of modifier type strings
    # health: "clean" | "issues_found"
    counts     = mesh_stats.get("counts", {})
    face_types = mesh_stats.get("face_types", {})
    uv_data    = mesh_stats.get("uv", {})
    problems   = mesh_stats.get("problems", {})
    health     = mesh_stats.get("health", "")
    rigging    = mesh_stats.get("rigging", {})
    mod_list   = mesh_stats.get("modifiers", [])

    has_uvs        = uv_data.get("has_uvs", False)
    uv_layer_count = uv_data.get("layer_count", 0)
    quad_count     = face_types.get("quads", 0)
    tri_count      = face_types.get("tris",  0)
    ngon_count     = face_types.get("ngons", 0)
    total_faces    = quad_count + tri_count + ngon_count or face_count
    quad_pct       = (quad_count / total_faces * 100) if total_faces > 0 else 0
    nm_edges       = problems.get("non_manifold_edges", 0)

    # Armature: inferred from deform_modifiers list in rigging block
    deform_mods    = rigging.get("deform_modifiers", [])
    has_armature   = "ARMATURE" in deform_mods

    # Modifier types from the modifiers list in mesh_stats
    modifier_types = [m.get("type", "") for m in mod_list if isinstance(m, dict)]
    has_multires   = "MULTIRES" in modifier_types
    has_subsurf    = "SUBSURF"  in modifier_types
    no_modifiers   = len(modifier_types) == 0

    # ── STAGE 1 — Concept / Sculpt ────────────────────────────────────────────
    if vertex_count > 300_000:
        stage_scores[1] += 3
        signals_detected.append(f"Very high vertex count ({vertex_count:,}) — typical of sculpts")
    if has_multires:
        stage_scores[1] += 3
        signals_detected.append("Multires modifier present — sculpt workflow")
    if not has_uvs and vertex_count > 100_000:
        stage_scores[1] += 2
        signals_detected.append("No UVs on high-poly mesh — pre-retopo")
    if ngon_count > quad_count and vertex_count > 50_000:
        stage_scores[1] += 1
        signals_detected.append("Ngon-heavy topology — typical of sculpt/scan")

    # ── STAGE 2 — Retopology / Base Mesh ──────────────────────────────────────
    if 3_000 <= vertex_count <= 120_000:
        stage_scores[2] += 2
        signals_detected.append(f"Vertex count ({vertex_count:,}) in retopo/game-mesh range")
    if quad_pct > 70 and total_faces > 100:
        stage_scores[2] += 2
        signals_detected.append(f"Quad-dominant mesh ({quad_pct:.0f}% quads) — intentional retopo")
    if not has_uvs and vertex_count < 120_000:
        stage_scores[2] += 1
        signals_detected.append("No UVs yet — retopo may be in progress")
    if has_subsurf:
        stage_scores[2] += 1
        signals_detected.append("Subsurf modifier — base mesh workflow")

    # ── STAGE 3 — Bake-Ready ──────────────────────────────────────────────────
    if has_uvs and not has_materials and vertex_count < 120_000:
        stage_scores[3] += 3
        signals_detected.append("UVs present, no material — classic bake-ready state")
    if uv_layer_count >= 2:
        stage_scores[3] += 2
        signals_detected.append(f"{uv_layer_count} UV channels — lightmap/bake setup")
    if has_uvs and vertex_count < 120_000 and not has_armature:
        stage_scores[3] += 1

    # ── STAGE 4 — Texture / Material ──────────────────────────────────────────
    if has_uvs and has_materials and material_count >= 1:
        stage_scores[4] += 3
        signals_detected.append(f"UVs + {material_count} material(s) — texture/material stage")
    if material_count > 1:
        stage_scores[4] += 1
        signals_detected.append(f"Multiple materials ({material_count}) — multi-material asset")

    # ── STAGE 5 — Rig / Animation ─────────────────────────────────────────────
    if has_armature:
        stage_scores[5] += 4
        signals_detected.append("ARMATURE deform modifier detected — rig/animation stage")
    if has_armature and has_materials:
        stage_scores[5] += 1
        signals_detected.append("Armature + materials — rigged character setup")

    # ── STAGE 6 — Export-Ready / Unreal Prep ──────────────────────────────────
    if has_uvs and has_materials and nm_edges == 0 and health == "clean":
        stage_scores[6] += 3
        signals_detected.append("Clean mesh + UVs + materials — potential export candidate")
    if uv_layer_count >= 2 and has_materials and nm_edges == 0:
        stage_scores[6] += 2
        signals_detected.append("Lightmap UV + clean mesh — export-ready signals")
    if no_modifiers:
        stage_scores[6] += 1
        signals_detected.append("No modifiers present — modifiers likely already applied")

    # ── Determine winner ───────────────────────────────────────────────────────
    sorted_stages = sorted(stage_scores.items(), key=lambda x: x[1], reverse=True)
    top_stage, top_score   = sorted_stages[0]
    sec_stage, sec_score   = sorted_stages[1]

    # Ambiguity: top two within 1 point → ambiguous, default to higher-demand stage
    ambiguous = (top_score - sec_score) <= 1 and top_score > 0
    if ambiguous:
        # Higher stage number = more demanding standards
        primary = max(top_stage, sec_stage)
        alternate = min(top_stage, sec_stage)
    else:
        primary   = top_stage
        alternate = sec_stage

    confidence = "high" if (top_score - sec_score) >= 3 else \
                 "medium" if (top_score - sec_score) >= 1 else "low"

    # ── Stage metadata ─────────────────────────────────────────────────────────
    STAGE_META = {
        1: {
            "name": "Concept / Sculpt",
            "standards": [
                "No polygon limits — detail capture is the goal",
                "Topology quality not evaluated at this stage",
                "Focus: sculpt fidelity and silhouette",
            ],
            "next_steps": [
                "Retopology to game-ready mesh (~5k–80k verts depending on asset)",
                "UV unwrap on the retopo mesh",
                "Bake high-to-low normal/AO/curvature maps",
            ],
        },
        2: {
            "name": "Retopology / Base Mesh",
            "standards": [
                "Quad dominance >85% in deformation zones",
                "Animation-friendly edge loops at all joints",
                "Poles placed away from deformation areas",
                "No ngons in areas that will deform",
                "Density appropriate for deformation complexity",
            ],
            "next_steps": [
                "UV unwrap with appropriate seam placement",
                "Validate deformation topology before rigging",
                "Set up high-poly bake source if not already done",
            ],
        },
        3: {
            "name": "Bake-Ready",
            "standards": [
                "UV islands non-overlapping (unless intentional)",
                "No UV stretching >20%",
                "Sufficient projection distance from high-poly",
                "No flipped normals on low-poly",
                "Matching silhouette with high-poly source",
            ],
            "next_steps": [
                "Bake normal map, AO, curvature, and color ID",
                "Validate bake output for projection errors",
                "Set up PBR material with baked maps",
            ],
        },
        4: {
            "name": "Texture / Material",
            "standards": [
                "PBR metallic/roughness workflow (not specular)",
                "Power-of-2 texture resolutions (512/1024/2048/4096)",
                "Consistent texel density across the asset",
                "No broken image paths",
                "Material count appropriate for draw call budget",
            ],
            "next_steps": [
                "Verify PBR material response in rendered view",
                "Check texel density consistency",
                "Begin rig setup if character asset",
                "Export test to Unreal to verify material response",
            ],
        },
        5: {
            "name": "Rig / Animation",
            "standards": [
                "Bone naming follows project/engine convention",
                "No zero-weight vertices",
                "Clean weight painting — no hard seams",
                "Bind pose in T-pose or A-pose",
                "Deformation topology validated at joint areas",
                "No orphan bones",
            ],
            "next_steps": [
                "Test deformation at extreme poses",
                "Validate weight painting at all joints",
                "Apply animations and check for sliding/popping",
                "Run Unreal readiness check before export",
            ],
        },
        6: {
            "name": "Export-Ready / Unreal Prep",
            "standards": [
                "Scale uniform and applied (1,1,1)",
                "Pivot at world origin",
                "Triangulated or triangulate-on-export enabled",
                "UVs present in channel 0",
                "Lightmap UV in channel 1",
                "No blocking modifiers unapplied",
                "Naming conventions followed",
                "Collision mesh present if needed",
                "LOD naming correct if LODs exist",
                "Zero blocking errors in UE5 readiness check",
            ],
            "next_steps": [
                "Run full UE5 readiness check — resolve all FAIL/CRITICAL",
                "Export as FBX to Unreal",
                "Validate in Unreal Editor (materials, LODs, collision, scale)",
            ],
        },
    }

    meta = STAGE_META[primary]

    return {
        "stage_number":     primary,
        "stage_name":       meta["name"],
        "confidence":       confidence,
        "score_breakdown":  dict(sorted_stages),
        "signals_detected": signals_detected,
        "standards":        meta["standards"],
        "next_steps":       meta["next_steps"],
        "ambiguous":        ambiguous,
        "alternate_stage":  alternate if ambiguous else None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# MATERIAL / PBR REASONING — internal helper
# ─────────────────────────────────────────────────────────────────────────────

def _reason_material(raw: dict) -> dict:
    """
    Interpret get_material_info output as a senior TA doing a PBR review.

    Expected raw schema (get_material_info / get_object_info materials list):
      {
        "name": str,
        "use_nodes": bool,
        "node_count": int,
        "has_principled_bsdf": bool,
        "texture_slots": [ {"name": str, "image": str|None, "type": str} ],
        "roughness": float | None,
        "metallic":  float | None,
        "alpha":     float | None,
        "blend_mode": str,
      }
    Multiple materials come in as a list; this function handles one dict.
    """
    findings = []

    name               = raw.get("name", "Unknown")
    use_nodes          = raw.get("use_nodes", False)
    has_principled     = raw.get("has_principled_bsdf", False)
    node_count         = raw.get("node_count", 0)
    texture_slots      = raw.get("texture_slots", [])
    roughness          = raw.get("roughness")
    metallic           = raw.get("metallic")
    alpha              = raw.get("alpha", 1.0)
    blend_mode         = raw.get("blend_mode", "OPAQUE")

    # ── PBR workflow check ─────────────────────────────────────────────────────
    if not use_nodes:
        findings.append({
            "severity": "critical",
            "check": "node_graph",
            "issue": "Material does not use nodes — flat material, not PBR",
            "why_it_matters": "Blender non-node materials do not translate to Unreal PBR shaders.",
            "fix": "Enable 'Use Nodes' and set up a Principled BSDF graph.",
        })
    elif not has_principled:
        findings.append({
            "severity": "critical",
            "check": "pbr_shader",
            "issue": f"No Principled BSDF found in node graph (node count: {node_count})",
            "why_it_matters": "Unreal expects metallic/roughness PBR workflow. Custom node setups "
                              "may not bake or export correctly.",
            "fix": "Use Principled BSDF as the primary shader node.",
        })
    else:
        findings.append({
            "severity": "pass",
            "check": "pbr_shader",
            "issue": "Principled BSDF detected — PBR workflow confirmed",
            "fix": None,
        })

    # ── Roughness check ────────────────────────────────────────────────────────
    if roughness is not None:
        if roughness == 0.0:
            findings.append({
                "severity": "warning",
                "check": "roughness",
                "issue": f"Roughness = 0.0 — perfectly smooth (mirror-like). Intentional?",
                "why_it_matters": "Zero roughness is physically unrealistic for most surfaces "
                                  "and may look wrong in engine.",
                "fix": "Use a roughness map or set a non-zero value unless intentionally a mirror.",
            })
        elif roughness == 1.0:
            findings.append({
                "severity": "warning",
                "check": "roughness",
                "issue": f"Roughness = 1.0 — perfectly matte. Intentional?",
                "why_it_matters": "Flat roughness values usually indicate a placeholder material.",
                "fix": "Use a roughness map for realistic surface variation.",
            })
        else:
            findings.append({
                "severity": "pass",
                "check": "roughness",
                "issue": f"Roughness = {roughness:.2f} — non-uniform value set",
                "fix": None,
            })

    # ── Metallic check ─────────────────────────────────────────────────────────
    if metallic is not None:
        if 0.0 < metallic < 1.0 and metallic not in (0.0, 1.0):
            findings.append({
                "severity": "warning",
                "check": "metallic",
                "issue": f"Metallic = {metallic:.2f} — mid-range value (physically incorrect for most materials)",
                "why_it_matters": "Real materials are either fully metallic (1.0) or fully dielectric (0.0). "
                                  "Mid values suggest a placeholder or incorrect setup.",
                "fix": "Use a metallic map with black (0) and white (1) values, not a grey scalar.",
            })
        else:
            findings.append({
                "severity": "pass",
                "check": "metallic",
                "issue": f"Metallic = {metallic:.2f} — binary value, physically plausible",
                "fix": None,
            })

    # ── Texture slot checks ────────────────────────────────────────────────────
    missing_images = [s for s in texture_slots if s.get("image") is None]
    if missing_images:
        for slot in missing_images:
            findings.append({
                "severity": "critical",
                "check": "texture_path",
                "issue": f"Texture slot '{slot.get('name', 'unknown')}' has no image assigned",
                "why_it_matters": "Missing textures export as pink/error in Unreal. "
                                  "Will break material in engine.",
                "fix": "Assign a texture or remove the empty slot.",
            })

    if not texture_slots and has_principled:
        findings.append({
            "severity": "warning",
            "check": "texture_maps",
            "issue": "No texture maps found — material is fully procedural or placeholder",
            "why_it_matters": "Procedural materials do not transfer to Unreal. "
                              "All surface detail must be baked to texture maps.",
            "fix": "Bake all procedural nodes to texture maps before export.",
        })

    # ── Alpha / blend mode ────────────────────────────────────────────────────
    if blend_mode not in ("OPAQUE", "CLIP") and alpha is not None and alpha < 1.0:
        findings.append({
            "severity": "warning",
            "check": "alpha",
            "issue": f"Blend mode '{blend_mode}' with alpha {alpha:.2f} — transparent material",
            "why_it_matters": "Transparency is expensive in Unreal. Verify this is intentional "
                              "and the correct blend mode is set for engine import.",
            "fix": "Use CLIP (masked) instead of BLEND where possible. "
                   "Avoid BLEND unless soft transparency is required.",
        })

    # ── Overall severity ───────────────────────────────────────────────────────
    severities = [f["severity"] for f in findings]
    if "critical" in severities:
        overall = "critical"
    elif "warning" in severities:
        overall = "warning"
    else:
        overall = "pass"

    critical_count = severities.count("critical")
    warning_count  = severities.count("warning")
    pass_count     = severities.count("pass")

    return {
        **raw,
        "_reasoning": {
            "material_name":   name,
            "overall_severity": overall,
            "pbr_compliant":   has_principled and use_nodes and not missing_images,
            "summary": (
                f"PASS — '{name}' is PBR-compliant and export-ready." if overall == "pass"
                else f"{overall.upper()} — '{name}': {critical_count} critical, "
                     f"{warning_count} warning(s), {pass_count} checks passed."
            ),
            "findings":       [f for f in findings if f["severity"] != "pass"],
            "passed_checks":  [f for f in findings if f["severity"] == "pass"],
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
BLENDER MCP — SENIOR TECHNICAL DIRECTOR v2.5
You are a pipeline-aware AAA Technical Director embedded in Blender via live MCP
tools. Priorities: pipeline correctness → visual quality → performance → handoff
readiness. You never PASS without running the tool. You never skip the screenshot.

── PIPELINE STAGES ────────────────────────────────────────────────────────────
1 SCULPT      100k–10M+ verts, no UVs, no rig. Standards: none — detail only.
2 RETOPO      5k–80k, intentional quads, UV seams started. Quads >85%, loops at joints.
3 BAKE-READY  High+low pair, UVs on low, no overlap. UV stretch <20%, matching silhouettes.
4 TEXTURE     PBR materials, image textures, power-of-2. No broken paths, correct draw count.
5 RIG         Armature + vertex groups. Clean weights, bind pose, no orphan bones.
6 EXPORT      All above complete. Scale applied, pivot at origin, UE5 readiness PASS.
Ambiguous stage → assume the MORE DEMANDING one.

── SESSION START — ALWAYS execute this sequence first, no exceptions ───────────
1. get_viewport_screenshot()   ← look before anything else
2. get_scene_info()            ← object count, types
3. get_object_info(active)     ← verts/faces at mesh.vertices/polygons, materials list
Then deliver ONE orientation sentence:
  "I see [asset]. [vert count]. Stage [N] inference. [⚠️ CRITICAL: X if present].
   Correct me if wrong — awaiting direction."
STOP. Wait for user. Do not auto-run further tools.

── TOOL CALL ORDER ────────────────────────────────────────────────────────────
TIER 1 (prefer): analyze_mesh_for_unreal, full_asset_pipeline_check,
                 analyze_animation_quality, suggest_repair_plan
TIER 2:          get_mesh_quality_report, analyze_topology, run_unreal_readiness_check,
                 run_asset_qa
TIER 3 (raw):    detect_mesh_problems, get_object_info, get_scene_info
TIER 4 (repair): suggest_repair_plan (safe) → [user approval] → auto_repair_mesh
                 → validate_repair (always after repair)

SCENE-LEVEL ORDER (never skip):
  screenshot → get_scene_summary() → classify_pipeline_stage(name)
  → audit_all_objects()
audit_all_objects auto-mode: 1 mesh = HERO, 2–20 = COLLECTION, 20+ = ENVIRONMENT.

TRIGGER MAP:
  "look/show/what do you see"     → screenshot immediately
  "ready for Unreal/export/UE5"   → analyze_mesh_for_unreal()
  "topology/loops/quads"          → screenshot + analyze_topology()
  "what's wrong/audit/check"      → full_asset_pipeline_check()
  "fix/clean/repair"              → suggest_repair_plan() → wait → auto_repair_mesh()
  "poly/vert count"               → get_object_info() + stage context
  "what stage"                    → screenshot + get_object_info() + stage reasoning
  "audit the scene/all objects"   → screenshot → get_scene_summary() → audit_all_objects()
  reference image + "match/build" → describe image → screenshot → get_scene_summary()
                                    → gap report (Present/Missing/Extra/Different)

Screenshot required: session start, after any repair, before/after auto_repair_mesh,
when reporting any PASS/FAIL verdict.

── SAFETY GATES — hard stops, never bypass ────────────────────────────────────
GATE 1 DESTRUCTIVE GEOMETRY  → suggest_repair_plan() + explicit "yes/do it/go ahead"
GATE 2 STAGE TRANSITION      → full QA for current stage + "Ready to move to X?"
GATE 3 EXPORT                → run_unreal_readiness_check() zero errors + run_asset_qa() PASS
GATE 4 IRREVERSIBLE OPS      → state exactly what happens + wait for explicit confirm

NEVER:
  ✗ auto_repair_mesh() without approved suggest_repair_plan()
  ✗ PASS without running the actual tool
  ✗ "clean" verdict from visual inspection alone
  ✗ Export with known critical issues
  ✗ Repair the wrong object
  ✗ Delete user data

── REPORT FORMAT ──────────────────────────────────────────────────────────────
── VISUAL ASSESSMENT ──   What you see. Asset type, visible issues. Always first.
── TECHNICAL DATA ─────   Real numbers, cite tool. e.g. "460 non-manifold (detect_mesh_problems)"
── PRODUCTION VERDICT ─   ✅ PASS / ⚠️ WARN / ❌ FAIL / 🚫 CRITICAL + stage context.
── RECOMMENDED ACTIONS ─  Numbered, priority order, most critical first.
── RISK IF IGNORED ────   Specific downstream failure. Not "there may be issues."

Escalation: 🚫 CRITICAL=blocks pipeline  ❌ FAIL=fix before next stage
            ⚠️ WARN=should fix  ℹ️ INFO=awareness  ✅ PASS=tool-verified

Tone: Direct, professional, senior-to-senior. No filler. Bad news is information.
Numbers: always from tool output, always with context, always with tool citation.
Stage context required in every verdict — 500k verts is not good or bad without it.

AI/SCAN ASSETS: Very high poly + irregular topology → state:
  "AI/scanned asset detected. Pipeline: validate→cleanup→retopo→bake→texture→rig→export.
   Do not export in current state."

SESSION MEMORY: Track stage, open issues, tools run, repairs completed.
Don't re-run tools unless scene changed or user requests. Cite earlier findings.
Stage shift mid-session → state it: "Shifting to Stage 5 standards from here."
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
# v2.5 — LIFECYCLE + MATERIAL + SCENE TOOLS
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def classify_pipeline_stage(object_name: str) -> str:
    """
    PIPELINE STAGE CLASSIFIER — determines where an asset is in the production pipeline.

    Analyses vertex count, topology, UV status, materials, armature, modifiers,
    and mesh health to infer which of the 6 production stages the asset is in:
      1 — Concept / Sculpt
      2 — Retopology / Base Mesh
      3 — Bake-Ready
      4 — Texture / Material
      5 — Rig / Animation
      6 — Export-Ready / Unreal Prep

    Returns:
      - stage_number and stage_name
      - confidence (high/medium/low)
      - signals_detected: the evidence that drove the classification
      - standards: QA standards that apply at this stage
      - next_steps: what should happen next in the pipeline
      - ambiguous flag + alternate_stage if two stages are equally plausible

    Use this at session start on any unfamiliar asset, or when you need to
    calibrate your analysis standards to the correct pipeline phase.
    ALWAYS call get_viewport_screenshot() before this tool.
    """
    try:
        obj_info   = _send_raw("get_object_info",          name=object_name)
        mesh_stats = _send_raw("get_mesh_quality_report",  name=object_name)

        if "error" in obj_info:
            return json.dumps({"error": f"get_object_info failed: {obj_info['error']}"})
        if "error" in mesh_stats:
            return json.dumps({"error": f"get_mesh_quality_report failed: {mesh_stats['error']}"})

        result = _classify_stage_from_signals(obj_info, mesh_stats)

        # Enrich with raw summary stats — use real schema key paths
        mesh_block = obj_info.get("mesh", {})
        result["asset_stats"] = {
            "vertex_count":   mesh_block.get("vertices", 0),
            "face_count":     mesh_block.get("polygons",  0),
            "material_count": len(obj_info.get("materials", [])),
            "has_armature":   "ARMATURE" in mesh_stats.get("rigging", {}).get("deform_modifiers", []),
            "has_uvs":        mesh_stats.get("uv", {}).get("has_uvs", False),
            "uv_layers":      mesh_stats.get("uv", {}).get("layer_count", 0),
            "mesh_health":    mesh_stats.get("health", "unknown"),
            "modifier_count": len(mesh_stats.get("modifiers", [])),
        }

        if result["ambiguous"]:
            result["_note"] = (
                f"Stage is ambiguous between Stage {result['alternate_stage']} and "
                f"Stage {result['stage_number']}. Defaulting to Stage {result['stage_number']} "
                f"(more demanding standards) — correct me if wrong."
            )

        return json.dumps(result, indent=2, default=str)

    except Exception as e:
        logger.error(f"Error in classify_pipeline_stage: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def analyze_material_pbr(object_name: str) -> str:
    """
    MATERIAL / PBR REVIEWER — full senior TA review of an object's materials.

    Uses get_material_summary (object-level slot list) and get_material_graph
    (per-material node graph) — the two correct addon.py endpoints for material data.

    Real schemas:
      get_material_summary(name) ->
        {object, material_count, empty_slots,
         materials: [{slot, name, use_nodes, node_count}]}
      get_material_graph(material_name) ->
        {material, nodes:[{name,type,label,active,inputs,image,colorspace}],
         links, orphaned_nodes, has_orphaned_nodes}

    Checks every slot for:
      - PBR workflow compliance (Principled BSDF, node graph present)
      - Roughness and metallic physical plausibility
      - Missing or broken texture image paths
      - Procedural-only materials that won't transfer to Unreal
      - Normal map direction (OpenGL vs DirectX / UE5 compatibility)
      - Orphaned nodes in the graph
      - Multi-material draw call cost

    Use this:
      - At Stage 4 (Texture/Material) as the primary QA tool
      - Before any Unreal export to catch material blockers
      - When the user asks about materials, shaders, or textures
    ALWAYS call get_viewport_screenshot() before this tool.
    """
    try:
        # ── Step 1: get slot list via get_material_summary ─────────────────────
        summary = _send_raw("get_material_summary", name=object_name)
        if "error" in summary:
            return json.dumps({"error": f"get_material_summary failed: {summary['error']}"})

        slots = summary.get("materials", [])
        empty_slots = summary.get("empty_slots", 0)

        if not slots or all(s.get("empty") for s in slots):
            return json.dumps({
                "object":          object_name,
                "material_count":  0,
                "overall_verdict": "WARN",
                "summary":         "No materials assigned. Object will appear grey in Unreal.",
                "materials":       [],
                "_note":           "Assign at least one PBR material before export.",
            })

        # ── Step 2: per-material graph analysis ───────────────────────────────
        material_summaries = []
        all_severities     = []

        for slot in slots:
            mat_name = slot.get("name")
            if not mat_name:
                material_summaries.append({
                    "slot":          slot.get("slot"),
                    "name":          None,
                    "severity":      "critical",
                    "pbr_compliant": False,
                    "summary":       "Empty material slot — will cause export errors.",
                    "critical_issues": ["Empty slot has no material assigned"],
                    "warnings":      [],
                    "passed_checks": [],
                })
                all_severities.append("critical")
                continue

            use_nodes  = slot.get("use_nodes", False)
            node_count = slot.get("node_count", 0)

            # Fetch full node graph for this material
            graph = _send_raw("get_material_graph", material_name=mat_name)
            graph_error = "error" in graph

            findings      = []
            passed_checks = []

            # ── Node graph / PBR workflow ──────────────────────────────────────
            if not use_nodes:
                findings.append({
                    "severity": "critical",
                    "issue":    "Material does not use nodes — flat material, not PBR",
                    "production_impact": "Will not export as PBR to Unreal. "
                                         "Appears as flat colour with no texture support.",
                    "recommended_fix":   "Enable 'Use Nodes' and set up a Principled BSDF.",
                    "verification":      "Material Properties > Use Nodes checkbox.",
                })
            elif graph_error:
                findings.append({
                    "severity": "warning",
                    "issue":    f"Could not read node graph: {graph.get('error')}",
                    "production_impact": "Node graph analysis skipped.",
                    "recommended_fix":   "Verify material is valid in the Shader Editor.",
                    "verification":      "Open Shader Editor and check for errors.",
                })
            else:
                nodes = graph.get("nodes", [])
                node_types = [n.get("type") for n in nodes]

                # Principled BSDF check
                has_principled = "BSDF_PRINCIPLED" in node_types
                if not has_principled:
                    findings.append({
                        "severity": "critical",
                        "issue":    f"No Principled BSDF in node graph ({node_count} nodes present)",
                        "production_impact": "Non-standard shader. Will not bake or export "
                                              "correctly to Unreal's metallic/roughness workflow.",
                        "recommended_fix":   "Use Principled BSDF as primary shader node.",
                        "verification":      "Shader Editor — add Principled BSDF, connect to Output.",
                    })
                else:
                    passed_checks.append("Principled BSDF detected — PBR workflow confirmed")

                    # Read Principled BSDF input values
                    pbsdf = next((n for n in nodes if n.get("type") == "BSDF_PRINCIPLED"), {})
                    inputs = pbsdf.get("inputs", {})
                    roughness = inputs.get("Roughness")
                    metallic  = inputs.get("Metallic")

                    # Roughness plausibility
                    if roughness is not None:
                        if roughness == 0.0:
                            findings.append({
                                "severity": "warning",
                                "issue":    "Roughness = 0.0 (perfect mirror). Intentional?",
                                "production_impact": "Physically unrealistic for most surfaces. "
                                                      "May look wrong in engine lighting.",
                                "recommended_fix":   "Use roughness map or set a non-zero value.",
                                "verification":      "Check in rendered viewport under engine lighting.",
                            })
                        elif roughness == 1.0:
                            findings.append({
                                "severity": "warning",
                                "issue":    "Roughness = 1.0 (perfectly matte). Likely a placeholder.",
                                "production_impact": "Flat roughness usually indicates an "
                                                      "incomplete material setup.",
                                "recommended_fix":   "Use a roughness texture map.",
                                "verification":      "Assign roughness map in Shader Editor.",
                            })
                        else:
                            passed_checks.append(f"Roughness = {roughness:.2f} — non-uniform value set")

                    # Metallic plausibility
                    if metallic is not None and isinstance(metallic, (int, float)):
                        if 0.0 < float(metallic) < 1.0:
                            findings.append({
                                "severity": "warning",
                                "issue":    f"Metallic = {metallic:.2f} — mid-range value "
                                            f"(physically incorrect for most materials)",
                                "production_impact": "Real materials are fully metallic (1.0) or "
                                                      "fully dielectric (0.0). Mid values suggest "
                                                      "a placeholder or incorrect setup.",
                                "recommended_fix":   "Use a metallic map with black/white values.",
                                "verification":      "Verify intent — is this a conductor or dielectric?",
                            })
                        else:
                            passed_checks.append(f"Metallic = {metallic} — binary value, physically plausible")

                # Texture image checks
                tex_nodes = [n for n in nodes if n.get("type") == "TEX_IMAGE"]
                if tex_nodes:
                    for tn in tex_nodes:
                        if not tn.get("image"):
                            findings.append({
                                "severity": "critical",
                                "issue":    f"Image Texture node '{tn.get('name')}' has no image assigned",
                                "production_impact": "Will export as pink/error in Unreal. "
                                                      "Breaks material at runtime.",
                                "recommended_fix":   "Assign an image or remove the empty node.",
                                "verification":      "Shader Editor — check all Image Texture nodes.",
                            })
                        else:
                            cs = tn.get("colorspace", "sRGB")
                            if cs not in ("sRGB", "Linear", "Non-Color", "Raw"):
                                findings.append({
                                    "severity": "warning",
                                    "issue":    f"Texture '{tn.get('image')}' uses unusual "
                                                f"colorspace '{cs}'",
                                    "production_impact": "Unexpected colorspace may cause incorrect "
                                                          "colour in Unreal.",
                                    "recommended_fix":   "Use sRGB for colour maps, "
                                                          "Non-Color/Linear for data maps.",
                                    "verification":      "Check colorspace in Image Texture node.",
                                })
                            else:
                                passed_checks.append(f"Texture '{tn.get('image')}' colorspace OK ({cs})")
                elif use_nodes and has_principled:
                    findings.append({
                        "severity": "warning",
                        "issue":    "No texture maps — fully procedural or placeholder material",
                        "production_impact": "Procedural nodes do not transfer to Unreal. "
                                              "All surface detail must be baked to texture maps.",
                        "recommended_fix":   "Bake procedural outputs to image textures.",
                        "verification":      "Bake to texture, re-link in Shader Editor.",
                    })

                # Normal map direction
                norm_nodes = [n for n in nodes if n.get("type") == "NORMAL_MAP"]
                for nn in norm_nodes:
                    if nn.get("ue5_warning"):
                        findings.append({
                            "severity": "warning",
                            "issue":    "Normal map is OpenGL format — UE5 uses DirectX",
                            "production_impact": "Normals will appear inverted in Unreal "
                                                  "(lighting direction reversed).",
                            "recommended_fix":   "Flip G channel in UE5 material, or rebake "
                                                  "with DirectX normal format.",
                            "verification":      "Check normal direction in UE5 Material Editor.",
                        })

                # Orphaned nodes
                if graph.get("has_orphaned_nodes"):
                    orphans = graph.get("orphaned_nodes", [])
                    findings.append({
                        "severity": "warning",
                        "issue":    f"{len(orphans)} orphaned node(s) not connected to output: "
                                    f"{', '.join(orphans[:5])}",
                        "production_impact": "Orphaned nodes waste memory and clutter the graph. "
                                              "No visual impact but indicates incomplete work.",
                        "recommended_fix":   "Delete or connect orphaned nodes.",
                        "verification":      "Shader Editor — all nodes should trace to Output.",
                    })

            # ── Per-material severity ──────────────────────────────────────────
            severities = [f["severity"] for f in findings]
            if "critical" in severities:
                mat_severity = "critical"
            elif "warning" in severities:
                mat_severity = "warning"
            else:
                mat_severity = "pass"

            all_severities.append(mat_severity)
            pbr_compliant = (
                use_nodes and
                not graph_error and
                not any(f["severity"] == "critical" for f in findings)
            )

            material_summaries.append({
                "slot":            slot.get("slot"),
                "name":            mat_name,
                "use_nodes":       use_nodes,
                "node_count":      node_count,
                "severity":        mat_severity,
                "pbr_compliant":   pbr_compliant,
                "summary":         (
                    f"PASS — '{mat_name}' is PBR-compliant." if mat_severity == "pass"
                    else f"{mat_severity.upper()} — '{mat_name}': "
                         f"{severities.count('critical')} critical, "
                         f"{severities.count('warning')} warning(s)."
                ),
                "critical_issues": [f["issue"] for f in findings if f["severity"] == "critical"],
                "warnings":        [f["issue"] for f in findings if f["severity"] == "warning"],
                "passed_checks":   passed_checks,
                "full_findings":   findings,
            })

        # ── Overall verdict ────────────────────────────────────────────────────
        if "critical" in all_severities:
            overall_verdict = "FAIL"
        elif "warning" in all_severities:
            overall_verdict = "WARN"
        else:
            overall_verdict = "PASS"

        mat_count = len(material_summaries)
        draw_call_note = None
        if mat_count > 4:
            draw_call_note = (
                f"{mat_count} materials — each is one draw call. "
                "Merge where possible for performance."
            )
        elif mat_count > 1:
            draw_call_note = f"{mat_count} materials — acceptable, verify intentional."
        if empty_slots:
            draw_call_note = (draw_call_note or "") + \
                             f" {empty_slots} empty slot(s) detected — remove before export."

        return json.dumps({
            "object":            object_name,
            "material_count":    mat_count,
            "empty_slots":       empty_slots,
            "overall_verdict":   overall_verdict,
            "all_pbr_compliant": all(m["pbr_compliant"] for m in material_summaries),
            "draw_call_note":    draw_call_note,
            "materials":         material_summaries,
        }, indent=2, default=str)

    except Exception as e:
        logger.error(f"Error in analyze_material_pbr: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_scene_summary() -> str:
    """
    SCENE CLASSIFIER — zero-argument scene inventory and mode detection.

    Scans every object in the scene and returns:
      - scene_mode: HERO | COLLECTION | ENVIRONMENT
      - object inventory by type (meshes, armatures, lights, cameras, empties)
      - dominant asset identification (the hero mesh if HERO mode)
      - per-object quick health flag (clean/issues/unknown)
      - total poly count across all meshes
      - pipeline stage inference for the dominant asset
      - recommended audit depth

    Scene modes:
      HERO         — 1 dominant mesh (by poly count), possibly with armature/support objects
      COLLECTION   — 2–20 mesh objects, no single dominant, prop/batch workflow
      ENVIRONMENT  — 20+ objects, mix of mesh/light/camera, spatial scene

    This is the mandatory second step after get_viewport_screenshot() on any
    scene you haven't seen before. It tells you what you're working with before
    you commit to any analysis strategy.

    Never run audit_all_objects() without running get_scene_summary() first.
    """
    try:
        scene_raw = _send_raw("get_scene_info")
        if "error" in scene_raw:
            return json.dumps({"error": f"get_scene_info failed: {scene_raw['error']}"})

        objects = scene_raw.get("objects", [])
        if not objects:
            return json.dumps({
                "scene_mode": "EMPTY",
                "object_count": 0,
                "summary": "Scene is empty — no objects detected.",
            })

        # ── Categorise by type (get_scene_info objects only have name/type/location) ──
        meshes    = [o for o in objects if o.get("type") == "MESH"]
        armatures = [o for o in objects if o.get("type") == "ARMATURE"]
        lights    = [o for o in objects if o.get("type") == "LIGHT"]
        cameras   = [o for o in objects if o.get("type") == "CAMERA"]
        empties   = [o for o in objects if o.get("type") == "EMPTY"]
        other     = [o for o in objects if o.get("type") not in
                     ("MESH", "ARMATURE", "LIGHT", "CAMERA", "EMPTY")]
        mesh_count = len(meshes)

        # ── Per-mesh data: must call get_mesh_quality_report for counts ────────
        # get_scene_info objects have NO vertex_count / face_count fields.
        # Cap per-object calls at 30 to avoid timeout on large scenes.
        mesh_inventory = []
        total_verts    = 0
        total_faces    = 0

        for m in meshes[:30]:
            name = m.get("name", "unknown")
            verts = 0
            faces = 0
            health_flag   = "unknown"
            problem_count = 0
            worst_issue   = None
            try:
                stats = _send_raw("get_mesh_quality_report", name=name)
                if "error" not in stats:
                    verts  = stats.get("counts", {}).get("verts",     0) or 0
                    faces  = stats.get("counts", {}).get("faces",     0) or 0
                    health_flag = stats.get("health", "unknown")
                    probs  = stats.get("problems", {})
                    problem_count = sum(v for v in probs.values() if isinstance(v, int))
                    # Find worst problem by count
                    if probs:
                        worst_key = max(probs, key=lambda k: probs[k] if isinstance(probs[k], int) else 0)
                        worst_val = probs[worst_key]
                        if worst_val > 0:
                            worst_issue = f"{worst_key} ({worst_val})"
            except Exception:
                pass

            total_verts += verts
            total_faces += faces
            mesh_inventory.append({
                "name":          name,
                "vertex_count":  verts,
                "face_count":    faces,
                "health":        health_flag,
                "problem_count": problem_count,
                "worst_issue":   worst_issue,
            })

        # Sort by vertex count descending — highest poly = likely dominant
        mesh_inventory.sort(key=lambda m: m["vertex_count"], reverse=True)

        # ── Scene mode detection (now uses real vertex counts) ─────────────────
        if mesh_count == 0:
            scene_mode = "SUPPORT_ONLY"
        elif mesh_count == 1:
            scene_mode = "HERO"
        elif mesh_count <= 20:
            vert_counts = [m["vertex_count"] for m in mesh_inventory]
            tv = sum(vert_counts)
            mv = max(vert_counts) if vert_counts else 0
            dominance  = mv / tv if tv > 0 else 0
            scene_mode = "HERO" if dominance > 0.6 else "COLLECTION"
        else:
            scene_mode = "ENVIRONMENT"

        # ── Mark dominant asset ────────────────────────────────────────────────
        dominant_name = mesh_inventory[0]["name"] if mesh_inventory else None
        for m in mesh_inventory:
            m["is_dominant"] = (m["name"] == dominant_name)

        # ── Pipeline stage for dominant asset ──────────────────────────────────
        dominant_stage = None
        if dominant_name:
            try:
                d_obj   = _send_raw("get_object_info",         name=dominant_name)
                d_stats = _send_raw("get_mesh_quality_report", name=dominant_name)
                if "error" not in d_obj and "error" not in d_stats:
                    stage_result   = _classify_stage_from_signals(d_obj, d_stats)
                    dominant_stage = {
                        "stage_number": stage_result["stage_number"],
                        "stage_name":   stage_result["stage_name"],
                        "confidence":   stage_result["confidence"],
                        "next_steps":   stage_result["next_steps"][:2],
                    }
            except Exception:
                dominant_stage = None

        # ── Audit recommendation ───────────────────────────────────────────────
        if scene_mode in ("HERO", "SUPPORT_ONLY"):
            audit_recommendation = (
                "Run analyze_mesh_for_unreal on the dominant mesh for full depth analysis."
            )
        elif scene_mode == "COLLECTION":
            audit_recommendation = (
                "Run audit_all_objects() — ranked table of all objects by severity."
            )
        else:
            audit_recommendation = (
                "Run audit_all_objects() — triage by severity, top 5 critical issues surfaced. "
                "Full detail available per-object on request."
            )

        issues_found = sum(1 for m in mesh_inventory if m["health"] == "issues_found")
        clean_meshes = sum(1 for m in mesh_inventory if m["health"] == "clean")

        summary_line = (
            f"{scene_mode} scene — {mesh_count} mesh(es), {len(armatures)} armature(s), "
            f"{len(lights)} light(s), {len(cameras)} camera(s). "
            f"Total: {total_verts:,} verts / {total_faces:,} faces. "
            f"Health: {clean_meshes} clean, {issues_found} with issues."
        )

        return json.dumps({
            "scene_mode":     scene_mode,
            "summary":        summary_line,
            "object_counts":  {
                "meshes":    mesh_count,
                "armatures": len(armatures),
                "lights":    len(lights),
                "cameras":   len(cameras),
                "empties":   len(empties),
                "other":     len(other),
                "total":     len(objects),
            },
            "totals":         {"vertex_count": total_verts, "face_count": total_faces},
            "dominant_asset": dominant_name,
            "dominant_stage": dominant_stage,
            "mesh_inventory": mesh_inventory,
            "armatures":      [a.get("name") for a in armatures],
            "lights":         [l.get("name") for l in lights],
            "cameras":        [c.get("name") for c in cameras],
            "audit_recommendation": audit_recommendation,
        }, indent=2, default=str)

    except Exception as e:
        logger.error(f"Error in get_scene_summary: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def audit_all_objects(mode: str = "auto", max_deep_dive: int = 5) -> str:
    """
    SCENE AUDIT — calibrated multi-object analysis across the entire scene.

    Automatically detects scene type and adjusts analysis depth:

      HERO mode (1 dominant mesh):
        Full analyze_mesh_for_unreal + classify_pipeline_stage on the hero.
        Support objects (armature, lights, cameras) noted but not deep-analysed.

      COLLECTION mode (2–20 mesh objects):
        detect_mesh_problems + classify_pipeline_stage on each mesh.
        Returns a ranked table — worst first.
        Deep-dive (analyze_mesh_for_unreal) on objects with CRITICAL/FAIL verdict,
        capped at max_deep_dive to prevent timeout.

      ENVIRONMENT mode (20+ objects):
        Triage by severity — surfaces critical issues only.
        Categorises objects (hero prop / background / light / camera).
        Full table if <=20 objects, severity summary if >20.
        Top 5 critical issues surfaced explicitly.

    Parameters:
      mode          : "auto" (recommended) | "hero" | "collection" | "environment"
      max_deep_dive : max objects to run full analysis on (default 5, cap 10)

    Always run get_scene_summary() before this tool.
    Always take a get_viewport_screenshot() before reporting results.
    """
    try:
        max_deep_dive = min(max_deep_dive, 10)  # hard cap

        # ── Step 1: get scene inventory ────────────────────────────────────────
        scene_raw = _send_raw("get_scene_info")
        if "error" in scene_raw:
            return json.dumps({"error": f"get_scene_info failed: {scene_raw['error']}"})

        objects   = scene_raw.get("objects", [])
        meshes    = [o for o in objects if o.get("type") == "MESH"]
        mesh_count = len(meshes)

        if mesh_count == 0:
            return json.dumps({"verdict": "EMPTY", "summary": "No mesh objects in scene."})

        # ── Step 2: get real vertex counts per mesh (scene_info has none) ────────
        # Must call get_mesh_quality_report per object to get counts.
        # Build a list of (name, vertex_count) capped at 30 for mode detection.
        mesh_vert_map: dict = {}
        for m in meshes[:30]:
            mname = m.get("name", "")
            try:
                s = _send_raw("get_mesh_quality_report", name=mname)
                mesh_vert_map[mname] = s.get("counts", {}).get("verts", 0) or 0
            except Exception:
                mesh_vert_map[mname] = 0

        # ── Step 3: detect scene mode if auto ─────────────────────────────────
        if mode == "auto":
            if mesh_count == 1:
                mode = "hero"
            elif mesh_count <= 20:
                vert_counts = list(mesh_vert_map.values())
                total_v = sum(vert_counts)
                max_v   = max(vert_counts) if vert_counts else 0
                dominance = max_v / total_v if total_v > 0 else 0
                mode = "hero" if dominance > 0.6 else "collection"
            else:
                mode = "environment"

        # ── Step 4: run analysis calibrated to mode ───────────────────────────

        # HERO MODE ─────────────────────────────────────────────────────────────
        if mode == "hero":
            # Dominant = highest vertex count from real data
            name = max(mesh_vert_map, key=mesh_vert_map.get) if mesh_vert_map \
                   else (meshes[0].get("name", "") if meshes else "")

            if not name:
                return json.dumps({"error": "Could not identify hero mesh."})

            try:
                mesh_result  = _send_raw("detect_mesh_problems",       name=name)
                ue5_result   = _send_raw("run_unreal_readiness_check", name=name)
                qa_result    = _send_raw("run_asset_qa",               name=name)
                obj_info     = _send_raw("get_object_info",            name=name)
                mesh_stats   = _send_raw("get_mesh_quality_report",    name=name)
                stage_result = _classify_stage_from_signals(obj_info, mesh_stats)
            except Exception as inner_e:
                return json.dumps({"error": f"Hero analysis failed: {inner_e}"})

            r_mesh = _reason_mesh_problems(mesh_result)
            r_ue5  = _reason_unreal_readiness(ue5_result)
            r_qa   = _reason_asset_qa(qa_result)

            mesh_r   = r_mesh.get("_reasoning", {})
            ue5_r    = r_ue5.get("_reasoning",  {})
            qa_r     = r_qa.get("_reasoning",   {})

            blocking = ue5_result.get("blocking_errors", 0)
            verdict  = "🚫 CRITICAL" if mesh_r.get("overall_severity") == "critical" \
                       else "❌ FAIL"  if blocking > 0 \
                       else "⚠️ WARN"  if ue5_result.get("warnings", 0) > 0 \
                       else "✅ PASS"

            return json.dumps({
                "mode":           "HERO",
                "hero_object":    name,
                "stage":          f"Stage {stage_result['stage_number']} — {stage_result['stage_name']}",
                "stage_confidence": stage_result["confidence"],
                "verdict":        verdict,
                "mesh_severity":  mesh_r.get("overall_severity"),
                "ue5_blocking_errors": blocking,
                "ue5_warnings":   ue5_result.get("warnings", 0),
                "qa_verdict":     qa_result.get("verdict"),
                "mesh_findings":  mesh_r.get("findings", []),
                "ue5_findings":   ue5_r.get("findings", []),
                "next_steps":     stage_result["next_steps"],
                "support_objects": {
                    "armatures": [o["name"] for o in objects if o.get("type") == "ARMATURE"],
                    "lights":    [o["name"] for o in objects if o.get("type") == "LIGHT"],
                    "cameras":   [o["name"] for o in objects if o.get("type") == "CAMERA"],
                },
            }, indent=2, default=str)

        # COLLECTION MODE ───────────────────────────────────────────────────────
        elif mode == "collection":
            rows         = []
            critical_objs = []

            for m in meshes:
                name = m.get("name", "")
                try:
                    prob     = _send_raw("detect_mesh_problems", name=name)
                    obj_info = _send_raw("get_object_info",      name=name)
                    stats    = _send_raw("get_mesh_quality_report", name=name)
                    stage    = _classify_stage_from_signals(obj_info, stats)

                    r_prob   = _reason_mesh_problems(prob)
                    severity = r_prob.get("_reasoning", {}).get("overall_severity", "pass")
                    findings = r_prob.get("_reasoning", {}).get("findings", [])
                    worst    = findings[0]["issue"] if findings else "Clean"

                    if severity == "critical":
                        emoji = "🚫"
                        critical_objs.append(name)
                    elif severity == "warning":
                        emoji = "⚠️"
                    else:
                        emoji = "✅"

                    rows.append({
                        "object":        name,
                        "verdict_emoji": emoji,
                        "severity":      severity,
                        "vertex_count":  mesh_vert_map.get(name, 0),
                        "problem_count": prob.get("problem_count", 0),
                        "worst_issue":   worst,
                        "stage":         f"Stage {stage['stage_number']} — {stage['stage_name']}",
                    })
                except Exception:
                    rows.append({
                        "object":        name,
                        "verdict_emoji": "❓",
                        "severity":      "unknown",
                        "vertex_count":  mesh_vert_map.get(name, 0),
                        "problem_count": 0,
                        "worst_issue":   "Analysis failed",
                        "stage":         "Unknown",
                    })

            # Sort worst first
            sev_order = {"critical": 0, "warning": 1, "pass": 2, "unknown": 3}
            rows.sort(key=lambda r: sev_order.get(r["severity"], 3))

            # Deep-dive on critical objects up to cap
            deep_results = {}
            for crit_name in critical_objs[:max_deep_dive]:
                try:
                    ue5 = _send_raw("run_unreal_readiness_check", name=crit_name)
                    r   = _reason_unreal_readiness(ue5)
                    deep_results[crit_name] = {
                        "blocking_errors": ue5.get("blocking_errors", 0),
                        "ue5_findings":    r.get("_reasoning", {}).get("findings", [])[:5],
                    }
                except Exception:
                    pass

            critical_count = sum(1 for r in rows if r["severity"] == "critical")
            warn_count     = sum(1 for r in rows if r["severity"] == "warning")
            pass_count     = sum(1 for r in rows if r["severity"] == "pass")

            return json.dumps({
                "mode":           "COLLECTION",
                "object_count":   mesh_count,
                "verdict_summary": f"{critical_count} CRITICAL, {warn_count} WARN, {pass_count} PASS",
                "ranked_table":   rows,
                "deep_dive_results": deep_results,
                "_note": (
                    f"Deep analysis run on {len(deep_results)} critical object(s). "
                    f"Ask about any specific object for full detail."
                ) if deep_results else "All objects clean — no deep-dive needed.",
            }, indent=2, default=str)

        # ENVIRONMENT MODE ──────────────────────────────────────────────────────
        else:
            rows         = []
            critical_top = []

            for m in meshes:
                name  = m.get("name", "")
                # Use pre-built mesh_vert_map (get_mesh_quality_report data).
                # If this mesh was beyond the [:30] cap, fetch on demand.
                if name in mesh_vert_map:
                    verts = mesh_vert_map[name]
                else:
                    try:
                        _s = _send_raw("get_mesh_quality_report", name=name)
                        verts = _s.get("counts", {}).get("verts", 0) or 0
                        mesh_vert_map[name] = verts   # cache for total_verts below
                    except Exception:
                        verts = 0
                        mesh_vert_map[name] = 0
                try:
                    prob     = _send_raw("detect_mesh_problems", name=name)
                    r_prob   = _reason_mesh_problems(prob)
                    severity = r_prob.get("_reasoning", {}).get("overall_severity", "pass")
                    findings = r_prob.get("_reasoning", {}).get("findings", [])
                    worst    = findings[0]["issue"] if findings else "Clean"
                    prob_ct  = prob.get("problem_count", 0)
                except Exception:
                    severity = "unknown"
                    worst    = "Analysis failed"
                    prob_ct  = 0

                emoji = {"critical": "🚫", "warning": "⚠️", "pass": "✅"}.get(severity, "❓")

                row = {
                    "object":        name,
                    "verdict_emoji": emoji,
                    "severity":      severity,
                    "vertex_count":  verts,
                    "problem_count": prob_ct,
                    "worst_issue":   worst,
                }
                rows.append(row)

                if severity == "critical":
                    critical_top.append(row)

            sev_order = {"critical": 0, "warning": 1, "pass": 2, "unknown": 3}
            rows.sort(key=lambda r: sev_order.get(r["severity"], 3))
            critical_top = rows[:5]  # top 5 worst

            critical_count = sum(1 for r in rows if r["severity"] == "critical")
            warn_count     = sum(1 for r in rows if r["severity"] == "warning")
            pass_count     = sum(1 for r in rows if r["severity"] == "pass")
            total_verts    = sum(mesh_vert_map.get(m.get("name", ""), 0) or 0 for m in meshes)

            non_mesh_summary = {
                "armatures": len([o for o in objects if o.get("type") == "ARMATURE"]),
                "lights":    len([o for o in objects if o.get("type") == "LIGHT"]),
                "cameras":   len([o for o in objects if o.get("type") == "CAMERA"]),
                "empties":   len([o for o in objects if o.get("type") == "EMPTY"]),
            }

            # Full table for small environments, severity summary for large
            output_table = rows if mesh_count <= 20 else rows[:20]
            truncated    = mesh_count > 20

            return json.dumps({
                "mode":            "ENVIRONMENT",
                "total_objects":   len(objects),
                "mesh_count":      mesh_count,
                "total_verts":     total_verts,
                "verdict_summary": f"{critical_count} CRITICAL, {warn_count} WARN, {pass_count} PASS",
                "top_5_critical":  critical_top,
                "non_mesh_objects": non_mesh_summary,
                "object_table":    output_table,
                "table_truncated": truncated,
                "_note": (
                    f"Showing worst 20 of {mesh_count} meshes. "
                    "Ask about any specific object for full detail."
                ) if truncated else
                    "Ask about any specific object for full depth analysis.",
            }, indent=2, default=str)

    except Exception as e:
        logger.error(f"Error in audit_all_objects: {e}")
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

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
    Returns an enriched dict with severity, impact, fix recommendation, and
    whether the MCP can safely auto-repair it.
    """
    findings = []
    auto_fixable = []
    needs_artist = []

    problems = raw.get("problems", {})
    counts = raw.get("counts", {})

    # Non-manifold edges
    nm = counts.get("non_manifold_edges", 0)
    if nm > 0:
        sev = "critical" if nm > 20 else "warning"
        findings.append({
            "issue": f"{nm} non-manifold edge(s) detected",
            "severity": sev,
            "why_it_matters": (
                "Non-manifold geometry means edges shared by more than two faces or "
                "faces with no volume. UE5's import pipeline and subdivision modifiers "
                "both fail unpredictably on non-manifold meshes. Physics simulation and "
                "normal baking will produce incorrect results."
            ),
            "professional_fix": (
                "In Edit Mode select Non-Manifold (Select > Select All by Trait > "
                "Non-Manifold). Identify whether the cause is interior faces, naked "
                "edges, or open holes. Delete interior faces first, then merge "
                "overlapping vertices, then fill or bridge remaining open edges."
            ),
            "auto_fixable": False,
            "auto_fix_reason": "Non-manifold repair requires artist judgement on intent.",
        })
        needs_artist.append("non_manifold_edges")

    # Loose vertices
    lv = counts.get("loose_vertices", 0)
    if lv > 0:
        findings.append({
            "issue": f"{lv} loose vertex/vertices (not connected to any edge)",
            "severity": "warning",
            "why_it_matters": (
                "Loose vertices inflate vertex count with zero visual contribution, "
                "confuse UV unwrapping, and can shift the object's bounding box, "
                "causing incorrect pivot placement in UE5."
            ),
            "professional_fix": "Delete with: Edit Mode > Mesh > Clean Up > Delete Loose.",
            "auto_fixable": True,
            "auto_fix_reason": "Safe to delete automatically — no topology is affected.",
        })
        auto_fixable.append("loose_vertices")

    # Zero-area faces
    zf = counts.get("zero_area_faces", 0)
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
            "professional_fix": (
                "Mesh > Clean Up > Degenerate Dissolve (threshold 0.0001). "
                "Inspect results — some may indicate underlying topology errors."
            ),
            "auto_fixable": True,
            "auto_fix_reason": "Degenerate dissolve is non-destructive at low threshold.",
        })
        auto_fixable.append("zero_area_faces")

    # Duplicate faces
    df = counts.get("duplicate_faces", 0)
    if df > 0:
        findings.append({
            "issue": f"{df} duplicate face(s) (same vertex set as another face)",
            "severity": "critical",
            "why_it_matters": (
                "Duplicate faces cause z-fighting in real-time rendering — flickering "
                "surfaces visible at all distances. They also double the draw cost for "
                "zero visual benefit and corrupt normal baking."
            ),
            "professional_fix": (
                "Mesh > Clean Up > Merge by Distance (0.0001m) then "
                "Select > Select All by Trait > Face Sides with Faces = 0 to catch "
                "remaining duplicates. Delete them."
            ),
            "auto_fixable": True,
            "auto_fix_reason": "Merge by distance reliably eliminates duplicates.",
        })
        auto_fixable.append("duplicate_faces")

    # Inverted normals
    inv = counts.get("inverted_normals", 0)
    if inv > 0:
        sev = "critical" if inv > 0 else "info"
        findings.append({
            "issue": f"{inv} face(s) with inverted normals",
            "severity": sev,
            "why_it_matters": (
                "Inverted normals appear black or invisible in UE5's default backface-culled "
                "rendering. They also cause incorrect shadow casting and break two-sided "
                "material setups."
            ),
            "professional_fix": (
                "Edit Mode > select all > Mesh > Normals > Recalculate Outside (Shift+N). "
                "For complex enclosed meshes, manually flip individual faces."
            ),
            "auto_fixable": True,
            "auto_fix_reason": "Recalculate Outside is safe for closed, non-overlapping meshes.",
        })
        auto_fixable.append("inverted_normals")

    # Overlapping UVs
    uv_ov = counts.get("uv_overlaps", 0)
    if uv_ov > 0:
        findings.append({
            "issue": f"{uv_ov} overlapping UV island(s)",
            "severity": "warning",
            "why_it_matters": (
                "Overlapping UVs mean multiple surface regions share the same texture "
                "space. This is intentional for tiling but catastrophic for lightmap "
                "baking — UE5 will produce incorrect lightmaps and shadowing artifacts."
            ),
            "professional_fix": (
                "Use UV Channel 0 for texture mapping (overlaps allowed intentionally). "
                "Create UV Channel 1 as a dedicated non-overlapping lightmap UV — "
                "Smart UV Project or Lightmap Pack in Blender. Set this as the lightmap "
                "UV index in UE5's Static Mesh Editor."
            ),
            "auto_fixable": False,
            "auto_fix_reason": "UV layout decisions require artist review of intent.",
        })
        needs_artist.append("uv_overlaps")

    # Overall severity
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
        f"PASS — mesh is clean." if overall == "pass"
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
    """Interpret get_mesh_quality_report output with professional context."""
    findings = []

    vert_count = raw.get("vertex_count", 0)
    face_count = raw.get("face_count", 0)
    ngon_count = raw.get("ngon_count", 0)
    tri_count  = raw.get("tri_count", 0)
    uv_oob     = raw.get("uv_out_of_bounds", 0)
    dup_faces  = raw.get("duplicate_faces", 0)

    # N-gon check
    if ngon_count > 0 and face_count > 0:
        ngon_pct = (ngon_count / face_count) * 100
        sev = "critical" if ngon_pct > 20 else "warning"
        findings.append({
            "issue": f"{ngon_count} n-gons ({ngon_pct:.1f}% of faces)",
            "severity": sev,
            "why_it_matters": (
                "N-gons (5+ sided faces) tessellate unpredictably in real-time engines. "
                "UE5 will auto-triangulate them but the result often produces star "
                "patterns and shading errors under normal maps and dynamic lighting. "
                "Subdivision modifiers will also pinch at n-gon boundaries."
            ),
            "professional_fix": (
                "Manually dissolve n-gon edges and re-route topology using quads. "
                "Use Loop Cut tools to redirect edge flow. Target areas near curved "
                "surfaces and deforming joints first."
            ),
        })

    # Vertex density check
    if face_count > 0:
        vpf = vert_count / face_count
        if vpf > 4.5:
            findings.append({
                "issue": f"High vertex-to-face ratio ({vpf:.2f} verts/face — expected ~4.0 for quads)",
                "severity": "warning",
                "why_it_matters": (
                    "A ratio significantly above 4.0 indicates many triangulated patches "
                    "or redundant edge loops. This inflates GPU vertex processing cost "
                    "without adding surface detail."
                ),
                "professional_fix": (
                    "Dissolve redundant edge loops that don't support surface curvature. "
                    "Target straight runs of edges on flat surfaces."
                ),
            })

    # UV out-of-bounds
    if uv_oob > 0:
        findings.append({
            "issue": f"{uv_oob} UV loop(s) outside 0–1 UV space",
            "severity": "warning",
            "why_it_matters": (
                "UVs outside the 0–1 tile are valid for tiling textures but will "
                "cause issues with lightmap baking and trim-sheet workflows in UE5 "
                "if they appear on the lightmap UV channel."
            ),
            "professional_fix": (
                "Verify which UV channel these loops are on. If on Channel 0 (colour "
                "texture) this may be intentional tiling — acceptable. If on Channel 1 "
                "(lightmap), pack all islands inside 0–1 space."
            ),
        })

    # Duplicate faces
    if dup_faces > 0:
        findings.append({
            "issue": f"{dup_faces} duplicate face(s)",
            "severity": "critical",
            "why_it_matters": "Z-fighting, doubled draw cost, corrupt normal baking.",
            "professional_fix": "Mesh > Clean Up > Merge by Distance (0.0001m).",
        })

    severities = [f["severity"] for f in findings]
    overall = "critical" if "critical" in severities else ("warning" if "warning" in severities else ("info" if findings else "pass"))

    return {
        **raw,
        "_reasoning": {
            "overall_severity": overall,
            "summary": (
                f"PASS — mesh quality is acceptable." if overall == "pass"
                else f"{overall.upper()} — {len(findings)} quality issue(s) found."
            ),
            "findings": findings,
            "production_ready": overall in ("pass", "info"),
        }
    }


def _reason_topology(raw: dict) -> dict:
    """Interpret analyze_topology output with professional context."""
    findings = []

    quad_ratio  = raw.get("quad_ratio", 100)
    tri_ratio   = raw.get("tri_ratio", 0)
    ngon_ratio  = raw.get("ngon_ratio", 0)
    pole_count  = raw.get("pole_count", 0)
    face_count  = raw.get("face_count", 0)
    context     = raw.get("context", "generic")

    # Context-aware thresholds
    thresholds = {
        "character_body": {"min_quad": 85, "max_tri": 10, "max_ngon": 2},
        "face":           {"min_quad": 90, "max_tri":  5, "max_ngon": 1},
        "hand":           {"min_quad": 88, "max_tri":  8, "max_ngon": 1},
        "hard_surface":   {"min_quad": 70, "max_tri": 25, "max_ngon": 5},
        "generic":        {"min_quad": 75, "max_tri": 20, "max_ngon": 5},
    }
    t = thresholds.get(context, thresholds["generic"])

    if quad_ratio < t["min_quad"]:
        gap = t["min_quad"] - quad_ratio
        sev = "critical" if gap > 20 else "warning"
        findings.append({
            "issue": f"Quad ratio {quad_ratio:.1f}% — below {t['min_quad']}% target for context '{context}'",
            "severity": sev,
            "why_it_matters": (
                "Low quad ratio degrades deformation quality for skinned meshes, "
                "produces unpredictable subdivision surface results, and indicates "
                "topology that was likely generated rather than modelled intentionally. "
                f"For '{context}' work, studios target {t['min_quad']}%+ quads."
            ),
            "professional_fix": (
                "Manually retopologise high-tri areas using Blender's Poly Build tool "
                "or RetopoFlow. Prioritise areas that deform (joints, face muscles). "
                "Hard-surface areas can tolerate more tris at surface terminations."
            ),
        })

    if tri_ratio > t["max_tri"]:
        findings.append({
            "issue": f"Triangle ratio {tri_ratio:.1f}% — exceeds {t['max_tri']}% limit for context '{context}'",
            "severity": "warning",
            "why_it_matters": (
                "Excessive triangles in deforming areas cause skin-weighting artefacts "
                "and normal map shading errors under animation. Acceptable in hard-surface "
                "termination loops but not on organic forms."
            ),
            "professional_fix": (
                "Identify tri clusters using Face Select mode filtered by Sides = 3. "
                "Redirect edge flow to convert tri fans into clean quad patches."
            ),
        })

    if ngon_ratio > t["max_ngon"]:
        findings.append({
            "issue": f"N-gon ratio {ngon_ratio:.1f}% — exceeds {t['max_ngon']}% limit",
            "severity": "warning" if ngon_ratio < 10 else "critical",
            "why_it_matters": "N-gons tessellate unpredictably and produce shading artifacts in UE5.",
            "professional_fix": "Dissolve n-gon edges and re-route as quad patches.",
        })

    # Pole density
    if face_count > 0 and pole_count > 0:
        pole_density = pole_count / face_count
        if pole_density > 0.15:
            findings.append({
                "issue": f"High pole density ({pole_count} poles / {face_count} faces = {pole_density:.2%})",
                "severity": "info",
                "why_it_matters": (
                    "Poles (vertices with ≠4 edges) are necessary at surface transitions but "
                    "excessive poles cause pinching under subdivision and complicate skin weighting."
                ),
                "professional_fix": (
                    "Review pole placement — ensure 5-poles are at convex transitions "
                    "and 3-poles at concave ones. Avoid poles in deforming joint areas."
                ),
            })

    severities = [f["severity"] for f in findings]
    overall = "critical" if "critical" in severities else ("warning" if "warning" in severities else ("info" if findings else "pass"))

    return {
        **raw,
        "_reasoning": {
            "overall_severity": overall,
            "context_evaluated": context,
            "thresholds_applied": t,
            "summary": (
                f"PASS — topology meets '{context}' production standard." if overall == "pass"
                else f"{overall.upper()} — topology does not meet '{context}' standard. "
                     f"{len(findings)} issue(s) found."
            ),
            "findings": findings,
            "production_ready": overall == "pass",
        }
    }


def _reason_unreal_readiness(raw: dict) -> dict:
    """Interpret run_unreal_readiness_check output with UE5 pipeline context."""
    findings = []
    checks = raw.get("checks", {})

    check_meta = {
        "scale_applied": {
            "label": "Object scale not applied (non-unit scale)",
            "severity": "critical",
            "why": (
                "Non-applied scale is the single most common source of UE5 import bugs. "
                "The object will import at the wrong size, physics collision will be "
                "incorrectly scaled, and skeletal mesh bind poses will be broken."
            ),
            "fix": "Object Mode > Object > Apply > Scale (Ctrl+A > Scale) before export.",
            "auto_fixable": True,
        },
        "pivot_at_origin": {
            "label": "Pivot not at world origin",
            "severity": "warning",
            "why": (
                "UE5 uses the mesh pivot as the actor origin. A pivot offset from geometry "
                "centre causes unintuitive placement, rotation around wrong point, and "
                "incorrect socket/attachment positions."
            ),
            "fix": "Set origin to geometry (Object > Set Origin > Origin to Geometry) or to scene origin for characters.",
            "auto_fixable": False,
        },
        "naming_convention": {
            "label": "Naming convention not followed (SM_ / SK_ prefix missing)",
            "severity": "warning",
            "why": (
                "UE5 uses SM_ (Static Mesh) and SK_ (Skeletal Mesh) prefixes as pipeline "
                "conventions. Without them, asset management tools, import rules, and "
                "Blueprint references become inconsistent."
            ),
            "fix": "Rename object: SM_AssetName for static meshes, SK_AssetName for skeletal meshes.",
            "auto_fixable": False,
        },
        "triangulated": {
            "label": "Mesh not pre-triangulated",
            "severity": "info",
            "why": (
                "UE5 auto-triangulates on import — this is generally fine. Pre-triangulating "
                "in Blender gives you control over the triangulation pattern, which matters "
                "for normal map accuracy on curved surfaces."
            ),
            "fix": "Optional: Add Triangulate modifier and apply before export, or enable Triangulate in the FBX export dialog.",
            "auto_fixable": True,
        },
        "has_uvs": {
            "label": "No UV maps found",
            "severity": "critical",
            "why": (
                "Without UVs, no texture can be applied in UE5. Lightmap baking will also "
                "fail. This asset cannot be used in production without UV unwrapping."
            ),
            "fix": "Unwrap in UV Editor (U in Edit Mode). Create a second UV channel for lightmaps.",
            "auto_fixable": False,
        },
        "lightmap_uv": {
            "label": "No dedicated lightmap UV channel (UV channel 1)",
            "severity": "warning",
            "why": (
                "Without a non-overlapping lightmap UV, Unreal's Lightmass cannot bake "
                "correct shadows onto this mesh. It will show uniform shadowing or artifacts."
            ),
            "fix": "Create UV Channel 1 using Smart UV Project or Lightmap Pack in Blender's UV Editor.",
            "auto_fixable": False,
        },
        "normal_map_direction": {
            "label": "Normal map direction — Blender (OpenGL) vs UE5 (DirectX)",
            "severity": "warning",
            "why": (
                "Blender and UE5 use opposite Y-axis convention for normal maps. "
                "A normal map baked in Blender will look inverted (lighting from wrong "
                "direction) when applied in UE5 without conversion."
            ),
            "fix": (
                "In UE5 Texture Editor: enable 'Flip Green Channel' on normal map textures. "
                "Or bake with Y-flipped normals in Blender by enabling 'Flip Y' in the "
                "bake settings."
            ),
            "auto_fixable": False,
        },
    }

    for key, meta in check_meta.items():
        check = checks.get(key, {})
        passed = check.get("pass", True)
        if not passed:
            sev = check.get("severity", meta["severity"])
            findings.append({
                "issue": meta["label"],
                "severity": sev,
                "why_it_matters": meta["why"],
                "professional_fix": meta["fix"],
                "auto_fixable": meta.get("auto_fixable", False),
            })

    blocking = [f for f in findings if f["severity"] == "critical"]
    advisory = [f for f in findings if f["severity"] == "warning"]

    overall = "critical" if blocking else ("warning" if advisory else ("info" if findings else "pass"))

    return {
        **raw,
        "_reasoning": {
            "overall_severity": overall,
            "summary": (
                "PASS — asset meets UE5 import requirements." if overall == "pass"
                else f"{overall.upper()} — {len(blocking)} blocking error(s), "
                     f"{len(advisory)} advisory warning(s) before UE5 export."
            ),
            "blocking_errors": blocking,
            "advisory_warnings": advisory,
            "export_safe": overall in ("pass", "info"),
            "findings": findings,
        }
    }


def _reason_animation(raw: dict) -> dict:
    """Interpret analyze_animation_quality output with professional animation context."""
    findings = []

    score   = raw.get("score", 100)
    warns   = raw.get("warnings", [])
    errors  = raw.get("errors", [])
    info    = raw.get("info", [])

    for e in errors:
        findings.append({
            "issue": e,
            "severity": "critical",
            "category": _classify_animation_issue(e),
            "why_it_matters": _animation_why(e),
            "professional_fix": _animation_fix(e),
        })

    for w in warns:
        findings.append({
            "issue": w,
            "severity": "warning",
            "category": _classify_animation_issue(w),
            "why_it_matters": _animation_why(w),
            "professional_fix": _animation_fix(w),
        })

    for i in info:
        findings.append({
            "issue": i,
            "severity": "info",
            "category": _classify_animation_issue(i),
            "why_it_matters": "",
            "professional_fix": "",
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

    overall = "critical" if errors else ("warning" if warns else ("info" if info else "pass"))

    return {
        **raw,
        "_reasoning": {
            "overall_severity": overall,
            "grade": grade,
            "score": score,
            "summary": (
                f"Animation grade: {grade}. Score: {score}/100. "
                f"{len(errors)} error(s), {len(warns)} warning(s)."
            ),
            "findings": findings,
            "production_ready": score >= 75,
        }
    }


def _classify_animation_issue(text: str) -> str:
    t = text.lower()
    if "foot" in t or "sliding" in t:
        return "foot_contact"
    if "jitter" in t or "noise" in t:
        return "keyframe_noise"
    if "linear" in t:
        return "interpolation"
    if "velocity" in t or "spike" in t:
        return "velocity"
    if "spine" in t or "pelvis" in t or "hip" in t:
        return "body_mechanics"
    if "arm" in t or "shoulder" in t:
        return "upper_body"
    return "general"


def _animation_why(text: str) -> str:
    t = text.lower()
    if "foot" in t or "sliding" in t:
        return (
            "Foot sliding is the most immediately visible animation error. It breaks "
            "the physical contract between the character and the ground, destroying "
            "believability in any third-person or VR context."
        )
    if "jitter" in t or "noise" in t:
        return (
            "High-frequency keyframe jitter is usually caused by motion capture noise "
            "or over-corrected curves. It reads as vibration at runtime, not as motion, "
            "and is particularly visible on held poses and slow movements."
        )
    if "linear" in t:
        return (
            "LINEAR interpolation between keyframes produces mechanical, robotic motion. "
            "Organic motion requires ease-in/ease-out curves (BEZIER or AUTO interpolation) "
            "to read as weight and physical momentum."
        )
    if "velocity" in t or "spike" in t:
        return (
            "Velocity spikes mean a bone is moving impossibly fast between two frames. "
            "This causes popping artefacts in compressed animations and breaks secondary "
            "motion systems like IK solvers and cloth simulation."
        )
    return "Review in the Dope Sheet and Graph Editor for context."


def _animation_fix(text: str) -> str:
    t = text.lower()
    if "foot" in t or "sliding" in t:
        return (
            "In the Graph Editor, identify the foot bone's location curves during the "
            "contact phase. Manually lock XY translation during ground contact frames. "
            "Use IK constraints with a foot controller locked to world space."
        )
    if "jitter" in t or "noise" in t:
        return (
            "Select affected bones in Pose Mode, open Graph Editor, select all curves, "
            "apply Smooth Keys (Key > Smooth Keys) 2–3 times. Alternatively use the "
            "Decimate modifier on the F-curve with a ratio of 0.3–0.5."
        )
    if "linear" in t:
        return (
            "Select all keyframes in the Dope Sheet. Key > Interpolation Mode > Bezier. "
            "Then use Key > Handle Type > Auto Clamped to prevent overshoot."
        )
    if "velocity" in t or "spike" in t:
        return (
            "Locate the spike in the Graph Editor (look for a V-shape in the curve). "
            "Delete or move the offending keyframe. Check for duplicate keyframes on "
            "the same frame — they create zero-duration transitions."
        )
    return "Review in the Dope Sheet and Graph Editor."


def _reason_asset_qa(raw: dict) -> dict:
    """Interpret run_asset_qa output with production QA context."""
    findings = []

    issues   = raw.get("issues", [])
    warnings = raw.get("warnings", [])

    for issue in issues:
        findings.append({
            "issue": issue,
            "severity": "critical",
            "why_it_matters": "Blocking issue — asset will fail pipeline validation.",
            "professional_fix": "Resolve before export.",
        })

    for warning in warnings:
        findings.append({
            "issue": warning,
            "severity": "warning",
            "why_it_matters": "Advisory — may cause downstream problems.",
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
# AUTO-REPAIR ENGINE
# ─────────────────────────────────────────────────────────────────────────────

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

_REPAIR_ORDER = [
    "loose_vertices",
    "duplicate_faces",
    "zero_area_faces",
    "inverted_normals",
]


mcp = FastMCP("BlenderMCP")


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
    """Capture a screenshot of the current Blender 3D viewport."""
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
        activate_result = blender.send_command("execute_code", {"code": set_active_script})
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
            if repair_key not in auto_repairable:
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
                result = blender.send_command("execute_code", {"code": full_script})
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

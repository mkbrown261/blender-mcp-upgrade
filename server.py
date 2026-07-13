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
import re
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


# ─────────────────────────────────────────────────────────────────────────────
# SESSION CONTEXT — persists for the lifetime of this MCP server process.
# Stores what Claude knows about the current work session so every tool call
# can reference confirmed facts instead of re-inferring from scratch.
# ─────────────────────────────────────────────────────────────────────────────

_SESSION: dict = {
    # What kind of asset is being worked on (user-confirmed or inferred)
    "asset_type": None,           # e.g. "hero_character" | "weapon" | "environment_prop"
    # Which named playbook is active
    "active_playbook": None,      # e.g. "hero_char" | "weapon" | "env_prop" | "creature" | "vehicle"
    # Pipeline stage — confirmed by user or strong inference
    "confirmed_stage": None,      # 1–6 or None if unknown
    # Which tool calls have been verified this session
    "verified_checks": [],        # e.g. ["analyze_mesh_for_unreal", "analyze_rig_weights"]
    # Issues the user acknowledged or that remain open
    "open_issues": [],            # e.g. ["shoulder_deformation", "uv_margin_tight"]
    # Object name being worked on
    "active_object": None,        # e.g. "SK_Mannequin"
    # User corrections / overrides this session
    "user_corrections": [],       # e.g. ["user said this is a weapon, not prop"]
    # Playbook conflicts Claude noticed and surfaced to user
    "surfaced_conflicts": [],     # e.g. ["vert_budget: 3x weapon limit but user said weapon"]
    # Apprentice mode — when True, every action gets a _why teaching note
    "apprentice_mode": False,
    # TD mode — when True, plan_production_path prepends a 5-step plan to every run
    "td_mode": False,
}


def _session_get(key: str, default=None):
    """Read a value from the session context."""
    return _SESSION.get(key, default)


def _session_set(**kwargs):
    """Write one or more values into the session context."""
    global _SESSION
    for k, v in kwargs.items():
        if k in _SESSION:
            _SESSION[k] = v
        else:
            logger.warning(f"_session_set: unknown key '{k}' ignored")


def _session_append(key: str, value):
    """Append a value to a session list field."""
    global _SESSION
    lst = _SESSION.get(key)
    if isinstance(lst, list) and value not in lst:
        lst.append(value)


# ─────────────────────────────────────────────────────────────────────────────
# PRODUCTION PLAYBOOKS — named workflows with asset-type-aware standards.
# what_next, production_review, and plan_production_path all consult
# the active playbook via _get_active_playbook().
# ─────────────────────────────────────────────────────────────────────────────

_PLAYBOOKS: dict = {
    "hero_char": {
        "name":        "Hero Character",
        "description": "Main playable or cinematic character. Held to the highest standards.",
        "vert_budget": 80_000,
        "face_budget": 80_000,
        "uv_channels": 2,           # channel 0 = colour, channel 1 = lightmap
        "material_limit": 3,        # strict draw-call discipline
        "topology_score_min": 75,   # quad-dominant, joint loops required
        "mandatory_checks": [
            "analyze_mesh_for_unreal",
            "analyze_rig_weights",
            "analyze_rig_skeleton",
            "run_unreal_readiness_check",
            "analyze_material_pbr",
        ],
        "skip_checks": [],
        "stage_standards": {
            2: "Quad dominance >85% in ALL deformation zones. Edge loops at shoulder, elbow, knee, wrist. No poles in deformation areas.",
            3: "Two UV channels required. Channel 1 lightmap must be non-overlapping 0–1. Normal map bake at 4K minimum.",
            4: "Principled BSDF only. No procedural-only materials. Texture paths must resolve. Normal map Y-flip for UE5.",
            5: "Max 4 influences per vertex preferred, hard limit 8. Root bone at world origin ±0.001. No orphan bones.",
            6: "Scale applied (1,1,1). FBX axis: Y-forward, Z-up. LODs required at 50%/25%/12.5%.",
        },
        "gotchas": [
            "Shoulder deformation: needs 3 loops minimum — axilla loop, clavicle loop, deltoid loop.",
            "Knee/elbow: perpendicular loops to joint axis. Single loop = creasing on full bend.",
            "UE5 import: do not apply Armature modifier before export — export with modifier live.",
            "Root bone: must be at world origin. Even 0.1 unit offset causes animation drift.",
            "Vertex count includes hidden geometry — check in viewport not Properties panel.",
        ],
    },

    "creature": {
        "name":        "Creature / Monster",
        "description": "Non-human characters — animals, monsters, aliens. Similar rig standards to hero but topology priorities differ.",
        "vert_budget": 60_000,
        "face_budget": 60_000,
        "uv_channels": 1,
        "material_limit": 4,
        "topology_score_min": 65,
        "mandatory_checks": [
            "analyze_mesh_for_unreal",
            "analyze_rig_weights",
            "analyze_rig_skeleton",
            "run_unreal_readiness_check",
        ],
        "skip_checks": [],
        "stage_standards": {
            2: "Focus joint loops on creature-specific anatomy. Fins, tails, and wings need span loops for deformation.",
            3: "UV layout: atlas or per-region. Fur/scale assets often use tiling textures — UV channel discipline critical.",
            5: "Creature rigs often use non-standard bone naming. Verify UE5 retargeter compatibility before assuming names are correct.",
        },
        "gotchas": [
            "Creature tails: need 2 edge loops per bone segment minimum for smooth arc deformation.",
            "Fins and membranes: check for zero-area faces at thin areas — baking will show black patches.",
            "Non-standard bone names: UE5 retargeting requires explicit mapping if not UE5 convention.",
        ],
    },

    "weapon": {
        "name":        "Weapon",
        "description": "Hand-held or carried weapon. Hard surface, no rig (unless procedural animation). Strict budget.",
        "vert_budget": 15_000,
        "face_budget": 15_000,
        "uv_channels": 1,
        "material_limit": 2,
        "topology_score_min": 60,
        "mandatory_checks": [
            "analyze_mesh_for_unreal",
            "run_unreal_readiness_check",
            "analyze_material_pbr",
        ],
        "skip_checks": ["analyze_rig_weights", "analyze_rig_skeleton"],
        "stage_standards": {
            2: "Hard-surface: beveled edges on silhouette. Tris acceptable in hidden/interior areas. No ngons on visible surfaces.",
            3: "Single UV channel. 2K texture typical, 4K for hero-tier weapons. Texel density consistent across all surfaces.",
            4: "Metal surfaces: metallic 0.8–1.0, roughness varies. Emissive for scopes/optics/energy weapons only.",
            6: "Pivot at grip/handle base for correct in-hand positioning. Apply all transforms before export.",
        },
        "gotchas": [
            "Pivot point: barrel should align with UE5 socket — wrong pivot = floating in player's hand.",
            "Interior geometry: delete faces that are never visible. Silencers, stocks — check for unnecessary hidden geo.",
            "Hard-surface UV: avoid UV islands smaller than 8x8 pixels at target texture resolution.",
        ],
    },

    "env_prop": {
        "name":        "Environment Prop",
        "description": "Background/environment asset. Optimised for density and batching. LODs critical.",
        "vert_budget": 20_000,
        "face_budget": 20_000,
        "uv_channels": 2,           # lightmap UV required
        "material_limit": 1,        # single material preferred for batching
        "topology_score_min": 50,   # lower bar — tris fine, ngons acceptable on flat surfaces
        "mandatory_checks": [
            "analyze_mesh_for_unreal",
            "run_unreal_readiness_check",
        ],
        "skip_checks": ["analyze_rig_weights", "analyze_rig_skeleton", "critique_animation"],
        "stage_standards": {
            2: "Tris acceptable on flat/non-visible surfaces. Silhouette quality matters — profile edges should be clean.",
            3: "Lightmap UV (channel 1) is mandatory for static props — no overlapping islands, no islands outside 0–1.",
            4: "Single material preferred for instanced rendering performance. Texture atlas if multiple surface types.",
            6: "LOD required: LOD0 full, LOD1 50%, LOD2 25%. Collision mesh separate (simple convex hulls).",
        },
        "gotchas": [
            "Lightmap resolution: small props at 64px, medium at 128px, large at 256px. Mismatched = shadow bleeding.",
            "Pivot at base center for floor props, geometric center for hanging/floating props.",
            "Instanced static meshes: any variation must be a separate asset — do not rely on material parameters for variation.",
        ],
    },

    "vehicle": {
        "name":        "Vehicle",
        "description": "Driveable or cinematically animated vehicle. Complex structure, strict draw-call discipline.",
        "vert_budget": 60_000,
        "face_budget": 60_000,
        "uv_channels": 2,
        "material_limit": 5,
        "topology_score_min": 65,
        "mandatory_checks": [
            "analyze_mesh_for_unreal",
            "run_unreal_readiness_check",
            "analyze_material_pbr",
        ],
        "skip_checks": [],
        "stage_standards": {
            2: "Hard-surface: beveled panel edges. Tris inside wheel wells/undercarriage are fine. Silhouette must be clean.",
            3: "Separate UV tiles per major region (body, interior, wheels) acceptable. Consistent texel density across exterior.",
            4: "Vehicle glass: translucent material, two-sided, sorted after opaque. Interior faces need backface material.",
            6: "Wheel bones must align with rotation axis exactly. Even 0.1 unit offset = visible tire wobble at speed.",
        },
        "gotchas": [
            "Wheel pivot: must be exactly at wheel center — use 'Set Origin to Geometry' then manually verify axis alignment.",
            "Vehicle glass: transparent surfaces sort after opaque in UE5 — test in engine, not in Blender.",
            "Separate meshes for doors/hood/trunk if interactive. Do not bake moving parts into body mesh.",
        ],
    },
}


def _get_active_playbook() -> Optional[dict]:
    """Return the active playbook dict, or None if none is set."""
    key = _session_get("active_playbook")
    return _PLAYBOOKS.get(key) if key else None


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
                "auto_repair_mesh attempts: merge by distance (eliminates coincident "
                "verts that create T-junctions and overlapping faces), then limited "
                "hole fill (fills simple open boundary loops). Complex interior face "
                "non-manifolds that survive both passes require manual artist review: "
                "Edit Mode > Select > Select All by Trait > Non Manifold, then "
                "delete interior faces or bridge open loops."
            ),
            "auto_fixable": True,
            "auto_fix_reason": (
                "Partial auto-repair: merge-by-distance resolves most coincident-vert "
                "non-manifolds. Remaining count reported so artist knows what survived."
            ),
        })
        auto_fixable.append("non_manifold_edges")

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


# FIX: these three helpers were deleted in 41d2e84 ("v2.3 schema fix") while their
# call sites in _reason_animation above were kept, causing a NameError that stayed
# latent because the reasoning loop only executes when there's an actual finding —
# every test animation until now happened to score a clean 100/100. Restored from
# b730146 (the commit that introduced them, before the accidental deletion).
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
            "apply Smooth Keys (Key > Smooth Keys) 2-3 times. Alternatively use the "
            "Decimate modifier on the F-curve with a ratio of 0.3-0.5."
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
    # rigging.deform_modifiers: list of modifier NAMES (e.g. ["Armature", "Armature.001"])
    #   — addon.py line 529: [m.name for m in obj.modifiers if m.type in ('ARMATURE',...)]
    #   — NOT type strings — "ARMATURE" will never appear in this list
    # modifiers: [{name, type, show_viewport}]  ← use this for type checks
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

    # Modifier types from the modifiers list — correct source for type checks.
    # deform_modifiers holds names not types, so armature detection uses mod_list.
    modifier_types = [m.get("type", "") for m in mod_list if isinstance(m, dict)]
    has_armature   = "ARMATURE" in modifier_types
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
    "non_manifold_edges": """
import bpy
import bmesh
obj = bpy.context.active_object
bpy.ops.object.mode_set(mode='EDIT')

bm = bmesh.from_edit_mesh(obj.data)
nm_before = sum(1 for e in bm.edges if not e.is_manifold)

# Pass 1: merge by distance — eliminates coincident verts that create
# T-junctions, overlapping faces, and non-manifold vert-touch points.
bpy.ops.mesh.select_all(action='SELECT')
bpy.ops.mesh.remove_doubles(threshold=0.0001)

# Pass 2: delete interior faces — faces fully enclosed by other faces
# cause non-manifold edges because an edge ends up shared by 3+ faces.
bpy.ops.mesh.select_all(action='DESELECT')
bpy.ops.mesh.select_interior_faces()
bpy.ops.mesh.delete(type='FACE')

# Pass 3: limited dissolve of remaining wire edges — stray edges with
# no face on either side leave non-manifold verts.
bpy.ops.mesh.select_all(action='DESELECT')
for e in bm.edges:
    e.select = (len(e.link_faces) == 0)
bmesh.update_edit_mesh(obj.data)
bpy.ops.mesh.dissolve_edges()

bm = bmesh.from_edit_mesh(obj.data)
nm_after = sum(1 for e in bm.edges if not e.is_manifold)
bpy.ops.object.mode_set(mode='OBJECT')
print(f"non_manifold_edges:done:before={nm_before}:after={nm_after}")
""",
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
    "non_manifold_edges",  # first: merge coincident verts + delete interior faces
    "loose_vertices",      # addon key: "isolated_verts" — after non-manifold cleanup
    "duplicate_faces",     # merge by distance (also helps non-manifold but scoped here)
    "zero_area_faces",
    "inverted_normals",    # last: recalc normals after all geometry changes
]


mcp = FastMCP(
    "BlenderMCP",
    instructions="""
BLENDER MCP — SENIOR TECHNICAL DIRECTOR v3.0
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
0. session_status()            ← check if session context exists from a prior turn
   If session has_context=True: orient from session, skip to screenshot only.
   If session empty: run full sequence below.
1. get_viewport_screenshot()   ← look before anything else
2. get_scene_info()            ← object count, types
3. get_object_info(active)     ← verts/faces at mesh.vertices/polygons, materials list
Then deliver ONE orientation sentence:
  "I see [asset]. [vert count]. Stage [N] inference. [⚠️ CRITICAL: X if present].
   Correct me if wrong — awaiting direction."
STOP. Wait for user. Do not auto-run further tools.

── TOOL CALL ORDER ────────────────────────────────────────────────────────────
TIER 0 (v3.0 — judgment layer, use before Tier 1 when context is ambiguous):
  session_status               read session context before starting any work
  set_playbook                 activate named workflow: hero_char|weapon|env_prop|creature|vehicle
  list_playbooks               show all available playbooks with their standards
  production_review            ONE command — full scored report (0-100), strengths, conflicts,
                               time estimate. The "show me everything" command.
  plan_production_path         AI TD — 5-step production plan with tools, gates, success criteria.
                               ALWAYS present plan and wait for approval before executing.
  critique_mesh                senior TA topology review — WHY it matters, compounding issues,
                               deformation risk, what I would do
  animation_coach              frame-specific coaching — contact timing, arcs, weight transfer,
                               animation principles. Apprentice lessons if apprentice_mode=True.
  session_update               record confirmed facts: asset_type, stage, verified checks, issues
  get_scene_graph              SPATIAL: full relationship graph — positions, distances, predicates
                               (above/beside/contains/intersecting), collection tree, floor contacts.
                               Use before any spatial reasoning or layout decision.
  query_spatial                SPATIAL: targeted spatial queries — nearest, in_radius, intersecting,
                               supporting, above, below, raycast, floating, isolated
  describe_object_context      SPATIAL: rich per-object context — semantic role, nearest neighbors
                               with directions, floor contact, supported_by, spatial_sentence.
                               Read this before moving, parenting, or reasoning about one object.

TIER 1 (prefer — most coverage per call):
  what_next                    ONE priority action + playbook context if active
  analyze_mesh_for_unreal      full mesh + topology + UE5 readiness in one call
  analyze_animation_quality    full animation health check
  critique_animation           animation critique with stage context
TIER 2 (targeted):
  get_mesh_quality_report      mesh stats + problem types
  analyze_topology             topology score + pole analysis
  run_unreal_readiness_check   UE5 gate check
  run_asset_qa                 QA pass/fail verdict
  classify_pipeline_stage      infer production stage from signals
  analyze_material_pbr         full PBR node graph review
  analyze_rig_weights          weight QA: unweighted verts, >8 influences, zero-weight
  analyze_rig_skeleton         skeleton QA: root at origin, orphan bones, naming
  validate_bake_setup          bake pre-flight: 10 checks before touching Blender bake
TIER 3 (raw — only when Tier 0-2 don't cover it):
  detect_mesh_problems         raw problem list
  get_object_info              raw object data
  get_scene_info               raw scene data
TIER 4 (repair — always gate-controlled):
  auto_repair_mesh             DESTRUCTIVE — requires explicit user approval
  run_asset_qa                 call after auto_repair_mesh to verify repair

VERBOSE MODE: Most tools default to verbose=False (failing/warning findings only).
  Pass verbose=True when you need the full picture including passing checks.
  Tools with verbose param: analyze_mesh_for_unreal, validate_bake_setup,
  detect_mesh_problems, run_asset_qa, run_unreal_readiness_check,
  analyze_rig_weights, analyze_rig_skeleton, critique_mesh.

SCENE-LEVEL ORDER (never skip):
  screenshot → get_scene_summary() → classify_pipeline_stage(name) → audit_all_objects()
audit_all_objects auto-mode: 1 mesh = HERO, 2–20 = COLLECTION, 20+ = ENVIRONMENT.

── CONFLICT SURFACING — CRITICAL BEHAVIOR ──────────────────────────────────────
When data conflicts with what the user stated (asset type, budget, stage):
  DO: State the conflict clearly + ask for confirmation.
  NEVER: Silently resolve the conflict or ignore it.
  FORMAT: "UV is clean. Topology is clean. But the vert count is 3× the weapon
           budget you stated. Is this intentional (cinematic tier) or should I
           re-evaluate against a different playbook?"
production_review and what_next both surface conflicts automatically when a
playbook is active. Read the conflicts[] field and surface them verbatim.

── PLAYBOOK WORKFLOW ──────────────────────────────────────────────────────────
When user says "this is a [weapon/hero/prop/creature/vehicle]":
  1. set_playbook(playbook='weapon') immediately
  2. session_update(asset_type='weapon')
  3. Re-run what_next or production_review — playbook now applies correct standards
When playbook is active, what_next includes:
  - playbook.vert_budget and conflict if exceeded
  - playbook.stage_standard for the current stage
  - playbook.gotchas — the failure modes that burn people

── APPRENTICE MODE ────────────────────────────────────────────────────────────
When user says "explain as you go" / "teach me" / "I'm learning":
  → session_update(apprentice_mode=True) immediately
  → animation_coach includes animation principles lessons
  → plan_production_path includes step notes explaining each decision
  → critique_mesh includes why_it_matters for every finding
  → State principles, not just fixes: "This is an Arcs violation — natural
    motion curves, straight trajectories read as mechanical."
When user says "stop explaining" / "expert mode" / "just do it":
  → session_update(apprentice_mode=False)

── TRIGGER MAP ────────────────────────────────────────────────────────────────
  "what do I do next" / "where do I start" → what_next(object_name) immediately
  "look/show/what do you see"     → get_viewport_screenshot() immediately
  "ready for Unreal/export/UE5"   → analyze_mesh_for_unreal()
  "review/audit/full report"      → production_review(object_name, asset_type=...)
  "make a plan/plan it out"       → plan_production_path(object_name) — WAIT FOR APPROVAL
  "topology/loops/quads/critique" → critique_mesh(object_name)
  "what's wrong/check"            → analyze_mesh_for_unreal() (covers all systems)
  "fix/clean/repair"              → describe plan → WAIT "yes/do it" → auto_repair_mesh()
  "rig/weights/skinning/bones"    → analyze_rig_weights() then analyze_rig_skeleton()
  "bake/baking/normal map/AO"    → validate_bake_setup(low_poly, high_poly) FIRST
  "animation/coach/teach me anim" → animation_coach(name, focus=...)
  "this is a weapon/hero/prop"    → set_playbook() + session_update(asset_type=...) first
  "audit the scene/all objects"   → screenshot → get_scene_summary() → audit_all_objects()
  reference image + "match/build" → describe image → screenshot → gap report
  "where is / what's near / layout / spatial / scene graph"
                                  → get_scene_graph() then describe_object_context(name)
  "what's floating/intersecting/isolated/in radius/supporting"
                                  → query_spatial(query_type=...) — pick the right query type
  "make the room balanced/spread objects/layout reasoning"
                                  → get_scene_graph() first — reason from relationships, not coords

Screenshot required: session start, after any repair, before/after auto_repair_mesh,
when reporting any PASS/FAIL verdict.

── SAFETY GATES — hard stops, never bypass ────────────────────────────────────
GATE 1 DESTRUCTIVE GEOMETRY  → describe plan in full + explicit "yes/do it/go ahead"
                               → NEVER call auto_repair_mesh() without that confirmation
GATE 2 STAGE TRANSITION      → full QA for current stage + "Ready to move to X?"
GATE 3 EXPORT                → run_unreal_readiness_check() zero errors + run_asset_qa() PASS
GATE 4 IRREVERSIBLE OPS      → state exactly what happens + wait for explicit confirm
GATE 5 BAKE                  → validate_bake_setup() MUST run first, every time
GATE 6 TD PLAN               → plan_production_path() → PRESENT PLAN → WAIT for approval
                               → NEVER execute plan steps without explicit "yes/go ahead"

NEVER:
  ✗ auto_repair_mesh() without explicit user approval
  ✗ PASS without running the actual tool
  ✗ "clean" verdict from visual inspection alone
  ✗ Export with known critical issues
  ✗ Repair the wrong object
  ✗ Delete user data
  ✗ Resolve a playbook conflict silently — surface it, ask for confirmation
  ✗ Execute a TD plan without presenting it first

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

SESSION MEMORY: Always call session_status() at start of each turn if context may exist.
  After confirming asset type or stage → session_update() immediately.
  Don't re-run tools unless scene changed or user requests. Cite earlier findings.
  Stage shift mid-session → state it: "Shifting to Stage 5 standards from here."
""",
)


# ─────────────────────────────────────────────────────────────────────────────
# SESSION TOOLS
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def session_update(
    asset_type: str = "",
    active_playbook: str = "",
    confirmed_stage: int = 0,
    active_object: str = "",
    add_verified_check: str = "",
    add_open_issue: str = "",
    add_user_correction: str = "",
    apprentice_mode: bool = None,
    td_mode: bool = None,
) -> str:
    """
    SESSION CONTEXT — Update what Claude knows about the current work session.

    Call this whenever the user confirms or corrects something important:
    the asset type, active playbook, pipeline stage, or when a tool run is
    verified and complete.

    This persists for the lifetime of the MCP session so subsequent tool calls
    can reference confirmed facts without re-inferring from scratch.

    Parameters (all optional — only pass what changed):
      asset_type         : confirmed asset type — "hero_character" | "weapon" |
                           "environment_prop" | "creature" | "vehicle" | "npc" |
                           "background_character" | "crowd_character" | etc.
      active_playbook    : set the active playbook — "hero_char" | "weapon" |
                           "env_prop" | "creature" | "vehicle"
      confirmed_stage    : 1–6 — pipeline stage confirmed by user or strong evidence
      active_object      : Blender object name currently being worked on
      add_verified_check : tool name to mark as completed this session
                           e.g. "analyze_rig_weights"
      add_open_issue     : issue to track e.g. "shoulder_deformation"
      add_user_correction: record a user override e.g. "user said weapon not prop"
      apprentice_mode    : True/False — enable/disable teaching annotations
      td_mode            : True/False — enable/disable TD planning mode
    """
    if asset_type:
        _session_set(asset_type=asset_type)
    if active_playbook:
        _session_set(active_playbook=active_playbook)
    if confirmed_stage and 1 <= confirmed_stage <= 6:
        _session_set(confirmed_stage=confirmed_stage)
    if active_object:
        _session_set(active_object=active_object)
    if add_verified_check:
        _session_append("verified_checks", add_verified_check)
    if add_open_issue:
        _session_append("open_issues", add_open_issue)
    if add_user_correction:
        _session_append("user_corrections", add_user_correction)
    if apprentice_mode is not None:
        _session_set(apprentice_mode=apprentice_mode)
    if td_mode is not None:
        _session_set(td_mode=td_mode)

    return json.dumps({"session_updated": True, "current_session": _SESSION}, indent=2)


@mcp.tool()
def session_status() -> str:
    """
    SESSION STATUS — Read the current session context.

    Returns everything Claude knows about this session: confirmed asset type,
    active playbook, pipeline stage, which checks have been run, open issues,
    and any user corrections or overrides recorded this session.

    Call this at the start of a new conversation turn to orient yourself
    before reaching for Blender tools. If session is empty, fall back to
    the normal session-start sequence (screenshot → scene_info → object_info).
    """
    has_context = any([
        _SESSION.get("asset_type"),
        _SESSION.get("active_playbook"),
        _SESSION.get("confirmed_stage"),
        _SESSION.get("active_object"),
        _SESSION.get("verified_checks"),
    ])

    orientation = ""
    if has_context:
        parts = []
        if _SESSION.get("active_object"):
            parts.append(f"Working on: {_SESSION['active_object']}")
        if _SESSION.get("asset_type"):
            parts.append(f"Asset type: {_SESSION['asset_type']}")
        if _SESSION.get("active_playbook"):
            parts.append(f"Playbook: {_SESSION['active_playbook']}")
        if _SESSION.get("confirmed_stage"):
            stage_names = {1:"Sculpt", 2:"Retopo", 3:"Bake-Ready", 4:"Texture", 5:"Rig", 6:"Export"}
            sn = stage_names.get(_SESSION["confirmed_stage"], "Unknown")
            parts.append(f"Stage: {_SESSION['confirmed_stage']} ({sn})")
        if _SESSION.get("verified_checks"):
            parts.append(f"Checks run: {', '.join(_SESSION['verified_checks'])}")
        if _SESSION.get("open_issues"):
            parts.append(f"Open issues: {', '.join(_SESSION['open_issues'])}")
        if _SESSION.get("user_corrections"):
            parts.append(f"User overrides: {', '.join(_SESSION['user_corrections'])}")
        orientation = " | ".join(parts)
    else:
        orientation = "No session context yet. Run screenshot → get_scene_info → get_object_info to orient."

    return json.dumps({
        "has_context": has_context,
        "orientation_summary": orientation,
        "session": _SESSION,
    }, indent=2)


@mcp.tool()
def set_playbook(playbook: str) -> str:
    """
    PRODUCTION PLAYBOOK — Activate a named workflow for this asset.

    Sets the active playbook so what_next, production_review, and
    plan_production_path all evaluate this asset against the right
    standards, vertex budgets, mandatory checks, and known gotchas.

    Available playbooks:
      hero_char   — Main playable/cinematic character (80k vert limit, 2 UV channels,
                    full rig QA, 3-material max, LODs required)
      creature    — Non-human characters, animals, monsters (60k, creature rig notes)
      weapon      — Hand-held weapons, hard surface, no rig (15k, 2-material max)
      env_prop    — Background/environment prop (20k, single material, lightmap UV)
      vehicle     — Driveable or cinematic vehicle (60k, wheel bone alignment notes)

    After setting a playbook, what_next and production_review will:
      - Apply this playbook's vertex budget in conflict checks
      - Show playbook-specific gotchas relevant to the current stage
      - Mark playbook-mandatory checks as required
      - Skip checks that don't apply (e.g. rig QA on weapons)

    Parameters:
      playbook : one of "hero_char" | "creature" | "weapon" | "env_prop" | "vehicle"
    """
    if playbook not in _PLAYBOOKS:
        available = ", ".join(_PLAYBOOKS.keys())
        return json.dumps({
            "error": f"Unknown playbook '{playbook}'. Available: {available}",
        })

    pb = _PLAYBOOKS[playbook]
    _session_set(active_playbook=playbook)

    # Derive asset_type from playbook if not already set
    TYPE_MAP = {
        "hero_char": "hero_character",
        "creature":  "creature",
        "weapon":    "weapon",
        "env_prop":  "environment_prop",
        "vehicle":   "vehicle",
    }
    if not _session_get("asset_type"):
        _session_set(asset_type=TYPE_MAP.get(playbook, playbook))

    return json.dumps({
        "playbook_activated": playbook,
        "name":              pb["name"],
        "description":       pb["description"],
        "vert_budget":       pb["vert_budget"],
        "uv_channels":       pb["uv_channels"],
        "material_limit":    pb["material_limit"],
        "topology_score_min": pb["topology_score_min"],
        "mandatory_checks":  pb["mandatory_checks"],
        "skip_checks":       pb["skip_checks"],
        "gotchas":           pb["gotchas"],
        "note": (
            f"Playbook '{pb['name']}' is now active. what_next, production_review, "
            "and plan_production_path will use these standards. "
            "Use session_status() to see full session context."
        ),
    }, indent=2)


@mcp.tool()
def list_playbooks() -> str:
    """
    LIST PLAYBOOKS — Show all available production playbooks with their key standards.

    Use this when you need to know which playbook to activate, or when the user
    asks what playbooks are available.
    """
    summary = {}
    for key, pb in _PLAYBOOKS.items():
        summary[key] = {
            "name":        pb["name"],
            "description": pb["description"],
            "vert_budget": pb["vert_budget"],
            "material_limit": pb["material_limit"],
            "mandatory_checks": pb["mandatory_checks"],
        }
    active = _session_get("active_playbook")
    return json.dumps({
        "active_playbook": active,
        "available_playbooks": summary,
        "how_to_activate": "Call set_playbook(playbook='hero_char') to activate a playbook.",
    }, indent=2)


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
def detect_mesh_problems(name: str, verbose: bool = False) -> str:
    """
    Detect common mesh problems (non-manifold geometry, loose vertices,
    zero-area faces, duplicate faces, inverted normals) on a named object.

    v2.2: Each problem explained with production impact, professional fix,
    and whether it can be auto-repaired by auto_repair_mesh.
    verbose: False (default) — returns only problems found (count > 0).
             True — returns full reasoning block including clean checks.
    """
    raw = _send_raw("detect_mesh_problems", name=name)
    if "error" in raw:
        return json.dumps(raw, indent=2)
    enriched = _reason_mesh_problems(raw)
    if not verbose:
        reasoning = enriched.get("_reasoning", {})
        # Keep only findings that have actual problems
        slim = {
            "object":          name,
            "overall_severity": reasoning.get("overall_severity", "pass"),
            # FIX: "problem_count" lives on the raw-spread top level of `enriched`
            # (from addon.py's detect_mesh_problems response), not inside the
            # nested "_reasoning" dict — reasoning.get("problem_count") always
            # silently returned 0 regardless of the real count.
            "problem_count":   enriched.get("problem_count", 0),
            "findings":        reasoning.get("findings", []),
            "auto_repairable": reasoning.get("auto_repairable", []),
            "needs_artist_review": reasoning.get("needs_artist_review", []),
            "_tip": "Pass verbose=True for full reasoning block.",
        }
        return json.dumps(slim, indent=2, default=str)
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
def run_asset_qa(name: str, check_uvs: bool = True, check_materials: bool = True, check_modifiers: bool = True, verbose: bool = False) -> str:
    """
    Run a production QA pass on a named object: UVs, materials, modifiers,
    weight paint, duplicate faces, and other readiness checks.

    v2.2: Response enriched with blocking vs advisory categorisation
    and professional fix guidance from the reasoning engine.
    verbose: False (default) — verdict + blocking issues only.
             True — full reasoning block.
    """
    raw = _send_raw("run_asset_qa", name=name, check_uvs=check_uvs, check_materials=check_materials, check_modifiers=check_modifiers)
    if "error" in raw:
        return json.dumps(raw, indent=2)
    enriched = _reason_asset_qa(raw)
    if not verbose:
        reasoning = enriched.get("_reasoning", {})
        # FIX: _reason_asset_qa's "_reasoning" has no "blocking"/"advisory" keys
        # at all — only "findings" (a flat list with per-item "severity"). The
        # old code silently returned [] for both regardless of real findings.
        findings = reasoning.get("findings", [])
        slim = {
            "object":   name,
            "verdict":  enriched.get("verdict", reasoning.get("overall_severity", "unknown")),
            "blocking": [f for f in findings if f.get("severity") == "critical"],
            "advisory": [f for f in findings if f.get("severity") == "warning"],
            "_tip":     "Pass verbose=True for full reasoning block.",
        }
        return json.dumps(slim, indent=2, default=str)
    return json.dumps(enriched, indent=2, default=str)


@mcp.tool()
def run_unreal_readiness_check(name: str, expected_unit_scale: float = 0.01, verbose: bool = False) -> str:
    """
    Check whether a named object is ready for Unreal Engine 5 import.
    Validates scale, pivot, naming, UVs, lightmap UV, collision, and normal map direction.

    v2.2: Each failed check explained with UE5 pipeline context, severity,
    and specific fix instructions from the reasoning engine.
    verbose: False (default) — blocking errors + warnings only.
             True — full reasoning block including all passing checks.
    """
    raw = _send_raw("run_unreal_readiness_check", name=name, expected_unit_scale=expected_unit_scale)
    if "error" in raw:
        return json.dumps(raw, indent=2)
    enriched = _reason_unreal_readiness(raw)
    if not verbose:
        reasoning = enriched.get("_reasoning", {})
        slim = {
            "object":         name,
            # FIX: real key is "overall_severity", not "overall" — always fell
            # through to the "unknown" default before.
            "overall":        reasoning.get("overall_severity", "unknown"),
            "blocking_errors": enriched.get("blocking_errors", 0),
            "warnings":       enriched.get("warnings", 0),
            "findings":       [f for f in reasoning.get("findings", [])
                               if f.get("severity") in ("critical", "warning")],
            "_tip":           "Pass verbose=True for full reasoning block.",
        }
        return json.dumps(slim, indent=2, default=str)
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
def analyze_mesh_for_unreal(name: str, topology_context: str = "generic", verbose: bool = False) -> str:
    """
    COMPOUND TOOL — Full pre-export analysis in one call.

    Runs detect_mesh_problems + get_mesh_quality_report + analyze_topology +
    run_unreal_readiness_check simultaneously, then combines all findings
    into a single prioritised report with professional fix guidance.

    Use this as the first step before any UE5 export workflow.

    topology_context: 'generic' | 'character_body' | 'face' | 'hand' | 'hard_surface'
    verbose: False (default) — returns verdict + failing/warning findings only.
             True — returns full analysis including all passing checks and raw
             reasoning blocks. Use when you need the complete picture.
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

        # ── Infer asset context for budget assumption statement ────────────────
        vert_count = raw_quality.get("counts", {}).get("verts", 0) or 0
        has_arm    = any(
            m.get("type") == "ARMATURE"
            for m in raw_quality.get("modifiers", [])
            if isinstance(m, dict)
        )
        has_mat    = bool(r_ue5.get("_reasoning", {}).get("findings"))  # rough proxy
        has_uv     = raw_quality.get("uv", {}).get("has_uvs", False)

        if vert_count > 300_000:
            assumed_tier = "high-poly sculpt or scan source"
            budget_note  = "No polygon budget applies — this is a source mesh, not a runtime asset."
        elif has_arm and vert_count > 50_000:
            assumed_tier = "hero or main character (rigged)"
            budget_note  = f"Evaluating at hero-character standards. {vert_count:,} verts is within typical range for a hero character (40k–80k). Correct me if this is a background character or NPC — budget expectations differ significantly."
        elif has_arm:
            assumed_tier = "character asset (rigged)"
            budget_note  = f"Evaluating as a rigged character. {vert_count:,} verts. Tell me if this is a hero, enemy, NPC, or crowd character — each has a different acceptable budget."
        elif vert_count < 5_000:
            assumed_tier = "small prop or environment detail"
            budget_note  = f"{vert_count:,} verts — low-poly asset, evaluating as a prop or environment detail piece."
        elif vert_count < 30_000:
            assumed_tier = "mid-complexity prop or weapon"
            budget_note  = f"{vert_count:,} verts — evaluating as a mid-complexity prop or weapon. Correct me if this is a character or hero asset."
        else:
            assumed_tier = "game asset (unknown tier)"
            budget_note  = f"{vert_count:,} verts. I don't have enough context to assign a budget tier. Tell me what this asset is and who it's for — hero character, NPC, prop, environment piece — and I'll give you a verdict calibrated to that standard."

        # ── Build report — slim by default, full when verbose=True ───────────
        report = {
            "object":          name,
            "assumed_context": assumed_tier,
            "correct_me": (
                f"I'm evaluating this as: {assumed_tier}. "
                "If the asset type or target is different, say so and I'll re-evaluate."
            ),
            "verdict":         verdict,
            "overall_severity": overall,
            "summary": (
                f"{verdict} — {len(critical)} blocking error(s), "
                f"{len(warnings)} warning(s), {len(info)} info item(s). "
                f"{len(auto_fixable_all)} issue(s) can be auto-repaired via auto_repair_mesh."
            ),
            "action_required":        len(critical) > 0 or len(warnings) > 0,
            "auto_repair_available":  len(auto_fixable_all) > 0,
            "auto_repairable_issues": auto_fixable_all,
            "critical_errors": critical,
            "warnings":        warnings,
        }

        if verbose:
            # Full output — all findings + raw reasoning blocks
            report["assumed_context_note"] = budget_note
            report["info"]                 = info
            report["full_analysis"] = {
                "mesh_problems":    r_problems.get("_reasoning", {}),
                "mesh_quality":     r_quality.get("_reasoning", {}),
                "topology":         r_topology.get("_reasoning", {}),
                "unreal_readiness": r_ue5.get("_reasoning", {}),
            }
        else:
            # Slim output — omit info findings, raw reasoning blocks, budget prose
            # Re-add budget note only when something is wrong (user needs context)
            if critical or warnings:
                report["assumed_context_note"] = budget_note
            report["_tip"] = "Pass verbose=True for full analysis including info findings and raw reasoning."

        return json.dumps(report, indent=2, default=str)

    except Exception as e:
        logger.error(f"Error in analyze_mesh_for_unreal: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def auto_repair_mesh(name: str, dry_run: bool = False) -> str:
    """
    AUTO-REPAIR — Safe mesh cleanup loop: Scan → Diagnose → Repair → Verify.

    Automatically fixes the following problems (in safe order):
      1. Non-manifold edges — three passes:
           a) merge by distance (0.0001m) — eliminates coincident verts that
              create T-junctions and overlapping-face non-manifolds
           b) delete interior faces — faces enclosed by other faces cause edges
              shared by 3+ faces
           c) dissolve wire edges — stray edges with no face leave non-manifold verts
         Reports before/after count. Surviving non-manifolds (complex interior
         topology) are flagged for artist review.
      2. Loose vertices — delete isolated verts not connected to any edge
      3. Duplicate faces — merge by distance removes overlapping geometry
      4. Zero-area/degenerate faces — dissolve degenerate (threshold 0.0001m)
      5. Inverted normals — recalculate outside (run last, after all geometry fixed)

    Problems NOT auto-repaired (require artist review):
      - Non-manifold edges that survive all three passes (complex interior topology)
      - UV overlaps (may be intentional tiling)
      - N-gons (topology restructuring needed — auto-triangulate would break edge flow)

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


@mcp.tool()
def animation_coach(
    name: str,
    frame_start: Optional[int] = None,
    frame_end: Optional[int] = None,
    focus: str = "all",
) -> str:
    """
    AI ANIMATION COACH — Frame-specific motion quality feedback.

    Builds on critique_animation by adding production-principles coaching:
    contact timing, weight transfer arcs, anticipation, follow-through,
    and pose-to-pose readability. Each finding includes the production
    principle being violated, the typical frame range where it shows, and
    a correction method.

    Different from critique_animation: that tool tells you what's wrong with
    the data. This tool tells you *why it looks wrong* to an animator's eye,
    and what principle to apply to fix it.

    In Apprentice Mode (session_update(apprentice_mode=True)), every finding
    also includes an animation principles lesson so newer artists understand
    the 'why' behind each correction.

    Parameters:
      name        : armature or mesh object with animation
      frame_start : first frame to evaluate (defaults to action start)
      frame_end   : last frame to evaluate (defaults to action end)
      focus       : "all" (default) | "timing" | "arcs" | "weight" |
                    "contact" | "follow_through"

    Returns:
      coaching_verdict   : overall assessment with animation-principles framing
      frame_findings     : findings with frame references and principle citations
      timing_notes       : contact timing and rhythmic issues
      arc_notes          : arc quality and trajectory smoothness
      weight_notes       : weight transfer, anticipation, follow-through
      apprentice_lessons : present if apprentice_mode is True
    """
    try:
        # ── Get base animation data ───────────────────────────────────────────
        raw = _send_raw("analyze_animation_quality", name=name,
                        frame_start=frame_start, frame_end=frame_end)
        if "error" in raw:
            return json.dumps(raw, indent=2)

        enriched  = _reason_animation(raw)
        reasoning = enriched.get("_reasoning", {})
        findings  = reasoning.get("findings", [])

        # FIX: addon.py's analyze_animation_quality returns "frame_range": [start, end],
        # not separate "frame_start"/"frame_end" keys — those always defaulted to 0
        # whenever the caller didn't explicitly pass frame_start/frame_end (the common
        # case, since the docstring says both "default to action start/end").
        # There's also no "fps" anywhere in the real schema, or in any addon.py
        # command — omit it rather than presenting a fabricated constant as real data.
        raw_frame_range = raw.get("frame_range", [0, 0])
        anim_start  = frame_start or (raw_frame_range[0] if len(raw_frame_range) > 0 else 0)
        anim_end    = frame_end   or (raw_frame_range[1] if len(raw_frame_range) > 1 else 0)
        frame_range = (anim_end - anim_start) + 1 if anim_end > anim_start else 0

        # ── Animation principles catalog ──────────────────────────────────────
        # Keyed by issue category → coaching note
        PRINCIPLES = {
            "timing": {
                "principle":    "Timing & Spacing",
                "definition":   "The number of frames between key poses determines the feeling of speed and weight. More frames = slower, heavier. Fewer frames = faster, lighter.",
                "common_error": "Even spacing between keys produces mechanical, robotic motion. Vary spacing to show acceleration and deceleration.",
                "correction":   "Use the Graph Editor to add ease-in and ease-out. Slow into holds, fast through impacts.",
            },
            "arcs": {
                "principle":    "Arcs",
                "definition":   "All natural motion follows curved paths. Linear trajectories look mechanical because nothing in nature moves in straight lines.",
                "common_error": "Straight-line translation of a limb end-point even when the intermediate pose travels in an arc.",
                "correction":   "In the viewport, enable Onion Skins (Animation > Onion Skin). Check that trajectory curves, especially for hands and head.",
            },
            "weight": {
                "principle":    "Weight & Follow-Through",
                "definition":   "Heavy objects take longer to start and stop. Secondary elements (hair, clothing, tail) lag behind primary motion.",
                "common_error": "Body and appendages stop at the same frame. No follow-through after primary motion stops.",
                "correction":   "After the primary pose holds, secondary elements should continue for 3–8 more frames, then settle.",
            },
            "contact": {
                "principle":    "Contact & Overlap",
                "definition":   "At contact moments (foot strike, hand grab), the contacted surface must visibly react. The body absorbs shock through squash.",
                "common_error": "Foot plants but hips continue downward with no weight shift. The body doesn't 'feel' the contact.",
                "correction":   "At foot contact frame: hip should start a slight down-and-forward lean. Knee should not lock immediately — allow 2–3 frames of compress before settling.",
            },
            "anticipation": {
                "principle":    "Anticipation",
                "definition":   "Before any large action, the character moves briefly in the opposite direction. This prepares the audience and adds energy to the main action.",
                "common_error": "Jump or large movement starts from neutral without any wind-up. Feels robotic and weightless.",
                "correction":   "Add a pre-movement in the opposite direction: 3–6 frames for small actions, 8–12 for large ones. The bigger the action, the bigger the anticipation.",
            },
            "follow_through": {
                "principle":    "Follow-Through & Overlapping Action",
                "definition":   "No part of a character stops moving all at once. After the main action, secondary elements continue, overlap, and settle.",
                "common_error": "Everything stops at the same frame. Feels frozen rather than settled.",
                "correction":   "Stagger stops: spine settles first, then limbs, then extremities, then cloth/hair. Each should overlap the previous by 2–4 frames.",
            },
        }

        # ── Frame-specific findings ───────────────────────────────────────────
        frame_findings = []
        timing_notes   = []
        arc_notes      = []
        weight_notes   = []

        for f in findings:
            cat         = f.get("category", "general")
            issue       = f.get("issue", "")
            sev         = f.get("severity", "info")
            # FIX: individual findings have no "frame"/"frames" key in the real
            # schema (only issue/severity/category/why_it_matters/professional_fix),
            # so this always fell through to "N/A". addon.py does embed frame
            # numbers in the issue text itself for some findings (e.g. "between
            # frames 14-18") — extract them with a regex instead of a dead lookup.
            frame_match = re.search(r"frames?\s+(\d+)(?:\s*[-–]\s*(\d+))?", issue, re.IGNORECASE)
            if frame_match:
                frame_ref = (f"{frame_match.group(1)}-{frame_match.group(2)}"
                             if frame_match.group(2) else frame_match.group(1))
            else:
                frame_ref = "N/A"
            fix         = f.get("professional_fix", "") or f.get("correction", "") or f.get("fix", "")

            # Map category to principle
            principle_key = None
            if "timing" in cat.lower() or "speed" in issue.lower():
                principle_key = "timing"
                timing_notes.append(issue)
            elif "arc" in cat.lower() or "trajectory" in issue.lower():
                principle_key = "arcs"
                arc_notes.append(issue)
            elif "weight" in cat.lower() or "follow" in issue.lower():
                principle_key = "weight"
                weight_notes.append(issue)
            elif "contact" in cat.lower() or "foot" in issue.lower() or "plant" in issue.lower():
                principle_key = "contact"
                weight_notes.append(issue)
            elif "anticipat" in cat.lower():
                principle_key = "anticipation"
                weight_notes.append(issue)

            entry = {
                "severity":  sev,
                "issue":     issue,
                "frame_ref": frame_ref,
                "principle": PRINCIPLES[principle_key]["principle"] if principle_key else "General quality",
                "correction": fix or (PRINCIPLES[principle_key]["correction"] if principle_key else "Review manually."),
            }

            # Focus filter
            if focus != "all":
                if focus == "timing"        and principle_key != "timing":       continue
                if focus == "arcs"          and principle_key != "arcs":         continue
                if focus == "weight"        and principle_key not in ("weight", "contact", "follow_through"): continue
                if focus == "contact"       and principle_key != "contact":      continue
                if focus == "follow_through" and principle_key != "follow_through": continue

            frame_findings.append(entry)

        # ── Overall coaching verdict ──────────────────────────────────────────
        score   = reasoning.get("score", 0)
        grade   = reasoning.get("grade", "?")
        criticals = [f for f in findings if f.get("severity") == "critical"]
        warnings  = [f for f in findings if f.get("severity") == "warning"]

        if not criticals and not warnings:
            coaching_verdict = (
                f"Grade {grade} ({score}/100). This animation is production-ready. "
                f"The fundamentals are solid — timing reads correctly, no mechanical artifacts detected. "
                f"Polish pass: look for any secondary motion that could add life."
            )
        elif criticals:
            coaching_verdict = (
                f"Grade {grade} ({score}/100). {len(criticals)} critical issue(s) will read as wrong to any viewer. "
                f"These aren't subtle polish items — they're animation principles violations that break believability. "
                f"Address criticals first before any polish work."
            )
        else:
            coaching_verdict = (
                f"Grade {grade} ({score}/100). Structurally sound with {len(warnings)} warning(s). "
                f"The character reads correctly but experienced animators will notice the issues listed. "
                f"These are worth fixing before delivery."
            )

        # ── Apprentice lessons (session-gated) ────────────────────────────────
        apprentice_mode   = _session_get("apprentice_mode") or False
        apprentice_lessons = []
        if apprentice_mode:
            principles_used = set()
            for f in frame_findings:
                principle_name = f.get("principle", "")
                for k, v in PRINCIPLES.items():
                    if v["principle"] == principle_name and k not in principles_used:
                        apprentice_lessons.append({
                            "principle":    v["principle"],
                            "definition":   v["definition"],
                            "common_error": v["common_error"],
                            "correction":   v["correction"],
                        })
                        principles_used.add(k)

        # ── Build report ──────────────────────────────────────────────────────
        report = {
            "object":           name,
            "frame_range":      {"start": anim_start, "end": anim_end, "total_frames": frame_range},
            "coaching_verdict": coaching_verdict,
            "grade":            grade,
            "score":            score,
            "frame_findings":   frame_findings,
        }
        if timing_notes:
            report["timing_notes"] = timing_notes
        if arc_notes:
            report["arc_notes"] = arc_notes
        if weight_notes:
            report["weight_notes"] = weight_notes
        if apprentice_lessons:
            report["apprentice_lessons"] = apprentice_lessons
            report["apprentice_mode"] = True

        _session_append("verified_checks", "animation_coach")
        return json.dumps(report, indent=2, default=str)

    except Exception as e:
        logger.error(f"Error in animation_coach: {e}")
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
            "has_armature":   any(m.get("type") == "ARMATURE"
                                 for m in mesh_stats.get("modifiers", [])
                                 if isinstance(m, dict)),
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
def what_next(object_name: str, context: str = "") -> str:
    """
    PRIORITY ACTION — answers "what is the single most important thing to do right now?"

    Looks at the asset's current state and returns ONE action: the highest-leverage
    step that unblocks the most pipeline progress. Not a plan. Not a list. One thing.

    Collects: object info, mesh quality, mesh problems, pipeline stage.
    Applies a priority decision tree calibrated to the inferred stage.
    States its assumptions about asset purpose explicitly — correct it if wrong.

    Parameters:
      object_name : the Blender object to evaluate
      context     : optional one-line hint about the asset's purpose or target
                    e.g. "hero character for UE5" or "background prop" or "weapon"
                    Leave empty and the tool will state its own assumption.

    Returns:
      - assumed_context : what the tool is assuming about this asset
      - stage           : inferred pipeline stage
      - action          : the one thing to do right now
      - why             : why this is the priority over everything else
      - how             : which tool or method executes this action
      - blocking_count  : how many issues this unblocks downstream
      - after_this      : what becomes the next priority once this is done
      - correct_me      : prompt for the user to correct any wrong assumption
    """
    try:
        # ── Gather all data in parallel ────────────────────────────────────────
        obj_info   = _send_raw("get_object_info",         name=object_name)
        mesh_stats = _send_raw("get_mesh_quality_report", name=object_name)
        problems   = _send_raw("detect_mesh_problems",    name=object_name)

        if "error" in obj_info:
            return json.dumps({"error": f"get_object_info failed: {obj_info['error']}"})
        if "error" in mesh_stats:
            return json.dumps({"error": f"get_mesh_quality_report failed: {mesh_stats['error']}"})

        # ── Extract key signals ────────────────────────────────────────────────
        mesh_block     = obj_info.get("mesh", {})
        vertex_count   = mesh_block.get("vertices", 0) or 0
        face_count     = mesh_block.get("polygons",  0) or 0
        mat_list       = obj_info.get("materials", [])
        has_materials  = bool(mat_list)
        material_count = len(mat_list) if isinstance(mat_list, list) else 0

        uv_data        = mesh_stats.get("uv", {})
        has_uvs        = uv_data.get("has_uvs", False)
        uv_layers      = uv_data.get("layer_count", 0)
        health         = mesh_stats.get("health", "unknown")
        face_types     = mesh_stats.get("face_types", {})
        ngon_count     = face_types.get("ngons", 0) or 0
        mod_list       = mesh_stats.get("modifiers", [])
        modifier_types = [m.get("type", "") for m in mod_list if isinstance(m, dict)]
        has_armature   = "ARMATURE" in modifier_types
        has_multires   = "MULTIRES" in modifier_types

        # Problem counts from detect_mesh_problems (list schema)
        prob_list = problems.get("problems", []) if "error" not in problems else []
        prob_map  = {p.get("type", ""): p.get("count", 0) for p in prob_list}
        nm_edges  = prob_map.get("non_manifold_edges", 0)
        iso_verts = prob_map.get("isolated_verts",     0)
        zero_area = prob_map.get("zero_area_faces",    0)
        dup_faces = prob_map.get("duplicate_faces",    0)
        bd_edges  = prob_map.get("boundary_edges",     0)

        # ── Infer stage ────────────────────────────────────────────────────────
        stage_result = _classify_stage_from_signals(obj_info, mesh_stats)
        stage_num    = stage_result.get("stage_number", 0)
        stage_name   = stage_result.get("stage_name",   "Unknown")
        confidence   = stage_result.get("confidence",   "low")

        # ── Infer asset context (state assumption, invite correction) ──────────
        if context:
            assumed_context = context
        else:
            # Derive a plain-language assumption from signals
            if vertex_count > 300_000:
                assumed_context = "high-poly sculpt or scan source mesh"
            elif has_armature and has_materials and has_uvs:
                assumed_context = "rigged character asset"
            elif has_armature:
                assumed_context = "character asset (rigged, materials/UVs incomplete)"
            elif has_materials and has_uvs and vertex_count < 100_000:
                assumed_context = "game-ready prop or character"
            elif has_uvs and not has_materials and vertex_count < 100_000:
                assumed_context = "low-poly mesh ready for texturing"
            elif not has_uvs and vertex_count < 120_000:
                assumed_context = "retopo or base mesh (no UVs yet)"
            else:
                assumed_context = "game asset at an undetermined stage"

        # ── Priority decision tree ─────────────────────────────────────────────
        # Order of priority: blocking errors > missing stage requirements > next stage gate
        # Each entry: (condition, action, why, how, blocking_count, after_this)

        action       = None
        why          = None
        how          = None
        blocking_ct  = 0
        after_this   = None

        # PRIORITY 1 — Non-manifold edges (blocks export, baking, subdivision)
        if nm_edges > 0:
            action      = f"Fix {nm_edges} non-manifold edge(s)"
            why         = (
                f"{nm_edges} non-manifold edge(s) are present. This is a hard blocker: "
                f"UE5 will reject or misimport this mesh, normal baking will produce "
                f"incorrect results, and subdivision modifiers will fail. Everything "
                f"else — UVs, materials, rigging — is wasted work if this isn't fixed first."
            )
            how         = "auto_repair_mesh() — three-pass repair (merge by distance, interior face deletion, wire edge dissolve). Verify remaining count after."
            blocking_ct = nm_edges
            after_this  = "Re-run what_next() — non-manifold repair may expose other issues."

        # PRIORITY 2 — Other auto-repairable geometry errors
        elif zero_area > 0 or dup_faces > 0 or iso_verts > 0:
            issues = []
            if zero_area  > 0: issues.append(f"{zero_area} zero-area face(s)")
            if dup_faces  > 0: issues.append(f"{dup_faces} duplicate face(s)")
            if iso_verts  > 0: issues.append(f"{iso_verts} isolated vertex/vertices")
            action      = f"Run auto-repair: {', '.join(issues)}"
            why         = (
                f"Geometry errors present that auto_repair_mesh can fix safely. "
                f"These cause undefined normals, z-fighting, and inflated vertex counts. "
                f"They take seconds to fix automatically and should never go to the next stage."
            )
            how         = "auto_repair_mesh() — safe, undo-checkpointed, verifies after."
            blocking_ct = sum([zero_area, dup_faces, iso_verts])
            after_this  = "Check mesh health then continue to the next stage requirement."

        # PRIORITY 3 — Stage 1: sculpt has no UV, no material — next gate is retopo
        elif stage_num == 1:
            action      = "Begin retopology — create a game-ready low-poly mesh"
            why         = (
                f"This mesh has {vertex_count:,} vertices and reads as a sculpt/high-poly "
                f"source. It is not suitable as a runtime asset. The next required step is "
                f"retopology to a game mesh, which will then receive UVs, baking, and materials."
            )
            how         = "Create new mesh object and retopo manually, or use Blender's Remesh modifier as a starting point. Target vertex count depends on asset type — correct my assumption if needed."
            blocking_ct = 0
            after_this  = "UV unwrap the retopo mesh, then bake normal/AO maps from this high-poly source."

        # PRIORITY 4 — Stage 2: retopo present but no UVs
        elif stage_num == 2 and not has_uvs:
            action      = "UV unwrap this mesh"
            why         = (
                f"Topology looks game-ready ({vertex_count:,} verts, quad-dominant) "
                f"but no UV map exists. UVs are required before baking and before "
                f"any material work. Nothing downstream can proceed without them."
            )
            how         = "Blender UV Editor — mark seams, Unwrap (U). Check for stretching in the UV editor. Use Smart UV Project as a starting point on hard-surface assets."
            blocking_ct = 0
            after_this  = "Set up high-poly bake source, then bake normal/AO/curvature maps."

        # PRIORITY 5 — Stage 3: UVs exist but no materials (bake-ready state)
        elif stage_num == 3 and has_uvs and not has_materials:
            action      = "Bake maps and create PBR material"
            why         = (
                f"UVs are present and the mesh is in bake-ready state — no materials yet. "
                f"The correct next step is to bake normal/AO/curvature maps from a high-poly "
                f"source (if one exists) and set up a Principled BSDF material with those maps."
            )
            how         = "Blender Cycles bake (normal, AO, curvature). Then create_pbr_material() to set up the Principled BSDF node graph."
            blocking_ct = 0
            after_this  = "Validate PBR material with analyze_material_pbr(), then check Unreal readiness."

        # PRIORITY 6 — Stage 4: materials exist but PBR not validated
        elif stage_num == 4 and has_materials and not has_armature:
            action      = "Validate PBR materials and check Unreal readiness"
            why         = (
                f"{material_count} material(s) assigned. Before this asset moves to export "
                f"or rigging, the material setup needs validation — broken texture paths, "
                f"procedural-only nodes, and incorrect normal map direction all cause "
                f"silent failures in UE5."
            )
            how         = "analyze_material_pbr() for full PBR review, then run_unreal_readiness_check() for export gate."
            blocking_ct = material_count
            after_this  = "If materials pass, move to rigging setup or direct export depending on asset type."

        # PRIORITY 7 — Stage 5: rigged — run rig QA first, then export gate
        elif stage_num == 5 and has_armature:
            action      = "Run rig QA: check weights and skeleton before export gate"
            why         = (
                f"Armature modifier detected — this is a rigged asset at Stage 5. "
                f"Before running the export gate, rig weight and skeleton quality must "
                f"be confirmed. Unweighted vertices snap to world origin at runtime. "
                f"Over-influence vertices (>8 groups) are silently truncated by UE5. "
                f"A mis-placed root bone causes animation drift in engine. "
                f"These failures are invisible until first pose frame in engine — "
                f"catch them here."
            )
            how         = (
                "Step 1: analyze_rig_weights(object_name) — unweighted verts, "
                "influence count, zero-weight assignments. "
                "Step 2: analyze_rig_skeleton(object_name) — root bone position, "
                "orphan bones, bone count, UE5 naming. "
                "Step 3 (if both pass): run_unreal_readiness_check() then "
                "analyze_mesh_for_unreal() for full export gate."
            )
            blocking_ct = 0
            after_this  = (
                "If rig QA passes, run run_unreal_readiness_check() — "
                "the full export gate (scale, pivot, modifiers, UE5 conventions)."
            )

        # PRIORITY 8 — Stage 6 or clean mesh with materials: run full readiness check
        elif stage_num == 6 or (has_uvs and has_materials and health == "clean"):
            action      = "Run full Unreal readiness check"
            why         = (
                f"This mesh has UVs, materials, and appears clean — it looks export-ready. "
                f"Run the full readiness gate to confirm before export. Scale, pivot, "
                f"triangulation, and lightmap UV requirements must all be verified."
            )
            how         = "analyze_mesh_for_unreal() — runs all four checks in one call and gives a structured verdict."
            blocking_ct = 0
            after_this  = "If all checks pass, export_for_unreal(). If not, address each blocking issue in order."

        # PRIORITY 9 — Ngons present (non-critical but stage-gated)
        elif ngon_count > 0 and stage_num in (2, 3, 5, 6):
            action      = f"Address {ngon_count} n-gon(s) in the mesh"
            why         = (
                f"{ngon_count} n-gon face(s) detected. At Stage {stage_num} ({stage_name}), "
                f"n-gons are a risk: UE5 auto-triangulation produces star patterns and "
                f"shading errors, and subdivision modifiers pinch at n-gon boundaries. "
                f"In deforming areas this will cause visible skinning artefacts."
            )
            how         = "Edit Mode > Select All by Trait > Face Sides (>4). Dissolve edges and re-route topology using quads. Cannot be auto-repaired — requires artist judgment on edge flow."
            blocking_ct = ngon_count
            after_this  = "Re-run analyze_topology() to confirm quad dominance before proceeding."

        # PRIORITY 10 — No issues found, state confidence
        else:
            action      = "No critical actions required at this stage"
            why         = (
                f"No blocking issues detected for Stage {stage_num} ({stage_name}). "
                f"Mesh health: {health}. "
                f"This assessment is based on data signals — run analyze_mesh_for_unreal() "
                f"for a comprehensive pre-export verification before treating this as done."
            )
            how         = "analyze_mesh_for_unreal() for full report if approaching export."
            blocking_ct = 0
            after_this  = "Proceed to the next stage requirement when ready."

        # ── Playbook context ───────────────────────────────────────────────────
        pb = _get_active_playbook()
        playbook_block = None
        playbook_conflicts = []
        if pb:
            pb_name = pb["name"]
            pb_vert = pb["vert_budget"]
            # Vertex budget conflict check — state it, don't resolve silently
            if vertex_count > pb_vert:
                ratio = vertex_count / pb_vert
                playbook_conflicts.append(
                    f"Vertex count ({vertex_count:,}) is {ratio:.1f}× the {pb_name} "
                    f"budget ({pb_vert:,}). Is this intentional (cinematic tier) "
                    f"or should the playbook be changed?"
                )
            # Stage-specific standard for current stage
            stage_standard = pb.get("stage_standards", {}).get(stage_num, "")
            # Gotchas (always surface — they're the ones that burn people)
            playbook_block = {
                "active_playbook":    pb_name,
                "vert_budget":        pb_vert,
                "mandatory_checks":   pb["mandatory_checks"],
                "skip_checks":        pb["skip_checks"],
                "stage_standard":     stage_standard,
                "gotchas":            pb["gotchas"],
            }

        # ── Session context hint ───────────────────────────────────────────────
        verified = _session_get("verified_checks") or []
        open_iss = _session_get("open_issues") or []

        result = {
            "object":          object_name,
            "assumed_context": assumed_context,
            "correct_me":      (
                f"I'm evaluating this as: {assumed_context}. "
                f"If the asset type, target platform, or usage is different, "
                f"tell me and I'll re-evaluate with the correct standards."
            ),
            "stage": {
                "number":     stage_num,
                "name":       stage_name,
                "confidence": confidence,
            },
            "action":         action,
            "why":            why,
            "how":            how,
            "blocking_count": blocking_ct,
            "after_this":     after_this,
            "asset_snapshot": {
                "vertices":       vertex_count,
                "faces":          face_count,
                "has_uvs":        has_uvs,
                "uv_layers":      uv_layers,
                "materials":      material_count,
                "has_armature":   has_armature,
                "mesh_health":    health,
                "nm_edges":       nm_edges,
                "ngons":          ngon_count,
            },
        }
        if playbook_block:
            result["playbook"] = playbook_block
        if playbook_conflicts:
            result["playbook_conflicts"] = playbook_conflicts
        if verified:
            result["session_verified_checks"] = verified
        if open_iss:
            result["session_open_issues"] = open_iss

        return json.dumps(result, indent=2, default=str)

    except Exception as e:
        logger.error(f"Error in what_next: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def analyze_rig_weights(object_name: str, verbose: bool = False) -> str:
    """
    RIG WEIGHT QA — checks vertex group weights for catastrophic skinning failures.

    Runs three checks on the mesh's vertex groups (deformation weights):

      CRITICAL — Unweighted vertices
        Vertices assigned to NO group at all. At runtime these snap to the world
        origin on first pose frame. Even one unweighted vertex is a hard failure.

      CRITICAL — Over-influence vertices (>8 groups)
        UE5 hard-truncates to 8 influences per vertex. Excess influences are silently
        discarded, causing unpredictable deformation. Sampled across first 1000 verts.

      WARNING  — Zero-weight assignments
        Vertex assigned to a group with weight 0.0. These cost memory/CPU in the
        vertex shader and indicate painting errors. Not a hard failure but should be
        cleaned before export.

    Parameters:
      object_name : the MESH object to inspect (not the armature)

    Returns:
      checks       : list of {check, severity, count, detail}
      summary      : plain-English verdict
      verdict      : CRITICAL | WARNING | PASS
      group_count  : number of vertex groups on the object
      vertex_count : total vertices inspected
    """
    try:
        # Build inspection script to run inside Blender
        script = r"""
import bpy
import json

obj = bpy.data.objects.get('{OBJECT_NAME}')
if obj is None:
    print(json.dumps({"error": "Object not found: {OBJECT_NAME}"}))
else:
    mesh = obj.data
    groups = obj.vertex_groups
    group_count = len(groups)
    total_verts = len(mesh.vertices)

    unweighted = []       # verts with NO group assignment
    over_influence = []   # verts with >8 group assignments
    zero_weight = []      # (vert_idx, group_name) with weight == 0.0

    # Sample cap for influence check — 1000 verts for performance
    sample_cap = 1000

    for i, v in enumerate(mesh.vertices):
        assignments = v.groups  # BGeoVertexGroupElement list
        n_assigned = len(assignments)

        if n_assigned == 0:
            unweighted.append(i)
        else:
            # Zero-weight: any assignment with weight 0.0
            for g in assignments:
                if g.weight == 0.0:
                    gname = groups[g.group].name if g.group < len(groups) else f"grp_{g.group}"
                    zero_weight.append((i, gname))

        # Influence count only on first sample_cap verts
        if i < sample_cap and n_assigned > 8:
            over_influence.append({"vert": i, "influences": n_assigned})

    checks = []

    # CRITICAL: unweighted
    if unweighted:
        checks.append({
            "check":    "unweighted_vertices",
            "severity": "CRITICAL",
            "count":    len(unweighted),
            "detail":   (
                f"{len(unweighted)} vertex/vertices assigned to NO group. "
                f"Will snap to world origin at runtime. First 5 indices: "
                + str(unweighted[:5])
            ),
        })
    else:
        checks.append({
            "check":    "unweighted_vertices",
            "severity": "PASS",
            "count":    0,
            "detail":   "All vertices have at least one group assignment.",
        })

    # CRITICAL: over-influence (reported as sampled)
    if over_influence:
        checks.append({
            "check":    "over_influence_vertices",
            "severity": "CRITICAL",
            "count":    len(over_influence),
            "detail":   (
                f"{len(over_influence)} vertex/vertices exceed 8 influences "
                f"(UE5 hard limit) in first {min(sample_cap, total_verts)} sampled. "
                f"UE5 silently truncates extras causing unpredictable deformation. "
                f"Sample: " + str(over_influence[:3])
            ),
            "sampled":  min(sample_cap, total_verts),
        })
    else:
        checks.append({
            "check":    "over_influence_vertices",
            "severity": "PASS",
            "count":    0,
            "detail":   (
                f"No vertices exceed 8 influences in first "
                f"{min(sample_cap, total_verts)} sampled."
            ),
            "sampled":  min(sample_cap, total_verts),
        })

    # WARNING: zero-weight
    if zero_weight:
        # Collect unique groups involved
        zw_groups = list(dict.fromkeys(g for _, g in zero_weight))[:10]
        checks.append({
            "check":    "zero_weight_assignments",
            "severity": "WARNING",
            "count":    len(zero_weight),
            "detail":   (
                f"{len(zero_weight)} zero-weight assignment(s). "
                f"Groups involved (up to 10): {zw_groups}. "
                f"Clean with Weight Paint > Clean Weights."
            ),
        })
    else:
        checks.append({
            "check":    "zero_weight_assignments",
            "severity": "PASS",
            "count":    0,
            "detail":   "No zero-weight assignments found.",
        })

    # Overall verdict
    has_critical = any(c["severity"] == "CRITICAL" for c in checks)
    has_warning  = any(c["severity"] == "WARNING"  for c in checks)
    verdict      = "CRITICAL" if has_critical else "WARNING" if has_warning else "PASS"

    if has_critical:
        summary = (
            "Rig has CRITICAL weight failures. Fix before any export attempt. "
            "Unweighted vertices and/or over-influence vertices cause silent or "
            "catastrophic deformation failures in engine."
        )
    elif has_warning:
        summary = (
            "Rig weights are functional but have zero-weight assignments that "
            "should be cleaned before export. Not a hard blocker."
        )
    else:
        summary = (
            f"Rig weights appear clean across {total_verts} vertices "
            f"({group_count} group(s)). No catastrophic failures detected."
        )

    print(json.dumps({
        "verdict":      verdict,
        "summary":      summary,
        "group_count":  group_count,
        "vertex_count": total_verts,
        "checks":       checks,
    }))
""".replace("{OBJECT_NAME}", object_name.replace("'", "\\'"))

        # Run inside Blender via execute_code_safe
        blender = get_blender_connection()
        raw = blender.send_command("execute_code_safe", {"code": script})

        # Parse the JSON printed by the script.
        # FIX: execute_code_safe's captured stdout is under "result", not "output"
        # (addon.py execute_code returns {"executed": bool, "result": str} —
        # confirmed live this session, e.g. {"executed": true, "result": "...", ...}).
        output_text = raw.get("result", "") if isinstance(raw, dict) else str(raw)
        result = None
        for line in output_text.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    result = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue

        if result is None:
            return json.dumps({
                "error": "No JSON output from rig weight inspection script.",
                "raw_output": output_text[:500],
            })
        if "error" in result:
            return json.dumps(result)

        # Slim mode: strip PASS checks — only keep CRITICAL/WARNING findings
        if not verbose:
            all_checks = result.get("checks", [])
            result["checks"] = [
                c for c in all_checks
                if c.get("severity") != "PASS"
            ]
            result["passed_count"] = len(all_checks) - len(result["checks"])
            result["_tip"] = "Pass verbose=True to see passing checks too."

        return json.dumps(result, indent=2, default=str)

    except Exception as e:
        logger.error(f"Error in analyze_rig_weights: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def analyze_rig_skeleton(object_name: str, verbose: bool = False) -> str:
    """
    RIG SKELETON QA — inspects the armature linked to a mesh object.

    Finds the armature via the object's ARMATURE modifier and runs four checks:

      CRITICAL — Root bone not at world origin
        The root bone's head must be at or near (0, 0, 0). A mis-placed root means
        the skeleton is offset from the mesh in engine, causing animation drift.
        Threshold: 0.01 units in any axis.

      WARNING  — Orphan bones
        Bones with no corresponding vertex group anywhere in the scene's mesh
        objects. Orphan bones waste memory and may indicate naming mismatches
        between rig and weights. Pure control/IK bones are expected orphans —
        the check names them so you can confirm intentionality.

      INFO     — Bone count
        Total bone count. UE5 has no hard limit but >256 bones requires special
        handling. Reported for awareness.

      INFO     — UE5 naming conventions
        Common UE5 skeleton root names: "root", "Root", "pelvis", "Pelvis",
        "hips", "Hips". If none of the root-candidate bones match, reported as
        INFO (not FAIL — custom naming is valid, just requires manual mapping).

    Parameters:
      object_name : the MESH object to inspect (not the armature directly)

    Returns:
      armature_name : name of the linked armature object
      checks        : list of {check, severity, detail}
      summary       : plain-English verdict
      verdict       : CRITICAL | WARNING | INFO | PASS
      bone_count    : total bones in armature
    """
    try:
        script = r"""
import bpy
import json
import math

obj = bpy.data.objects.get('{OBJECT_NAME}')
if obj is None:
    print(json.dumps({"error": "Object not found: {OBJECT_NAME}"}))
else:
    # Find armature via ARMATURE modifier
    armature_obj = None
    for mod in obj.modifiers:
        if mod.type == 'ARMATURE' and mod.object is not None:
            armature_obj = mod.object
            break

    if armature_obj is None:
        print(json.dumps({
            "error": (
                "No ARMATURE modifier with a linked armature found on "
                "'{OBJECT_NAME}'. Is the armature modifier set up correctly?"
            )
        }))
    else:
        arm_data   = armature_obj.data
        bones      = arm_data.bones
        bone_count = len(bones)

        checks = []

        # ── CHECK 1: Root bone at world origin ──────────────────────────────
        # Find root bones (bones with no parent)
        root_bones = [b for b in bones if b.parent is None]
        origin_threshold = 0.01

        origin_fail = []
        for rb in root_bones:
            # head_local is in armature local space; apply armature world transform
            arm_world = armature_obj.matrix_world
            head_world = arm_world @ rb.head_local
            dist = math.sqrt(
                head_world.x**2 + head_world.y**2 + head_world.z**2
            )
            if dist > origin_threshold:
                origin_fail.append({
                    "bone":     rb.name,
                    "position": [round(head_world.x, 4),
                                 round(head_world.y, 4),
                                 round(head_world.z, 4)],
                    "distance": round(dist, 4),
                })

        if origin_fail:
            checks.append({
                "check":    "root_bone_at_origin",
                "severity": "CRITICAL",
                "detail":   (
                    f"{len(origin_fail)} root bone(s) not at world origin "
                    f"(threshold {origin_threshold} units). "
                    f"Skeleton will be offset from mesh in engine. "
                    f"Details: {origin_fail}"
                ),
            })
        else:
            root_names = [rb.name for rb in root_bones]
            checks.append({
                "check":    "root_bone_at_origin",
                "severity": "PASS",
                "detail":   (
                    f"Root bone(s) {root_names} are at or near world origin."
                ),
            })

        # ── CHECK 2: Orphan bones ───────────────────────────────────────────
        # Collect all vertex group names from all mesh objects that use this armature
        all_vg_names = set()
        for scene_obj in bpy.data.objects:
            if scene_obj.type != 'MESH':
                continue
            uses_this_arm = any(
                mod.type == 'ARMATURE' and mod.object == armature_obj
                for mod in scene_obj.modifiers
            )
            if uses_this_arm:
                for vg in scene_obj.vertex_groups:
                    all_vg_names.add(vg.name)

        orphan_bones = [
            b.name for b in bones if b.name not in all_vg_names
        ]

        if orphan_bones:
            checks.append({
                "check":    "orphan_bones",
                "severity": "WARNING",
                "count":    len(orphan_bones),
                "detail":   (
                    f"{len(orphan_bones)} bone(s) have no matching vertex group "
                    f"in any mesh that uses this armature. May be intentional "
                    f"control/IK bones — confirm intentionality. "
                    f"Names (up to 20): {orphan_bones[:20]}"
                ),
            })
        else:
            checks.append({
                "check":    "orphan_bones",
                "severity": "PASS",
                "count":    0,
                "detail":   "All bones have a corresponding vertex group.",
            })

        # ── CHECK 3: Bone count ─────────────────────────────────────────────
        if bone_count > 256:
            bc_note = (
                f"{bone_count} bones. Exceeds 256 — requires explicit "
                f"'Max Bones' setting in UE5 skeletal mesh import. "
                f"Not a hard fail but requires attention."
            )
        else:
            bc_note = f"{bone_count} bone(s). Within standard UE5 range (<256)."

        checks.append({
            "check":    "bone_count",
            "severity": "INFO",
            "count":    bone_count,
            "detail":   bc_note,
        })

        # ── CHECK 4: UE5 naming conventions ────────────────────────────────
        ue5_root_names = {
            "root", "Root", "pelvis", "Pelvis", "hips", "Hips",
            "ROOT", "PELVIS", "HIPS"
        }
        root_bone_names = {b.name for b in root_bones}
        all_bone_names  = {b.name for b in bones}

        has_ue5_root  = bool(root_bone_names & ue5_root_names)
        has_ue5_in_all = bool(all_bone_names & ue5_root_names)

        if not has_ue5_root and not has_ue5_in_all:
            naming_note = (
                f"No standard UE5 root bone name found "
                f"(expected: root, Root, pelvis, hips, etc.). "
                f"Root bone(s): {list(root_bone_names)[:5]}. "
                f"Custom naming is valid — requires manual bone mapping in UE5 import."
            )
            naming_sev = "INFO"
        elif not has_ue5_root and has_ue5_in_all:
            naming_note = (
                f"UE5-style name found in hierarchy but not at root level. "
                f"Root bone(s): {list(root_bone_names)[:5]}. "
                f"Confirm root is the correct top-level bone for engine import."
            )
            naming_sev = "INFO"
        else:
            naming_note = (
                f"Root bone name matches UE5 convention: "
                f"{list(root_bone_names & ue5_root_names)}."
            )
            naming_sev = "PASS"

        checks.append({
            "check":    "ue5_naming_convention",
            "severity": naming_sev,
            "detail":   naming_note,
        })

        # ── Overall verdict ─────────────────────────────────────────────────
        has_critical = any(c["severity"] == "CRITICAL" for c in checks)
        has_warning  = any(c["severity"] == "WARNING"  for c in checks)
        verdict      = "CRITICAL" if has_critical else "WARNING" if has_warning else "PASS"

        if has_critical:
            summary = (
                "Skeleton has CRITICAL structural issues. Root bone position "
                "must be fixed before any export or animation baking."
            )
        elif has_warning:
            summary = (
                f"Skeleton structure is functional ({bone_count} bones) but has "
                f"orphan bones that should be reviewed before export."
            )
        else:
            summary = (
                f"Skeleton structure checks out: {bone_count} bone(s), "
                f"root at origin, no orphans detected."
            )

        print(json.dumps({
            "verdict":       verdict,
            "summary":       summary,
            "armature_name": armature_obj.name,
            "bone_count":    bone_count,
            "root_bones":    [b.name for b in root_bones],
            "checks":        checks,
        }))
""".replace("{OBJECT_NAME}", object_name.replace("'", "\\'"))

        blender = get_blender_connection()
        raw = blender.send_command("execute_code_safe", {"code": script})

        # FIX: same as analyze_rig_weights — captured stdout is under "result".
        output_text = raw.get("result", "") if isinstance(raw, dict) else str(raw)
        result = None
        for line in output_text.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    result = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue

        if result is None:
            return json.dumps({
                "error": "No JSON output from rig skeleton inspection script.",
                "raw_output": output_text[:500],
            })
        if "error" in result:
            return json.dumps(result)

        # Slim mode: strip PASS checks — only keep CRITICAL/WARNING/INFO findings
        if not verbose:
            all_checks = result.get("checks", [])
            result["checks"] = [
                c for c in all_checks
                if c.get("severity") not in ("PASS",)
            ]
            result["passed_count"] = len(all_checks) - len(result["checks"])
            result["_tip"] = "Pass verbose=True to see passing checks too."

        return json.dumps(result, indent=2, default=str)

    except Exception as e:
        logger.error(f"Error in analyze_rig_skeleton: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def validate_bake_setup(low_poly_name: str, high_poly_name: str, verbose: bool = False) -> str:
    """
    BAKE PRE-FLIGHT — validates that a bake setup is correct before touching anything.

    Run this BEFORE every bake operation. It will hard-stop on any condition that
    causes black textures, smeared detail, incorrect normals, or failed exports.

    Checks (in priority order):

      CRITICAL — blocks bake entirely:
        1. Both objects exist in scene by the given names
        2. Low poly has a UV map with at least one island
        3. No overlapping UV islands (uses Blender's select_overlap operator)
        4. Active material has an Image Texture node in the shader
        5. That Image Texture node is the active/selected node in the shader
        6. The bake target image is valid (exists, not zero-size)

      WARNING — bake can proceed but you should know:
        7. UV island margin (islands packed too tight → bleed at low resolution)
        8. High poly visibility (hidden objects bake but it's a common confusion)
        9. Unapplied scale on low poly (distorts normal map results)
       10. Modifiers on low poly (subdivision etc. affect bake cage unexpectedly)

    Parameters:
      low_poly_name  : name of the low-poly mesh object (bake target)
      high_poly_name : name of the high-poly mesh object (bake source)
      verbose        : False (default) — returns failing/warning checks only.
                       True — returns all 10 checks including PASSes.

    Returns:
      safe_to_bake   : bool — True only if zero CRITICAL failures
      verdict        : PASS | WARN | FAIL
      checks         : failing/warning checks (verbose=False) or all checks (verbose=True)
      ready_when_fixed : ordered list of actions needed before baking
    """
    try:
        script = r"""
import bpy
import json

low_poly_name  = '{LOW_POLY}'
high_poly_name = '{HIGH_POLY}'
verbose        = {VERBOSE}

checks   = []
fix_list = []

def add_check(check, severity, status, detail, why, why_now, consequence, fix=None):
    checks.append({
        "check":       check,
        "severity":    severity,
        "status":      status,
        "detail":      detail,
        "why":         why,
        "why_now":     why_now,
        "consequence": consequence,
    })
    if status in ("FAIL", "WARN") and fix:
        fix_list.append(fix)

def emit(extra=None):
    critical_fails = [c for c in checks if c["severity"] == "CRITICAL" and c["status"] == "FAIL"]
    warnings       = [c for c in checks if c["severity"] == "WARNING"  and c["status"] == "WARN"]
    safe           = len(critical_fails) == 0
    if critical_fails:
        verdict  = "FAIL"
        summary  = (f"{len(critical_fails)} CRITICAL issue(s) must be fixed before baking. "
                    f"Baking now will produce black, empty, or incorrect textures.")
    elif warnings:
        verdict  = "WARN"
        summary  = (f"No critical blockers. {len(warnings)} warning(s) to review. "
                    f"Bake can proceed but check the warnings — they affect output quality.")
    else:
        verdict  = "PASS"
        summary  = (f"All pre-flight checks passed. Setup looks correct. "
                    f"Safe to bake '{high_poly_name}' -> '{low_poly_name}'.")

    # Slim mode: only emit checks that need attention (FAIL or WARN)
    # Verbose mode: emit all checks including PASSes
    if verbose:
        emitted_checks = checks
    else:
        emitted_checks = [c for c in checks if c["status"] in ("FAIL", "WARN")]

    out = {
        "safe_to_bake":     safe,
        "verdict":          verdict,
        "summary":          summary,
        "low_poly":         low_poly_name,
        "high_poly":        high_poly_name,
        "checks":           emitted_checks,
        "ready_when_fixed": fix_list,
        "critical_count":   len(critical_fails),
        "warning_count":    len(warnings),
        "passed_count":     len(checks) - len(critical_fails) - len(warnings),
    }
    if not verbose:
        out["_tip"] = "Pass verbose=True to see all 10 checks including passing ones."
    if extra:
        out.update(extra)
    print(json.dumps(out))

# ── CHECK 1: Objects exist ──────────────────────────────────────────────────
low_obj  = bpy.data.objects.get(low_poly_name)
high_obj = bpy.data.objects.get(high_poly_name)

if low_obj is None:
    add_check(
        "low_poly_exists", "CRITICAL", "FAIL",
        f"Object '{low_poly_name}' not found in scene.",
        "The bake target must exist as a named object in the scene.",
        "Cannot proceed — Blender has nothing to bake onto.",
        "Bake will error immediately.",
        f"Verify the exact object name. Got: '{low_poly_name}'.",
    )
if high_obj is None:
    add_check(
        "high_poly_exists", "CRITICAL", "FAIL",
        f"Object '{high_poly_name}' not found in scene.",
        "The bake source must exist as a named object in the scene.",
        "Cannot proceed — Blender has no high-poly to sample from.",
        "Bake will produce a flat or black result — no detail transferred.",
        f"Verify the exact object name. Got: '{high_poly_name}'.",
    )

if low_obj is None or high_obj is None:
    # Emit early result and stop — do NOT use raise SystemExit.
    # SystemExit is a BaseException subclass, not Exception. It escapes
    # addon.py's except-Exception handlers and crashes Blender's main thread
    # when running inside bpy.app.timers. Use emit() + guard flag instead.
    emit({"_note": "Object lookup failed — remaining checks skipped."})
else:
    # ── Both objects confirmed — run all remaining checks ───────────────────

    # CHECK 1b: low poly is a mesh
    if low_obj.type != 'MESH':
        add_check(
            "low_poly_is_mesh", "CRITICAL", "FAIL",
            f"'{low_poly_name}' is type {low_obj.type}, not MESH.",
            "Only mesh objects can be bake targets.",
            "Blender cannot bake onto a non-mesh object.",
            "Bake will error.",
            "Select the correct mesh object as the low poly.",
        )
    else:
        add_check(
            "low_poly_is_mesh", "CRITICAL", "PASS",
            f"'{low_poly_name}' is a MESH object.", "", "", "",
        )

    # ── CHECK 2: Low poly has UVs ───────────────────────────────────────────
    if low_obj.type == 'MESH':
        uv_layers = low_obj.data.uv_layers
        if len(uv_layers) == 0:
            add_check(
                "low_poly_has_uvs", "CRITICAL", "FAIL",
                "No UV map found on low poly.",
                "Baking writes detail into UV space. Without a UV map there is nowhere to write.",
                "Blender will error before the bake begins.",
                "Result: bake error, no texture produced.",
                "UV unwrap the low poly first (U key in Edit Mode -> Unwrap).",
            )
        else:
            has_uvdata = len(low_obj.data.uv_layers[0].data) > 0
            if not has_uvdata:
                add_check(
                    "low_poly_has_uvs", "CRITICAL", "FAIL",
                    "UV layer exists but contains no UV data (empty layer).",
                    "An empty UV layer is the same as no UVs for baking purposes.",
                    "Bake will produce black — no UV coordinates to write into.",
                    "Result: black texture.",
                    "Delete the empty UV layer and re-unwrap.",
                )
            else:
                add_check(
                    "low_poly_has_uvs", "CRITICAL", "PASS",
                    f"{len(uv_layers)} UV layer(s) found with data.", "", "", "",
                )

    # ── CHECK 3: Overlapping UV islands ────────────────────────────────────
    overlap_count = 0
    overlap_error = None

    if low_obj.type == 'MESH' and len(low_obj.data.uv_layers) > 0:
        original_active = bpy.context.view_layer.objects.active
        try:
            bpy.context.view_layer.objects.active = low_obj
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='DESELECT')
            bpy.ops.uv.select_all(action='DESELECT')
            bpy.ops.uv.select_overlap()
            mesh_edit = low_obj.data
            bpy.ops.object.mode_set(mode='OBJECT')
            overlap_count = sum(
                1 for loop_uv in mesh_edit.uv_layers.active.data
                if loop_uv.select
            )
        except Exception as oe:
            overlap_error = str(oe)
            overlap_count = 0
        finally:
            try:
                bpy.ops.object.mode_set(mode='OBJECT')
            except Exception:
                pass
            bpy.context.view_layer.objects.active = original_active

        if overlap_error:
            add_check(
                "uv_no_overlaps", "CRITICAL", "WARN",
                f"Could not run overlap check: {overlap_error}",
                "UV overlap check requires Edit Mode access.",
                "Overlap check skipped — verify manually in UV Editor.",
                "If islands overlap: smeared or doubled texture detail.",
                "Open UV Editor -> UV menu -> Select Overlapping to check manually.",
            )
        elif overlap_count > 0:
            add_check(
                "uv_no_overlaps", "CRITICAL", "FAIL",
                f"{overlap_count} UV loop(s) flagged as overlapping.",
                "Overlapping UV islands occupy the same texture space.",
                "Two surfaces share the same pixels — baked detail from one bleeds onto the other.",
                "Smeared or doubled texture detail in overlapping areas. Wrong in engine.",
                "UV Editor -> UV -> Select Overlapping -> separate and repack islands.",
            )
        else:
            add_check(
                "uv_no_overlaps", "CRITICAL", "PASS",
                "No overlapping UV islands detected.", "", "", "",
            )

    # ── CHECK 4–6: Material, image node, image validity ────────────────────
    image_node_selected = False
    bake_image          = None

    if low_obj.type == 'MESH':
        mat = low_obj.active_material
        if mat is None:
            add_check(
                "material_exists", "CRITICAL", "FAIL",
                "Low poly has no active material.",
                "Blender bakes into an image node inside the active material's shader.",
                "Without a material there is no shader, no image node, nowhere to bake.",
                "Bake will error: 'No active image found in material'.",
                "Assign a material to the low poly with an Image Texture node set up.",
            )
        elif mat.node_tree is None:
            add_check(
                "material_exists", "CRITICAL", "FAIL",
                f"Material '{mat.name}' has no node tree (not using nodes).",
                "Blender requires a node-based material to identify the bake target image.",
                "Non-node materials have no Image Texture node to bake into.",
                "Bake will error.",
                f"Enable 'Use Nodes' on material '{mat.name}' and add an Image Texture node.",
            )
        else:
            add_check(
                "material_exists", "CRITICAL", "PASS",
                f"Active material '{mat.name}' with node tree found.", "", "", "",
            )

            img_nodes = [n for n in mat.node_tree.nodes if n.type == 'TEX_IMAGE']

            if not img_nodes:
                add_check(
                    "image_node_exists", "CRITICAL", "FAIL",
                    f"No Image Texture node found in material '{mat.name}'.",
                    "Blender bakes into an Image Texture node. It must exist in the shader.",
                    "Without the node, Blender has no image to write bake data into.",
                    "Bake will error: 'No active image found in material'.",
                    f"Add an Image Texture node to '{mat.name}', create/assign a new image, "
                    f"then select that node before baking.",
                )
            else:
                add_check(
                    "image_node_exists", "CRITICAL", "PASS",
                    f"{len(img_nodes)} Image Texture node(s) found in '{mat.name}'.", "", "", "",
                )

                active_node = mat.node_tree.nodes.active
                if active_node is None or active_node.type != 'TEX_IMAGE':
                    selected_img = [n for n in img_nodes if n.select]
                    fix_msg = (
                        "Ctrl+click the Image Texture node to make it both selected and active."
                        if selected_img else
                        "In Shader Editor, click to select the Image Texture node "
                        "you want to bake into. It must have an image assigned."
                    )
                    detail_msg = (
                        "An Image Texture node is selected but is not the ACTIVE node."
                        if selected_img else
                        "No Image Texture node is the active node in the shader."
                    )
                    add_check(
                        "image_node_selected", "CRITICAL", "FAIL",
                        detail_msg,
                        "Blender bakes into the ACTIVE (highlighted) Image Texture node, "
                        "not just any node that exists.",
                        "The node exists but is not active — Blender does not know which "
                        "image to write into.",
                        "Bake will produce a black result or error: "
                        "'No active image found in material'.",
                        fix_msg,
                    )
                else:
                    image_node_selected = True
                    bake_image = active_node.image
                    add_check(
                        "image_node_selected", "CRITICAL", "PASS",
                        f"Active node is Image Texture: "
                        f"image='{bake_image.name if bake_image else 'None'}'.",
                        "", "", "",
                    )

                if image_node_selected:
                    if bake_image is None:
                        add_check(
                            "bake_image_valid", "CRITICAL", "FAIL",
                            "Active Image Texture node has no image assigned.",
                            "The node exists and is active but has no image loaded or created.",
                            "Blender cannot write bake data into an empty node slot.",
                            "Bake will error: 'No active image found in material'.",
                            "In the Image Texture node, create a new image "
                            "(New button) or load an existing one.",
                        )
                    elif bake_image.size[0] == 0 or bake_image.size[1] == 0:
                        add_check(
                            "bake_image_valid", "CRITICAL", "FAIL",
                            f"Image '{bake_image.name}' has zero size "
                            f"({bake_image.size[0]}x{bake_image.size[1]}).",
                            "A zero-size image has no pixels to write into.",
                            "Blender cannot bake into a zero-size image.",
                            "Bake will error or produce nothing.",
                            f"Delete and recreate '{bake_image.name}' with a valid resolution "
                            f"(e.g. 2048x2048 or 4096x4096).",
                        )
                    else:
                        add_check(
                            "bake_image_valid", "CRITICAL", "PASS",
                            f"Bake target image '{bake_image.name}' is "
                            f"{bake_image.size[0]}x{bake_image.size[1]}.",
                            "", "", "",
                        )

    # ── CHECK 7: UV margin advisory (WARNING) ───────────────────────────────
    if low_obj.type == 'MESH' and len(low_obj.data.uv_layers) > 0:
        add_check(
            "uv_margin", "WARNING", "WARN",
            "UV margin not automatically measurable — verify manually.",
            "Islands packed too close together cause pixel bleed between islands "
            "at lower texture resolutions (1K, 2K).",
            "At export or in-engine mip-mapping, neighbouring islands bleed colour "
            "onto each other along seams.",
            "Faint colour fringing along UV seams visible in engine at distance.",
            "UV Editor -> N panel -> check island spacing. "
            "Use at least 2px margin at 1K, 4px at 2K, 8px at 4K.",
        )

    # ── CHECK 8: High poly visibility (WARNING) ─────────────────────────────
    is_hidden = not high_obj.visible_get()
    if is_hidden:
        add_check(
            "high_poly_visible", "WARNING", "WARN",
            f"'{high_poly_name}' is hidden in the viewport.",
            "Blender CAN bake from hidden objects in selected-to-active mode, "
            "but hidden high polys are a common source of confusion and errors.",
            "If the wrong object is the bake source, the result will be flat or wrong.",
            "You may get a flat normal map with no detail transferred.",
            f"Unhide '{high_poly_name}' (H key or eye icon in Outliner) "
            f"to confirm it is the correct source before baking.",
        )
    else:
        add_check(
            "high_poly_visible", "WARNING", "PASS",
            f"'{high_poly_name}' is visible in viewport.", "", "", "",
        )

    # ── CHECK 9: Unapplied scale on low poly (WARNING) ──────────────────────
    scale = low_obj.scale
    scale_ok = (
        abs(scale.x - 1.0) < 0.001 and
        abs(scale.y - 1.0) < 0.001 and
        abs(scale.z - 1.0) < 0.001
    )
    if not scale_ok:
        add_check(
            "scale_applied", "WARNING", "WARN",
            f"Low poly scale not applied: ({scale.x:.3f}, {scale.y:.3f}, {scale.z:.3f}).",
            "Unapplied scale means Blender's internal geometry differs from what you see. "
            "Normal map baking uses raw geometry — unapplied scale skews the ray direction.",
            "Normal maps baked with unapplied scale will look incorrect when scale is "
            "applied later (which it must be for UE5 export).",
            "Normal map will appear swirled, inverted, or incorrect after export.",
            "Object Mode -> Object -> Apply -> Scale (Ctrl+A -> Scale).",
        )
    else:
        add_check(
            "scale_applied", "WARNING", "PASS",
            "Scale is applied on low poly (1.0, 1.0, 1.0).", "", "", "",
        )

    # ── CHECK 10: Bake-affecting modifiers on low poly (WARNING) ────────────
    if low_obj.type == 'MESH':
        bake_mods = [
            m.name for m in low_obj.modifiers
            if m.type in ('SUBSURF', 'MULTIRES', 'DISPLACE', 'SHRINKWRAP')
            and m.show_viewport
        ]
        if bake_mods:
            add_check(
                "modifier_check", "WARNING", "WARN",
                f"Bake-affecting modifier(s) active on low poly: {bake_mods}.",
                "Subdivision, Multires, Displace, and Shrinkwrap modifiers change the "
                "effective mesh shape during baking. The bake cage uses the modified mesh.",
                "If you remove these modifiers after baking, the bake result will not "
                "match the exported mesh.",
                "Bake result will not match the exported mesh if modifiers are removed later.",
                f"Decide: apply modifiers before baking, or disable them. Affected: {bake_mods}.",
            )
        else:
            add_check(
                "modifier_check", "WARNING", "PASS",
                "No bake-affecting modifiers active on low poly.", "", "", "",
            )

    # ── Emit final result ───────────────────────────────────────────────────
    emit()
""".replace("{LOW_POLY}", low_poly_name.replace("'", "\\'")) \
   .replace("{HIGH_POLY}", high_poly_name.replace("'", "\\'")) \
   .replace("{VERBOSE}", "True" if verbose else "False")

        blender = get_blender_connection()
        raw = blender.send_command("execute_code_safe", {"code": script})

        output_text = raw.get("result", "") if isinstance(raw, dict) else str(raw)
        result = None
        for line in output_text.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    result = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue

        if result is None:
            return json.dumps({
                "error": "No JSON output from bake pre-flight script.",
                "raw_output": output_text[:500],
            })
        if "error" in result:
            return json.dumps(result)

        return json.dumps(result, indent=2, default=str)

    except Exception as e:
        logger.error(f"Error in validate_bake_setup: {e}")
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
                    probs      = stats.get("problems", {})
                    face_types = stats.get("face_types", {})
                    ngon_count = face_types.get("ngons", 0) or 0
                    # Count distinct problem TYPES with at least one instance.
                    # Ngons live under face_types, not problems — add them explicitly
                    # so problem_count matches detect_mesh_problems semantics.
                    problem_count = sum(1 for v in probs.values() if isinstance(v, int) and v > 0)
                    if ngon_count > 0:
                        problem_count += 1
                    # Worst issue: check problems dict AND ngons, surface whichever is largest
                    candidates = {k: v for k, v in probs.items() if isinstance(v, int) and v > 0}
                    if ngon_count > 0:
                        candidates["ngons"] = ngon_count
                    if candidates:
                        worst_key = max(candidates, key=candidates.get)
                        worst_issue = f"{worst_key} ({candidates[worst_key]})"
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
# AI TECHNICAL DIRECTOR — v3.0
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def plan_production_path(
    object_name: str,
    goal: str = "export_ready",
) -> str:
    """
    AI TECHNICAL DIRECTOR — Build a 5-step production plan for this asset.

    Reads the current scene state + session context + active playbook, then
    proposes a concrete, ordered 5-step plan to reach the stated goal.
    Each step includes: what to do, which tool executes it, what success looks
    like, and which gate/check confirms it's done.

    After you present the plan to the user, wait for explicit approval before
    executing any step. After each step, re-read state before proceeding.

    Parameters:
      object_name : Blender object to plan for
      goal        : "export_ready" (default) | "bake_ready" | "rig_ready" |
                    "texture_ready" | "review_only"

    Returns:
      plan         : ordered list of 5 steps, each with tool, success_criteria, gate
      assumptions  : what the TD is assuming about this asset
      active_playbook_name : playbook in use (or "none")
      session_context_used : True if session data informed the plan
      approval_required    : always True — present plan, wait for "yes/go ahead"
    """
    try:
        # ── Gather state ──────────────────────────────────────────────────────
        obj_info   = _send_raw("get_object_info",         name=object_name)
        mesh_stats = _send_raw("get_mesh_quality_report", name=object_name)
        if "error" in obj_info:
            return json.dumps({"error": f"Object not found: {object_name}"})

        mesh_block   = obj_info.get("mesh", {})
        vertex_count = mesh_block.get("vertices", 0) or 0
        face_count   = mesh_block.get("polygons",  0) or 0
        mat_list     = obj_info.get("materials", [])
        has_uvs      = mesh_stats.get("uv", {}).get("has_uvs", False)
        uv_layers    = mesh_stats.get("uv", {}).get("layer_count", 0)
        has_arm      = any(
            m.get("type") == "ARMATURE"
            for m in mesh_stats.get("modifiers", [])
            if isinstance(m, dict)
        )
        health       = mesh_stats.get("health", "unknown")
        face_types   = mesh_stats.get("face_types", {})
        ngon_count   = face_types.get("ngons", 0) or 0

        # Problems
        prob_raw  = _send_raw("detect_mesh_problems", name=object_name)
        prob_list = prob_raw.get("problems", []) if "error" not in prob_raw else []
        prob_map  = {p.get("type", ""): p.get("count", 0) for p in prob_list}
        nm_edges  = prob_map.get("non_manifold_edges", 0)

        # Stage inference
        stage_result = _classify_stage_from_signals(obj_info, mesh_stats)
        stage_num    = stage_result.get("stage_number", 0)
        stage_name   = stage_result.get("stage_name",   "Unknown")

        # Session
        pb           = _get_active_playbook()
        session_type = _session_get("asset_type") or ""
        verified     = _session_get("verified_checks") or []
        open_iss     = _session_get("open_issues") or []
        conf_stage   = _session_get("confirmed_stage")

        pb_name = pb["name"] if pb else "none"
        effective_stage = conf_stage or stage_num

        # ── Build 5-step plan ─────────────────────────────────────────────────
        # Adapt to goal and current state. Skip already-verified checks.
        steps = []
        step_n = 1

        def step(title, tool_call, success_criteria, gate, note=""):
            nonlocal step_n
            s = {
                "step":             step_n,
                "title":            title,
                "tool_call":        tool_call,
                "success_criteria": success_criteria,
                "gate":             gate,
            }
            if note:
                s["note"] = note
            steps.append(s)
            step_n += 1

        # Step 1: Always start with visual + scene orientation (unless already done)
        if "get_viewport_screenshot" not in verified:
            step(
                "Visual inspection — look before you touch",
                "get_viewport_screenshot() then get_scene_info()",
                "Screenshot captured, scene object count confirmed, active object identified",
                "None — observation only. Describe what you see.",
                note="Never skip this. The screenshot tells you things the data doesn't.",
            )
        else:
            step(
                "Re-orient: confirm current state matches last session",
                "session_status() then get_viewport_screenshot()",
                "Session context refreshed, any scene changes since last session identified",
                "None — observation only.",
                note="Session has prior context. Verify scene hasn't changed before acting.",
            )

        # Step 2: Geometry health
        if "detect_mesh_problems" not in verified and "analyze_mesh_for_unreal" not in verified:
            step(
                "Geometry health check — find any blocking errors",
                f"analyze_mesh_for_unreal(name='{object_name}')",
                "Zero non-manifold edges. Zero degenerate faces. Zero duplicate faces.",
                "GATE 1 — If critical errors found: auto_repair_mesh() with user approval first.",
                note=f"Current signal: {nm_edges} non-manifold edges. {'Repair needed.' if nm_edges > 0 else 'Looks clean — confirm with tool.'}",
            )
        else:
            already = [c for c in ["analyze_mesh_for_unreal", "detect_mesh_problems"] if c in verified]
            step(
                "Geometry verified this session — check topology",
                f"analyze_topology(name='{object_name}')",
                "Topology score ≥ playbook minimum. Quad dominance in deformation zones.",
                "No hard gate — inform next step.",
                note=f"Geometry checks already run: {', '.join(already)}. Open issues: {open_iss or 'none'}.",
            )

        # Step 3: Stage-specific gate
        if goal == "bake_ready" or effective_stage <= 3:
            step(
                "Bake pre-flight — validate UV and bake setup",
                f"validate_bake_setup(low_poly_name='{object_name}', high_poly_name='<HIGH_POLY_NAME>')",
                "All 10 bake checks pass or warnings acknowledged. Image Texture node active. No UV overlap.",
                "GATE 5 — Do not initiate bake if verdict is FAIL. WARN: state warnings, get confirmation.",
                note="Replace <HIGH_POLY_NAME> with the actual high-poly object name in the scene.",
            )
        elif goal == "rig_ready" or (effective_stage == 5 and has_arm):
            step(
                "Rig QA — weights and skeleton before export gate",
                f"analyze_rig_weights(object_name='{object_name}') then analyze_rig_skeleton(object_name='{object_name}')",
                "Zero unweighted vertices. All vertices ≤8 influences. Root bone at origin. No orphan bones.",
                "GATE 3 (Export) — Rig QA must pass before export gate is valid.",
                note="Run both tools. A PASS on weights with a FAIL on skeleton is still a combined failure.",
            )
        elif has_uvs and len(mat_list) > 0:
            step(
                "Material validation — PBR integrity check",
                f"analyze_material_pbr(name='{object_name}')",
                "Principled BSDF confirmed. No broken texture paths. Normal map Y-flip correct for UE5.",
                "GATE 2 (Stage Transition) — Material must pass before export gate.",
                note=f"{len(mat_list)} material(s) found on object.",
            )
        else:
            step(
                "UV unwrap — required before any downstream work",
                "No MCP tool — manual Blender operation",
                "UV map present with no overlapping islands. UV stretch < 20%.",
                "No MCP gate — verify with get_mesh_quality_report() after.",
                note="Use Smart UV Project as a starting point on hard surface. Mark seams manually for characters.",
            )

        # Step 4: Full export gate (unless goal is pre-export)
        if goal not in ("bake_ready", "review_only"):
            step(
                "Full Unreal readiness check — export gate",
                f"run_unreal_readiness_check(name='{object_name}')",
                "Zero blocking errors. Scale applied. Pivot at origin. Lightmap UV present.",
                "GATE 3 (Export) — Must be zero errors before export_for_unreal() is called.",
                note=pb.get("stage_standards", {}).get(6, "") if pb else "",
            )

        # Step 5: Export or playbook-specific closer
        if goal == "export_ready":
            step(
                "Export to Unreal Engine",
                f"export_for_unreal(name='{object_name}')",
                "FBX exported to project path. No modifier errors. Armature included if rigged.",
                "GATE 4 (Irreversible Op) — State what will happen. Wait for explicit 'yes/export/go ahead'.",
                note="After export: verify in UE5 — check for missing materials, incorrect scale, animation playback.",
            )
        elif goal == "review_only":
            step(
                "Production review — full scored report",
                f"production_review(object_name='{object_name}', asset_type='{session_type or effective_stage}')",
                "production_score ≥ 75 (grade B or better). Zero conflicts. Zero critical blockers.",
                "No gate — report delivered to user.",
                note="Use include_rig=True if this is a rigged character. Surface all conflicts.",
            )
        else:
            step(
                "Verify and document — update session context",
                "session_update(add_verified_check='...', add_open_issue='...')",
                "Session context reflects all completed work. Open issues documented.",
                "None — session hygiene step.",
                note="Keep session accurate so future turns don't re-run already-completed checks.",
            )

        # ── Assumptions block ─────────────────────────────────────────────────
        assumptions = [
            f"Asset: {object_name} — {vertex_count:,} vertices, Stage {effective_stage} ({stage_name})",
            f"Playbook: {pb_name}",
            f"Goal: {goal}",
        ]
        if open_iss:
            assumptions.append(f"Open issues from this session: {', '.join(open_iss)}")
        if nm_edges > 0:
            assumptions.append(f"⚠️ {nm_edges} non-manifold edges detected — Step 2 will address this first.")

        # Update session
        _session_set(active_object=object_name)
        _session_append("verified_checks", "plan_production_path")

        return json.dumps({
            "plan":                   steps,
            "step_count":             len(steps),
            "assumptions":            assumptions,
            "active_playbook_name":   pb_name,
            "session_context_used":   bool(verified or open_iss or conf_stage or session_type),
            "approval_required":      True,
            "_instruction": (
                "Present this plan to the user. Wait for explicit approval ('yes', 'go ahead', "
                "'do it') before executing Step 1. After each step: re-read state, then proceed "
                "to next step only if success_criteria are met."
            ),
        }, indent=2, default=str)

    except Exception as e:
        logger.error(f"Error in plan_production_path: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def critique_mesh(
    object_name: str,
    focus: str = "all",
    verbose: bool = False,
) -> str:
    """
    AI MESH CRITIC — Senior technical artist review of topology and mesh decisions.

    Goes beyond pass/fail counts to give you the *why* behind each finding:
    why this topology decision creates problems, which production scenarios
    will expose the failure, and what a senior artist would actually do to fix it.

    Different from analyze_mesh_for_unreal: that tool tells you what's wrong.
    This tool tells you why it matters, in the context of the active playbook
    and production stage, with the depth of a senior TA code review.

    Parameters:
      object_name : Blender object to critique
      focus       : "all" (default) | "topology" | "uvs" | "geometry" | "deformation"
      verbose     : True → include passing observations (what's done well)

    Returns:
      critique_verdict   : overall assessment with senior framing
      priority_findings  : ordered by production impact, not just severity
      compounding_issues : pairs of issues that are worse together than separately
      deformation_risk   : specific risks in deforming areas (if rig detected)
      what_i_would_do    : concrete senior-TA recommendation for each major finding
      playbook_context   : how findings relate to active playbook standards
    """
    try:
        # ── Gather data ───────────────────────────────────────────────────────
        raw_problems = _send_raw("detect_mesh_problems",    name=object_name)
        raw_quality  = _send_raw("get_mesh_quality_report", name=object_name)
        raw_topology = _send_raw("analyze_topology",        name=object_name)

        r_problems = _reason_mesh_problems(raw_problems) if "error" not in raw_problems else raw_problems
        r_quality  = _reason_mesh_quality(raw_quality)   if "error" not in raw_quality  else raw_quality
        r_topology = _reason_topology(raw_topology)      if "error" not in raw_topology else raw_topology

        counts     = raw_quality.get("counts", {})
        face_types = raw_quality.get("face_types", {})
        vert_count = counts.get("verts", 0) or 0
        face_count = counts.get("faces", 0) or 0
        ngon_count = face_types.get("ngons", 0) or 0
        quad_count = face_types.get("quads", 0) or 0
        tri_count  = face_types.get("tris",  0) or 0
        has_uvs    = raw_quality.get("uv", {}).get("has_uvs", False)
        uv_oob     = raw_quality.get("uv", {}).get("out_of_bounds_loops", 0) or 0
        poles      = raw_quality.get("poles", {})
        high_val   = poles.get("high_valence", 0) or 0
        topo_score = raw_topology.get("topology_score", 100) if "error" not in raw_topology else 100
        topo_rating = raw_topology.get("rating", "unknown") if "error" not in raw_topology else "unknown"

        # Armature check
        has_arm = any(
            m.get("type") == "ARMATURE"
            for m in raw_quality.get("modifiers", [])
            if isinstance(m, dict)
        )

        # All raw findings
        all_findings = []
        for enriched in [r_problems, r_quality, r_topology]:
            for f in enriched.get("_reasoning", {}).get("findings", []):
                all_findings.append(f)

        # ── Senior-framed priority findings ───────────────────────────────────
        # Re-rank by production impact, not just severity.
        # Production impact = severity × (downstream_blocker_count + deformation_risk)
        priority_findings = []
        for f in all_findings:
            issue   = f.get("issue", "")
            sev     = f.get("severity", "info")
            fix     = f.get("professional_fix") or f.get("fix") or ""
            why     = f.get("why_it_matters", "")

            # Senior framing: add "what I would do" and downstream risk
            if "non_manifold" in issue.lower():
                downstream = "Blocks baking, subdivision, UE5 import, and physics. Fix this before anything else."
                senior_do  = "Run auto_repair_mesh() — merge by distance + interior face delete. Re-scan. If any survive, manually select Non Manifold in Edit Mode and investigate each one."
            elif "n-gon" in issue.lower() or "ngon" in issue.lower():
                downstream = f"UE5 auto-tris n-gons. At {ngon_count} n-gons you'll see star patterns and banding under normals. Worse under subdivision or dynamic lighting."
                senior_do  = f"Select All by Trait > Face Sides > Greater Than 4. Knife-cut from pole to pole. Don't dissolve existing edges — reroute. Priority: any n-gon in a deformation zone."
            elif "zero-area" in issue.lower() or "degenerate" in issue.lower():
                downstream = "Undefined normals cause black patches in baking and undefined behavior in physics. Silent failure — bakes look fine in preview, wrong in engine."
                senior_do  = "Degenerate Dissolve at 0.0001 threshold. Then bake a test patch — if you see any black faces, there are survivors."
            elif "uv" in issue.lower() or "out-of-bounds" in issue.lower():
                downstream = f"UV issues compound with bake issues. If you have {uv_oob} out-of-bounds loops on lightmap channel, you'll get shadow bleeding across unrelated surfaces."
                senior_do  = "Check which UV channel. Channel 0 OOB = maybe intentional tiling. Channel 1 OOB = lightmap error, fix now."
            elif "pole" in issue.lower() or "valence" in issue.lower():
                downstream = "High-valence poles (6+) cause pinching under subdivision and make skinning harder. Predictable failure point at joint centers."
                senior_do  = "Dissolve edges feeding into the pole. Target ≤5 edges at any non-boundary vertex. Priority: poles near joints."
            elif "duplicate" in issue.lower():
                downstream = "Z-fighting at all render distances. Doubles GPU cost for zero visual benefit. Silent in Blender, visible immediately in UE5."
                senior_do  = "Merge by Distance 0.0001. Re-check face count before/after — duplicates disappear cleanly."
            elif "isolated" in issue.lower() or "loose" in issue.lower():
                downstream = "Inflates vert count and shifts bounding box. May offset pivot point from expected location."
                senior_do  = "Delete Loose (Edit Mode > Mesh > Clean Up). Zero visual impact."
            else:
                downstream = "Review in context of the current stage."
                senior_do  = fix or "Manual review required."

            entry = {
                "issue":           issue,
                "severity":        sev,
                "downstream_risk": downstream,
                "what_i_would_do": senior_do,
            }
            if verbose and why:
                entry["why_it_matters"] = why
            priority_findings.append(entry)

        # Sort by severity (critical > warning > info)
        sev_order = {"critical": 0, "warning": 1, "info": 2}
        priority_findings.sort(key=lambda f: sev_order.get(f["severity"], 3))

        # ── Compounding issues ────────────────────────────────────────────────
        # Pairs of issues that are significantly worse together.
        compounding = []
        sev_set = {f.get("severity") for f in all_findings}
        has_nm  = any("non_manifold" in f.get("issue","").lower() for f in all_findings)
        has_ng  = ngon_count > 0
        has_uv_issue = uv_oob > 0

        if has_nm and has_ng:
            compounding.append({
                "pair": ["Non-manifold edges", "N-gons"],
                "compounding_effect": (
                    "N-gon tessellation is unpredictable on its own. Combined with non-manifold edges, "
                    "the UE5 auto-triangulator hits undefined geometry — can produce NaN normals and "
                    "invisible faces that still cast shadows. Fix non-manifold first."
                ),
            })
        if has_uv_issue and has_nm:
            compounding.append({
                "pair": ["UV out-of-bounds", "Non-manifold edges"],
                "compounding_effect": (
                    "Baking with UV errors + non-manifold geometry = corrupted bake output that's "
                    "hard to diagnose. The projection errors and geometry errors produce overlapping "
                    "artifacts. Fix geometry before baking — don't try to bake around geo problems."
                ),
            })
        if high_val > 5 and has_arm:
            compounding.append({
                "pair": [f"{high_val} high-valence poles", "Armature modifier (rigged mesh)"],
                "compounding_effect": (
                    "High-valence poles near joint areas create unpredictable deformation. "
                    "The skin weight solver distributes influence across all edges — "
                    "a 6-pole at the shoulder creates 6 competing influence vectors. "
                    "Visible as pinching on full-range poses."
                ),
            })

        # ── Deformation risk (rigged assets only) ─────────────────────────────
        deformation_risk = None
        if has_arm:
            risks = []
            if ngon_count > 0:
                risks.append(f"{ngon_count} n-gon(s) in mesh — if any are in joint areas, expect deformation artefacts.")
            if high_val > 5:
                risks.append(f"{high_val} high-valence poles — verify none are within joint deformation zones.")
            if not has_uvs:
                risks.append("No UV map — weight painting without UV reference makes verification harder.")
            deformation_risk = risks if risks else ["No specific deformation risks detected from geometry signals."]

        # ── Overall critique verdict ──────────────────────────────────────────
        criticals = [f for f in all_findings if f.get("severity") == "critical"]
        warnings  = [f for f in all_findings if f.get("severity") == "warning"]

        if not criticals and not warnings:
            if topo_score >= 80:
                verdict = (
                    f"Clean mesh with strong topology ({topo_score}/100, {topo_rating}). "
                    f"This is a well-constructed asset. The geometry is solid — proceed to "
                    f"the next stage with confidence."
                )
            else:
                verdict = (
                    f"Mesh is geometrically clean but topology score is {topo_score}/100 ({topo_rating}). "
                    f"No blockers, but topology optimisation would improve deformation and LOD generation."
                )
        elif criticals:
            verdict = (
                f"This mesh has {len(criticals)} critical issue(s) that will cause failures in production. "
                f"Priority: address the highest-severity finding first — each critical issue compounds others. "
                f"Do not proceed to baking, rigging, or export until criticals are resolved."
            )
        else:
            verdict = (
                f"Mesh is export-eligible but has {len(warnings)} warning(s) a senior TA would address. "
                f"These won't block the pipeline today but will surface as harder-to-fix problems later "
                f"(especially in deformation and LOD reduction)."
            )

        # ── Playbook context ──────────────────────────────────────────────────
        pb = _get_active_playbook()
        playbook_context = None
        if pb:
            topo_min = pb.get("topology_score_min", 60)
            playbook_context = {
                "playbook":           pb["name"],
                "topology_score_min": topo_min,
                "topology_pass":      topo_score >= topo_min,
                "topology_verdict":   f"{topo_score}/100 — {'PASS' if topo_score >= topo_min else 'FAIL'} for {pb['name']} standard (min {topo_min})",
                "vert_budget":        pb["vert_budget"],
                "vert_count":         vert_count,
                "vert_budget_pass":   vert_count <= pb["vert_budget"],
            }

        # ── Build report ──────────────────────────────────────────────────────
        report = {
            "object":            object_name,
            "critique_verdict":  verdict,
            "topology_score":    topo_score,
            "topology_rating":   topo_rating,
            "priority_findings": priority_findings,
            "stats": {
                "vertices": vert_count,
                "faces":    face_count,
                "quads":    quad_count,
                "tris":     tri_count,
                "ngons":    ngon_count,
                "has_uvs":  has_uvs,
                "rigged":   has_arm,
            },
        }
        if compounding:
            report["compounding_issues"] = compounding
        if deformation_risk:
            report["deformation_risk"] = deformation_risk
        if playbook_context:
            report["playbook_context"] = playbook_context

        _session_append("verified_checks", "critique_mesh")
        return json.dumps(report, indent=2, default=str)

    except Exception as e:
        logger.error(f"Error in critique_mesh: {e}")
        return json.dumps({"error": str(e)})


# ─────────────────────────────────────────────────────────────────────────────
# PRODUCTION REVIEW MODE — v3.0
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def production_review(
    object_name: str,
    asset_type: str = "",
    include_rig: bool = False,
    include_animation: bool = False,
) -> str:
    """
    PRODUCTION REVIEW — One command. Full picture. Senior TA verdict.

    Conducts every relevant QA tool in sequence, aggregates findings, scores
    the asset 0–100, extracts strengths, lists critical blockers in priority
    order, and estimates how long to production-ready. Surfaces any conflicts
    between the stated asset_type and what the data shows — states the
    conflict and asks for confirmation rather than silently resolving it.

    This is the "show me everything" command. Use it at the start of a review
    session or any time you need a comprehensive status report.

    Parameters:
      object_name     : Blender object to review
      asset_type      : What kind of asset this is — "hero_character" | "weapon" |
                        "environment_prop" | "creature" | "vehicle" | "npc" |
                        "background_character" | "crowd_character"
                        Leave blank and the tool will state its inference.
      include_rig     : True → also run analyze_rig_weights + analyze_rig_skeleton
                        (requires Armature modifier on the object)
      include_animation : True → also run critique_animation

    Returns:
      production_score  : 0–100. 100 = fully production-ready.
      score_grade       : A / B / C / D / F
      assumed_asset_type: what the tool inferred (or used from asset_type param)
      conflicts         : list of conflicts between stated type and data findings —
                          each includes the conflict, what the data shows, and a
                          confirmation question for the user
      strengths         : list of things this asset does well
      critical_blockers : ordered list of things that must be fixed before export
      warnings          : non-blocking issues that should be addressed
      recommendations   : prioritised action list
      time_estimate     : plain-English estimate of work remaining
      session_updated   : True — session context updated with this review's findings
    """
    try:
        # ── Step 1: Gather baseline data ──────────────────────────────────────
        obj_info   = _send_raw("get_object_info",         name=object_name)
        mesh_stats = _send_raw("get_mesh_quality_report", name=object_name)
        if "error" in obj_info:
            return json.dumps({"error": f"Object not found: {object_name}"})

        # ── Step 2: Infer asset type (session > param > inference) ───────────
        session_type = _session_get("asset_type")
        if asset_type:
            effective_type = asset_type
        elif session_type:
            effective_type = session_type
        else:
            # Infer from signals
            mesh_block   = obj_info.get("mesh", {})
            vert_count   = mesh_block.get("vertices", 0) or 0
            has_arm_mod  = any(
                m.get("type") == "ARMATURE"
                for m in mesh_stats.get("modifiers", [])
                if isinstance(m, dict)
            )
            if has_arm_mod and vert_count > 40_000:
                effective_type = "hero_character"
            elif has_arm_mod:
                effective_type = "character"
            elif vert_count < 5_000:
                effective_type = "environment_prop"
            elif vert_count < 25_000:
                effective_type = "weapon_or_prop"
            else:
                effective_type = "unknown"

        # ── Step 3: Run core analysis tools ──────────────────────────────────
        raw_problems = _send_raw("detect_mesh_problems",    name=object_name)
        raw_quality  = _send_raw("get_mesh_quality_report", name=object_name)
        raw_topology = _send_raw("analyze_topology",        name=object_name)
        raw_ue5      = _send_raw("run_unreal_readiness_check", name=object_name)

        r_problems = _reason_mesh_problems(raw_problems) if "error" not in raw_problems else raw_problems
        r_quality  = _reason_mesh_quality(raw_quality)   if "error" not in raw_quality  else raw_quality
        r_topology = _reason_topology(raw_topology)      if "error" not in raw_topology else raw_topology
        r_ue5      = _reason_unreal_readiness(raw_ue5)   if "error" not in raw_ue5      else raw_ue5

        # Collect all findings
        all_findings = []
        for source, enriched in [
            ("mesh_problems",    r_problems),
            ("mesh_quality",     r_quality),
            ("topology",         r_topology),
            ("unreal_readiness", r_ue5),
        ]:
            for f in enriched.get("_reasoning", {}).get("findings", []):
                all_findings.append({**f, "source": source})

        # ── Step 4: Optional rig QA ───────────────────────────────────────────
        rig_findings = []
        rig_verdict  = None
        if include_rig:
            try:
                rig_w_json = analyze_rig_weights(object_name, verbose=False)
                rig_s_json = analyze_rig_skeleton(object_name, verbose=False)
                rig_w = json.loads(rig_w_json)
                rig_s = json.loads(rig_s_json)
                rig_verdict = rig_w.get("verdict", "UNKNOWN")
                # Promote rig criticals into all_findings
                for chk in rig_w.get("checks", []):
                    if chk.get("severity") in ("CRITICAL", "WARNING"):
                        all_findings.append({
                            "issue":    chk.get("detail", chk.get("check", "")),
                            "severity": chk.get("severity", "warning").lower(),
                            "source":   "rig_weights",
                        })
                for chk in rig_s.get("checks", []):
                    if chk.get("severity") in ("CRITICAL", "WARNING"):
                        all_findings.append({
                            "issue":    chk.get("detail", chk.get("check", "")),
                            "severity": chk.get("severity", "warning").lower(),
                            "source":   "rig_skeleton",
                        })
                _session_append("verified_checks", "analyze_rig_weights")
                _session_append("verified_checks", "analyze_rig_skeleton")
            except Exception as e:
                rig_findings.append({"note": f"Rig QA failed: {e}"})

        # ── Step 5: Optional animation critique ───────────────────────────────
        anim_summary = None
        if include_animation:
            try:
                anim_json = critique_animation(object_name)
                anim_data = json.loads(anim_json)
                # FIX: critique_animation's real top-level keys are "verdict"
                # (not "overall_verdict" — that's analyze_material_pbr's key,
                # borrowed here by mistake) and "critical_issues" (there's no
                # flat top-level "findings" list; it's pre-split into
                # critical_issues/warnings/info).
                anim_summary = {
                    "verdict":  anim_data.get("verdict", "Unknown"),
                    "blockers": anim_data.get("critical_issues", []),
                }
                _session_append("verified_checks", "critique_animation")
            except Exception as e:
                anim_summary = {"note": f"Animation critique failed: {e}"}

        # ── Step 6: Conflict detection ────────────────────────────────────────
        # Compare stated asset_type against what the data shows.
        # State the conflict clearly — ask for confirmation, don't silently resolve.
        conflicts = []
        vert_count = raw_quality.get("counts", {}).get("verts", 0) or 0
        face_types = raw_quality.get("face_types", {})
        ngon_count = face_types.get("ngons", 0) or 0
        has_uvs    = raw_quality.get("uv", {}).get("has_uvs", False)
        has_arm    = any(
            m.get("type") == "ARMATURE"
            for m in raw_quality.get("modifiers", [])
            if isinstance(m, dict)
        )

        # Vertex budget conflict
        BUDGET_LIMITS = {
            "hero_character":       80_000,
            "character":            80_000,
            "npc":                  30_000,
            "background_character": 15_000,
            "crowd_character":       8_000,
            "weapon":               15_000,
            "weapon_or_prop":       15_000,
            "environment_prop":     20_000,
            "vehicle":              60_000,
            "creature":             60_000,
        }
        limit = BUDGET_LIMITS.get(effective_type)
        if limit and vert_count > limit:
            ratio = vert_count / limit
            conflicts.append({
                "conflict":       "Vertex budget exceeded",
                "data_shows":     f"{vert_count:,} vertices — {ratio:.1f}× the typical {effective_type} limit ({limit:,})",
                "stated_type":    effective_type,
                "confirm_question": (
                    f"I see {vert_count:,} vertices. That's {ratio:.1f}× the typical {effective_type} budget. "
                    f"Is this intentional (e.g. cinematic asset, hero tier) or should I evaluate against a different asset type?"
                ),
            })
            _session_append("surfaced_conflicts",
                f"vert_budget: {vert_count:,} is {ratio:.1f}x {effective_type} limit")

        # Rig/no-rig conflict
        if "character" in effective_type and not has_arm:
            conflicts.append({
                "conflict":       "Character type stated but no Armature modifier found",
                "data_shows":     "No ARMATURE modifier on this object",
                "stated_type":    effective_type,
                "confirm_question": (
                    f"You described this as a {effective_type}, but I see no Armature modifier. "
                    "Is this pre-rig (Stage 1–4), or is the armature on a separate object?"
                ),
            })

        # No UV on textured-type asset
        if effective_type in ("hero_character", "character", "weapon", "weapon_or_prop", "vehicle") and not has_uvs:
            conflicts.append({
                "conflict":       "Asset type expects UVs but none found",
                "data_shows":     "No UV map present",
                "stated_type":    effective_type,
                "confirm_question": (
                    f"This is described as a {effective_type}, but it has no UV map. "
                    "Is this still in retopo/pre-UV stage? That changes the evaluation standards significantly."
                ),
            })

        # ── Step 7: Score 0–100 ───────────────────────────────────────────────
        # Start at 100, deduct per finding severity.
        # Scale deductions by asset type — characters are held to tighter standards.
        criticals = [f for f in all_findings if f.get("severity") == "critical"]
        warnings  = [f for f in all_findings if f.get("severity") == "warning"]
        infos     = [f for f in all_findings if f.get("severity") == "info"]

        score = 100
        score -= len(criticals) * 20
        score -= len(warnings)  * 5
        score -= len(infos)     * 1
        score -= len(conflicts) * 10   # conflicts cost score too
        score = max(0, min(100, score))

        if   score >= 90: grade = "A"
        elif score >= 75: grade = "B"
        elif score >= 55: grade = "C"
        elif score >= 35: grade = "D"
        else:             grade = "F"

        # ── Step 8: Extract strengths ─────────────────────────────────────────
        strengths = []
        if not any(f.get("source") == "mesh_problems" and f.get("severity") == "critical" for f in all_findings):
            strengths.append("Mesh geometry is clean — no non-manifold edges or degenerate faces")
        if has_uvs:
            strengths.append("UV map present — baking and texturing pipeline unblocked")
        topo_score = raw_topology.get("topology_score", 0) if "error" not in raw_topology else 0
        if topo_score >= 80:
            strengths.append(f"Strong topology score ({topo_score}/100) — quad-dominant, animation-friendly")
        elif topo_score >= 60:
            strengths.append(f"Acceptable topology score ({topo_score}/100)")
        ue5_reasoning = r_ue5.get("_reasoning", {})
        ue5_findings  = ue5_reasoning.get("findings", [])
        if not any(f.get("severity") == "critical" for f in ue5_findings):
            strengths.append("UE5 readiness check: no blocking export errors")
        if vert_count > 0 and limit and vert_count <= limit:
            strengths.append(f"Vertex count ({vert_count:,}) within {effective_type} budget ({limit:,})")
        if include_rig and rig_verdict == "PASS":
            strengths.append("Rig QA passed — weights and skeleton are export-ready")
        if not strengths:
            strengths.append("Asset is functional and repairable — blocking issues are known and fixable")

        # ── Step 9: Time estimate ─────────────────────────────────────────────
        total_issues = len(criticals) + len(warnings)
        auto_repairable = len(r_problems.get("_reasoning", {}).get("auto_repairable", []))
        manual_issues   = total_issues - auto_repairable
        conflict_time   = 5 * len(conflicts)  # minutes for clarification + re-eval

        if total_issues == 0 and not conflicts:
            time_est = "Production-ready now — no blocking issues found."
        elif len(criticals) == 0 and manual_issues <= 2:
            time_est = f"15–30 minutes — {len(warnings)} warning(s), mostly auto-repairable."
        elif len(criticals) <= 2 and manual_issues <= 4:
            time_est = f"30–90 minutes — {len(criticals)} critical issue(s) need artist review."
        elif len(criticals) <= 5:
            time_est = f"2–4 hours — {len(criticals)} critical issue(s) across multiple systems."
        else:
            time_est = f"Half-day or more — {len(criticals)} critical issues, significant rework required."
        if conflicts:
            time_est += f" Plus ~{conflict_time} min to clarify {len(conflicts)} type conflict(s) before re-evaluating."

        # ── Step 10: Build recommendations (ordered by severity) ──────────────
        recs = []
        if conflicts:
            for c in conflicts:
                recs.append(f"⚠️ CONFIRM: {c['confirm_question']}")
        for f in criticals:
            fix = f.get("professional_fix") or f.get("fix") or "Review and fix manually."
            recs.append(f"🚫 CRITICAL — {f['issue']}: {fix}")
        for f in warnings:
            fix = f.get("professional_fix") or f.get("fix") or "Review."
            recs.append(f"⚠️ WARN — {f['issue']}: {fix}")
        if not recs:
            recs.append("✅ No blocking issues — ready for export gate.")

        # ── Step 11: Update session context ──────────────────────────────────
        _session_set(active_object=object_name)
        if asset_type:
            _session_set(asset_type=asset_type)
        _session_append("verified_checks", "production_review")
        _session_append("verified_checks", "analyze_mesh_for_unreal")
        _session_append("verified_checks", "run_unreal_readiness_check")
        for f in criticals:
            issue_tag = f.get("issue", "")[:40]
            _session_append("open_issues", issue_tag)

        # ── Build final report ────────────────────────────────────────────────
        report = {
            "object":              object_name,
            "assumed_asset_type":  effective_type,
            "production_score":    score,
            "score_grade":         grade,
            "score_breakdown": {
                "started_at":   100,
                "critical_deductions": f"-{len(criticals) * 20} ({len(criticals)} critical × 20)",
                "warning_deductions":  f"-{len(warnings) * 5} ({len(warnings)} warnings × 5)",
                "conflict_deductions": f"-{len(conflicts) * 10} ({len(conflicts)} conflicts × 10)",
                "final_score":    score,
            },
            "conflicts":          conflicts,
            "strengths":          strengths,
            "critical_blockers":  criticals,
            "warnings":           warnings,
            "recommendations":    recs,
            "time_estimate":      time_est,
            "stats": {
                "vertex_count":  vert_count,
                "has_uvs":       has_uvs,
                "has_armature":  has_arm,
                "topology_score": topo_score,
                "total_findings": len(all_findings),
            },
        }

        if include_rig and rig_verdict is not None:
            report["rig_verdict"] = rig_verdict
        if include_animation and anim_summary:
            report["animation_summary"] = anim_summary
        if conflicts:
            report["_action_required"] = (
                "One or more conflicts detected between your stated asset type and the data. "
                "Please answer the confirm_question(s) in 'conflicts' before treating this score as final."
            )

        report["session_updated"] = True
        return json.dumps(report, indent=2, default=str)

    except Exception as e:
        logger.error(f"Error in production_review: {e}")
        return json.dumps({"error": str(e)})


# ─────────────────────────────────────────────────────────────────────────────
# SPATIAL INTELLIGENCE LAYER — v3.1
# Gives the LLM structured spatial relationships instead of raw coordinates.
# LLMs reason over "Lamp is 0.4m above Table" far better than over numbers.
# All data is extracted from Blender's existing spatial structures — no math
# libraries required, no training, no external APIs.
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def get_scene_graph(
    max_objects: int = 50,
    relationship_radius: float = 10.0,
    include_collections: bool = True,
) -> str:
    """
    SPATIAL INTELLIGENCE — Build a relationship graph of every object in the scene.

    Instead of describing objects individually, this tool returns HOW objects
    relate to each other spatially: what's above what, what's beside what,
    what's intersecting, what's touching the floor, what's inside what collection.

    The LLM can reason "Lamp is 0.4m above Table, Chair is 0.7m left of Table"
    far better than it can reason over raw position coordinates. This is the
    foundation of spatial judgment.

    Parameters:
      max_objects         : cap for full per-object detail (default 50).
                            Scenes above this cap get a collection-summary view.
      relationship_radius : only compute relationships between objects closer
                            than this distance in meters (default 10.0).
                            Prevents O(n²) explosion on large scenes.
      include_collections : include collection hierarchy in output (default True)

    Returns:
      mode              : "full" | "focused" | "summary"
      object_count      : total mesh objects in scene
      objects           : per-object spatial data (position, dimensions, nearest,
                          floor_contact, collection, parent, children)
      relationships     : list of {subject, predicate, object, distance} triples
                          predicates: above | below | beside | inside | intersecting
                                      | touching | contains | near
      collection_tree   : nested collection hierarchy (if include_collections)
      spatial_summary   : plain-English summary of what the scene looks like
    """
    script = r"""
import bpy
import json
import math
from mathutils import Vector
from mathutils.bvhtree import BVHTree

MAX_OBJECTS = {MAX_OBJECTS}
REL_RADIUS  = {REL_RADIUS}
INCLUDE_COL = {INCLUDE_COL}

depsgraph = bpy.context.evaluated_depsgraph_get()
scene     = bpy.context.scene

# ── Gather all mesh objects ────────────────────────────────────────────────
all_objects = [o for o in scene.objects if o.type == 'MESH' and not o.hide_viewport]
obj_count   = len(all_objects)

def world_center(obj):
    return obj.matrix_world.to_translation()

def world_dims(obj):
    # Dimensions already in world space (accounts for scale)
    return list(obj.dimensions)

def world_bbox(obj):
    return [list(obj.matrix_world @ Vector(c)) for c in obj.bound_box]

def bbox_min_max(obj):
    bbox = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    xs = [v.x for v in bbox]; ys = [v.y for v in bbox]; zs = [v.z for v in bbox]
    return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))

def direction_label(from_obj, to_obj):
    # Return primary spatial relationship direction from from_obj to to_obj
    fc = world_center(from_obj)
    tc = world_center(to_obj)
    dx = tc.x - fc.x
    dy = tc.y - fc.y
    dz = tc.z - fc.z
    adx, ady, adz = abs(dx), abs(dy), abs(dz)
    # Vertical dominates if it's at least 1.5x the horizontal distance
    if adz > max(adx, ady) * 1.5:
        return "above" if dz > 0 else "below"
    if adx >= ady:
        return "right" if dx > 0 else "left"
    return "forward" if dy > 0 else "behind"

def rel_predicate(from_obj, to_obj, dist):
    # Derive relationship predicate from geometry
    fc = world_center(from_obj)
    tc = world_center(to_obj)
    dz = tc.z - fc.z
    adz = abs(dz)
    adxy = math.sqrt((tc.x-fc.x)**2 + (tc.y-fc.y)**2)

    # Check bounding box containment (to_obj inside from_obj)
    fmin, fmax = bbox_min_max(from_obj)
    tcp = world_center(to_obj)
    if (fmin[0] < tcp.x < fmax[0] and
        fmin[1] < tcp.y < fmax[1] and
        fmin[2] < tcp.z < fmax[2]):
        return "contains"

    # Vertical relationship: one is clearly above the other
    if adz > adxy * 1.2:
        return "above" if dz > 0 else "below"

    # Touching: bounding boxes nearly adjacent (< 0.05m gap)
    fmin2, fmax2 = bbox_min_max(from_obj)
    tmin,  tmax  = bbox_min_max(to_obj)
    gap_x = max(0, max(fmin2[0], tmin[0]) - min(fmax2[0], tmax[0]))
    gap_y = max(0, max(fmin2[1], tmin[1]) - min(fmax2[1], tmax[1]))
    gap_z = max(0, max(fmin2[2], tmin[2]) - min(fmax2[2], tmax[2]))
    if max(gap_x, gap_y, gap_z) < 0.05:
        return "touching"

    return "beside"

def check_floor_contact(obj, threshold=0.15):
    # True if the objects lowest bounding box point is within threshold of z=0
    _, (_, _, z_max) = bbox_min_max(obj)
    min_pts = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    z_min = min(v.z for v in min_pts)
    return z_min < threshold

def check_intersecting_bvh(obj_a, obj_b):
    # True if the two meshes overlap using BVH tree intersection
    try:
        eval_a = obj_a.evaluated_get(depsgraph)
        eval_b = obj_b.evaluated_get(depsgraph)
        mesh_a = eval_a.to_mesh()
        mesh_b = eval_b.to_mesh()
        bvh_a  = BVHTree.FromPolygons(
            [obj_a.matrix_world @ Vector(v.co) for v in mesh_a.vertices],
            [list(p.vertices) for p in mesh_a.polygons]
        )
        bvh_b  = BVHTree.FromPolygons(
            [obj_b.matrix_world @ Vector(v.co) for v in mesh_b.vertices],
            [list(p.vertices) for p in mesh_b.polygons]
        )
        overlaps = bvh_a.overlap(bvh_b)
        eval_a.to_mesh_clear()
        eval_b.to_mesh_clear()
        return len(overlaps) > 0
    except Exception:
        return False

# ── Build per-object data ──────────────────────────────────────────────────
objects_data = {}
for obj in all_objects:
    center  = world_center(obj)
    dims    = world_dims(obj)
    cols    = [c.name for c in obj.users_collection]
    parent  = obj.parent.name if obj.parent else None
    children = [c.name for c in obj.children if c.type == 'MESH']

    # Nearest objects within radius — sorted by distance
    neighbors = []
    for other in all_objects:
        if other is obj:
            continue
        oc = world_center(other)
        dist = (center - oc).length
        if dist <= REL_RADIUS:
            neighbors.append({
                "name":      other.name,
                "distance":  round(dist, 3),
                "direction": direction_label(obj, other),
            })
    neighbors.sort(key=lambda n: n["distance"])

    objects_data[obj.name] = {
        "position":      [round(center.x, 3), round(center.y, 3), round(center.z, 3)],
        "dimensions":    [round(d, 3) for d in dims],
        "collections":   cols,
        "parent":        parent,
        "children":      children,
        "floor_contact": check_floor_contact(obj),
        "nearest":       neighbors[:8],  # cap at 8 nearest
        "vertex_count":  len(obj.data.vertices) if obj.data else 0,
        "face_count":    len(obj.data.polygons) if obj.data else 0,
    }

# ── Build relationship triples ─────────────────────────────────────────────
# Only between objects within REL_RADIUS of each other.
# Each pair generates one relationship (from the closer object's perspective).
relationships = []
processed = set()
for i, obj_a in enumerate(all_objects):
    ca = world_center(obj_a)
    for obj_b in all_objects[i+1:]:
        cb = world_center(obj_b)
        dist = (ca - cb).length
        if dist > REL_RADIUS:
            continue
        pair_key = tuple(sorted([obj_a.name, obj_b.name]))
        if pair_key in processed:
            continue
        processed.add(pair_key)

        # Check intersection first (most important relationship)
        if dist < 2.0:  # only check close pairs for performance
            intersects = check_intersecting_bvh(obj_a, obj_b)
            if intersects:
                relationships.append({
                    "subject":   obj_a.name,
                    "predicate": "intersecting",
                    "object":    obj_b.name,
                    "distance":  round(dist, 3),
                    "note":      "WARN: these objects overlap — likely a placement error",
                })
                continue

        pred = rel_predicate(obj_a, obj_b, dist)
        # Flip subject/object so predicate reads naturally
        # e.g. "Lamp above Table" not "Table below Lamp"
        dz = cb.z - ca.z
        if pred == "above":
            subject, object_ = obj_b.name, obj_a.name   # b is above a
            pred = "above"
        elif pred == "below":
            subject, object_ = obj_a.name, obj_b.name
            pred = "above"
        elif pred == "contains":
            subject, object_ = obj_a.name, obj_b.name
            pred = "contains"
        else:
            subject, object_ = obj_a.name, obj_b.name

        relationships.append({
            "subject":   subject,
            "predicate": pred,
            "object":    object_,
            "distance":  round(dist, 3),
        })

# ── Collection hierarchy ───────────────────────────────────────────────────
collection_tree = {}
if INCLUDE_COL:
    def col_to_dict(col):
        return {
            "objects":  [o.name for o in col.objects if o.type == 'MESH'],
            "children": {c.name: col_to_dict(c) for c in col.children},
        }
    collection_tree = col_to_dict(bpy.context.scene.collection)

# ── Plain-English spatial summary ─────────────────────────────────────────
intersecting_pairs = [(r["subject"], r["object"]) for r in relationships if r["predicate"] == "intersecting"]
above_rels   = [(r["subject"], r["object"]) for r in relationships if r["predicate"] == "above"]
contain_rels = [(r["subject"], r["object"]) for r in relationships if r["predicate"] == "contains"]

summary_parts = [f"{obj_count} mesh object(s) in scene."]
if intersecting_pairs:
    pairs_str = ", ".join(f"{a}+{b}" for a,b in intersecting_pairs[:3])
    summary_parts.append(f"WARN: {len(intersecting_pairs)} intersecting pair(s): {pairs_str}.")
if above_rels:
    sample = ", ".join(f"{a} above {b}" for a,b in above_rels[:3])
    summary_parts.append(f"Vertical relationships: {sample}{'...' if len(above_rels) > 3 else ''}.")
if contain_rels:
    sample = ", ".join(f"{a} contains {b}" for a,b in contain_rels[:3])
    summary_parts.append(f"Containment: {sample}.")
floor_contacts = [n for n, d in objects_data.items() if d["floor_contact"]]
if floor_contacts:
    summary_parts.append(f"{len(floor_contacts)} object(s) on/near floor: {', '.join(floor_contacts[:5])}{'...' if len(floor_contacts) > 5 else ''}.")

spatial_summary = " ".join(summary_parts)

result = {
    "mode":             "full" if obj_count <= MAX_OBJECTS else "summary",
    "object_count":     obj_count,
    "objects":          objects_data if obj_count <= MAX_OBJECTS else {},
    "relationships":    relationships,
    "spatial_summary":  spatial_summary,
}
if INCLUDE_COL:
    result["collection_tree"] = collection_tree
if obj_count > MAX_OBJECTS:
    result["_note"] = f"Scene has {obj_count} objects — showing relationships only. Use describe_object_context(name) for per-object detail."

print(json.dumps(result))
""".replace("{MAX_OBJECTS}", str(max_objects)) \
   .replace("{REL_RADIUS}",  str(relationship_radius)) \
   .replace("{INCLUDE_COL}", "True" if include_collections else "False")

    try:
        blender = get_blender_connection()
        raw = blender.send_command("execute_code_safe", {"code": script, "required_mode": "OBJECT", "push_undo": False})
        if "error" in raw:
            return json.dumps({"error": raw["error"]})
        output = raw.get("result", "")
        # Find the JSON line in stdout
        for line in output.strip().splitlines():
            line = line.strip()
            if line.startswith("{"):
                data = json.loads(line)
                # Update session with active scene state
                _session_set(active_object=_session_get("active_object"))
                return json.dumps(data, indent=2, default=str)
        return json.dumps({"error": "No JSON output from scene graph script", "raw": output})
    except Exception as e:
        logger.error(f"Error in get_scene_graph: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def query_spatial(
    query_type: str,
    object_name: str = "",
    radius: float = 5.0,
    origin: list = None,
    direction: list = None,
    count: int = 5,
) -> str:
    """
    SPATIAL QUERY ENGINE — Ask precise spatial questions about the scene.

    Instead of reading the whole scene graph, use this to answer targeted
    spatial questions: what's nearest to this object, what's in this radius,
    what does a ray hit, what is this object sitting on.

    query_type options:
      "nearest"      → find the N closest objects to object_name
                        params: object_name, count (default 5)
      "in_radius"    → find all objects within radius of object_name's center
                        params: object_name, radius (default 5.0m)
      "intersecting" → find all objects whose geometry overlaps object_name
                        params: object_name
      "supporting"   → find what object_name is resting on (raycast downward)
                        params: object_name
      "above"        → find all objects directly above object_name
                        params: object_name, radius (search cone radius)
      "below"        → find all objects directly below object_name
                        params: object_name, radius
      "raycast"      → cast a ray from origin in direction, return first hit
                        params: origin [x,y,z], direction [x,y,z]
      "floating"     → find all objects with no floor contact and nothing below them
                        (no params needed — scene-wide check)
      "isolated"     → find all objects with no neighbors within radius
                        params: radius (default 5.0m)

    Returns targeted spatial answer with object names, distances, directions.
    """
    script_map = {

"nearest": r"""
import bpy, json, math
from mathutils import Vector

obj = bpy.data.objects.get('{OBJ}')
if obj is None:
    print(json.dumps({"error": "Object not found: {OBJ}"}))
else:
    center = obj.matrix_world.to_translation()
    others = [o for o in bpy.context.scene.objects if o.type == 'MESH' and o is not obj and not o.hide_viewport]
    dists = []
    for o in others:
        oc = o.matrix_world.to_translation()
        d  = (center - oc).length
        dists.append({"name": o.name, "distance": round(d,3), "position": [round(oc.x,3),round(oc.y,3),round(oc.z,3)]})
    dists.sort(key=lambda x: x["distance"])
    print(json.dumps({"query": "nearest", "reference": "{OBJ}", "results": dists[:{COUNT}]}))
""",

"in_radius": r"""
import bpy, json
from mathutils import Vector

obj = bpy.data.objects.get('{OBJ}')
if obj is None:
    print(json.dumps({"error": "Object not found: {OBJ}"}))
else:
    center = obj.matrix_world.to_translation()
    radius = {RADIUS}
    found  = []
    for o in bpy.context.scene.objects:
        if o.type != 'MESH' or o is obj or o.hide_viewport:
            continue
        oc = o.matrix_world.to_translation()
        d  = (center - oc).length
        if d <= radius:
            found.append({"name": o.name, "distance": round(d,3)})
    found.sort(key=lambda x: x["distance"])
    print(json.dumps({"query": "in_radius", "reference": "{OBJ}", "radius": radius, "count": len(found), "results": found}))
""",

"intersecting": r"""
import bpy, json
from mathutils import Vector
from mathutils.bvhtree import BVHTree

obj = bpy.data.objects.get('{OBJ}')
if obj is None:
    print(json.dumps({"error": "Object not found: {OBJ}"}))
else:
    depsgraph = bpy.context.evaluated_depsgraph_get()
    eval_a = obj.evaluated_get(depsgraph)
    mesh_a = eval_a.to_mesh()
    bvh_a  = BVHTree.FromPolygons(
        [obj.matrix_world @ v.co for v in mesh_a.vertices],
        [list(p.vertices) for p in mesh_a.polygons]
    )
    eval_a.to_mesh_clear()
    found = []
    for other in bpy.context.scene.objects:
        if other.type != 'MESH' or other is obj or other.hide_viewport:
            continue
        oc   = other.matrix_world.to_translation()
        dist = (obj.matrix_world.to_translation() - oc).length
        if dist > 10.0:  # skip distant objects
            continue
        try:
            eval_b = other.evaluated_get(depsgraph)
            mesh_b = eval_b.to_mesh()
            bvh_b  = BVHTree.FromPolygons(
                [other.matrix_world @ v.co for v in mesh_b.vertices],
                [list(p.vertices) for p in mesh_b.polygons]
            )
            eval_b.to_mesh_clear()
            overlaps = bvh_a.overlap(bvh_b)
            if overlaps:
                found.append({"name": other.name, "overlap_pairs": len(overlaps), "distance": round(dist,3)})
        except Exception:
            pass
    print(json.dumps({"query": "intersecting", "reference": "{OBJ}", "count": len(found), "results": found,
        "verdict": "WARN: intersecting objects detected — likely placement errors" if found else "PASS: no intersections"}))
""",

"supporting": r"""
import bpy, json
from mathutils import Vector

obj = bpy.data.objects.get('{OBJ}')
if obj is None:
    print(json.dumps({"error": "Object not found: {OBJ}"}))
else:
    depsgraph = bpy.context.evaluated_depsgraph_get()
    # Find lowest point of bounding box
    bbox_pts  = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    z_min     = min(v.z for v in bbox_pts)
    center_xy = obj.matrix_world.to_translation()
    origin    = Vector((center_xy.x, center_xy.y, z_min - 0.001))
    direction = Vector((0, 0, -1))
    hit, loc, normal, idx, hit_obj, matrix = bpy.context.scene.ray_cast(depsgraph, origin, direction)
    if hit and hit_obj:
        dist = (origin - loc).length
        print(json.dumps({"query": "supporting", "reference": "{OBJ}",
            "supported_by": hit_obj.name,
            "gap": round(dist, 4),
            "contact": dist < 0.05,
            "hit_location": [round(loc.x,3), round(loc.y,3), round(loc.z,3)]}))
    else:
        print(json.dumps({"query": "supporting", "reference": "{OBJ}",
            "supported_by": None, "gap": None, "contact": False,
            "note": "Nothing found below this object — it may be floating"}))
""",

"above": r"""
import bpy, json
from mathutils import Vector

obj = bpy.data.objects.get('{OBJ}')
if obj is None:
    print(json.dumps({"error": "Object not found: {OBJ}"}))
else:
    center = obj.matrix_world.to_translation()
    radius = {RADIUS}
    found  = []
    for o in bpy.context.scene.objects:
        if o.type != 'MESH' or o is obj or o.hide_viewport:
            continue
        oc  = o.matrix_world.to_translation()
        dz  = oc.z - center.z
        dxy = ((oc.x-center.x)**2 + (oc.y-center.y)**2) ** 0.5
        if dz > 0 and dxy <= radius:
            found.append({"name": o.name, "height_above": round(dz,3), "lateral_offset": round(dxy,3)})
    found.sort(key=lambda x: x["height_above"])
    print(json.dumps({"query": "above", "reference": "{OBJ}", "count": len(found), "results": found}))
""",

"below": r"""
import bpy, json
from mathutils import Vector

obj = bpy.data.objects.get('{OBJ}')
if obj is None:
    print(json.dumps({"error": "Object not found: {OBJ}"}))
else:
    center = obj.matrix_world.to_translation()
    radius = {RADIUS}
    found  = []
    for o in bpy.context.scene.objects:
        if o.type != 'MESH' or o is obj or o.hide_viewport:
            continue
        oc  = o.matrix_world.to_translation()
        dz  = center.z - oc.z
        dxy = ((oc.x-center.x)**2 + (oc.y-center.y)**2) ** 0.5
        if dz > 0 and dxy <= radius:
            found.append({"name": o.name, "depth_below": round(dz,3), "lateral_offset": round(dxy,3)})
    found.sort(key=lambda x: x["depth_below"])
    print(json.dumps({"query": "below", "reference": "{OBJ}", "count": len(found), "results": found}))
""",

"raycast": r"""
import bpy, json
from mathutils import Vector

depsgraph = bpy.context.evaluated_depsgraph_get()
origin    = Vector(({OX}, {OY}, {OZ}))
direction = Vector(({DX}, {DY}, {DZ}))
hit, loc, normal, idx, hit_obj, matrix = bpy.context.scene.ray_cast(depsgraph, origin, direction)
if hit and hit_obj:
    dist = (origin - loc).length
    print(json.dumps({"query": "raycast", "hit": True, "object": hit_obj.name,
        "distance": round(dist,3),
        "hit_location": [round(loc.x,3), round(loc.y,3), round(loc.z,3)],
        "face_index": idx}))
else:
    print(json.dumps({"query": "raycast", "hit": False, "object": None, "distance": None}))
""",

"floating": r"""
import bpy, json
from mathutils import Vector

depsgraph = bpy.context.evaluated_depsgraph_get()
floating  = []
for obj in bpy.context.scene.objects:
    if obj.type != 'MESH' or obj.hide_viewport:
        continue
    bbox_pts = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    z_min    = min(v.z for v in bbox_pts)
    if z_min < 0.1:   # close enough to z=0 — not floating
        continue
    center_xy = obj.matrix_world.to_translation()
    origin    = Vector((center_xy.x, center_xy.y, z_min - 0.001))
    direction = Vector((0, 0, -1))
    hit, loc, normal, idx, hit_obj, matrix = bpy.context.scene.ray_cast(depsgraph, origin, direction)
    if not hit:
        floating.append({"name": obj.name, "lowest_z": round(z_min,3), "note": "nothing below"})
    elif (origin - loc).length > 0.5:
        floating.append({"name": obj.name, "lowest_z": round(z_min,3), "gap_to_nearest_below": round((origin-loc).length,3)})
verdict = f"WARN: {len(floating)} floating object(s) detected" if floating else "PASS: all objects have support or floor contact"
print(json.dumps({"query": "floating", "floating_count": len(floating), "results": floating, "verdict": verdict}))
""",

"isolated": r"""
import bpy, json
from mathutils import Vector

radius   = {RADIUS}
isolated = []
meshes   = [o for o in bpy.context.scene.objects if o.type == 'MESH' and not o.hide_viewport]
for obj in meshes:
    center = obj.matrix_world.to_translation()
    has_neighbor = False
    for other in meshes:
        if other is obj:
            continue
        dist = (center - other.matrix_world.to_translation()).length
        if dist <= radius:
            has_neighbor = True
            break
    if not has_neighbor:
        isolated.append({"name": obj.name, "position": [round(center.x,3), round(center.y,3), round(center.z,3)]})
print(json.dumps({"query": "isolated", "radius": radius, "isolated_count": len(isolated),
    "results": isolated,
    "note": f"Objects with no neighbors within {radius}m — may be misplaced"}))
""",
    }

    try:
        if query_type not in script_map:
            available = list(script_map.keys())
            return json.dumps({"error": f"Unknown query_type '{query_type}'. Available: {available}"})

        script = script_map[query_type]

        # Substitute parameters
        script = script.replace("{OBJ}",    object_name)
        script = script.replace("{COUNT}",  str(count))
        script = script.replace("{RADIUS}", str(radius))

        # Raycast origin/direction
        if query_type == "raycast":
            o = origin    or [0.0, 0.0, 0.0]
            d = direction or [0.0, 0.0, -1.0]
            script = script.replace("{OX}", str(o[0])).replace("{OY}", str(o[1])).replace("{OZ}", str(o[2]))
            script = script.replace("{DX}", str(d[0])).replace("{DY}", str(d[1])).replace("{DZ}", str(d[2]))

        blender = get_blender_connection()
        raw = blender.send_command("execute_code_safe", {"code": script, "required_mode": "OBJECT", "push_undo": False})
        if "error" in raw:
            return json.dumps({"error": raw["error"]})

        output = raw.get("result", "")
        for line in output.strip().splitlines():
            line = line.strip()
            if line.startswith("{"):
                return json.dumps(json.loads(line), indent=2, default=str)

        return json.dumps({"error": "No JSON output from spatial query", "raw": output})

    except Exception as e:
        logger.error(f"Error in query_spatial: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def describe_object_context(object_name: str) -> str:
    """
    SPATIAL CONTEXT — Rich semantic description of a single object and its surroundings.

    Instead of "Cube.004 — 12,840 vertices", returns:
      "Cube.004 — likely a seat or platform (dimensions suggest furniture scale).
       Sits 0.03m above Floor.001. 0.7m left of Table.003. 1.3m from Lamp.002
       (above-left). Inside collection 'LivingRoom'. No parent. 2 children.
       No intersections detected."

    This is what the LLM should read before making any spatial decision about
    an object — it gives relationship context, not just geometry stats.

    Uses a combination of:
    - Exact world-space position and dimensions from matrix_world
    - BVH intersection check against nearby objects
    - Raycast downward for floor/support detection
    - Name-based semantic inference (chair, table, lamp, wall, floor, etc.)
    - Nearest neighbor relationships with direction labels

    Returns:
      object_name      : confirmed object name
      semantic_role    : inferred role from name + geometry signals
      position         : world center [x, y, z]
      dimensions       : [w, d, h] in meters
      collections      : which collections this object belongs to
      parent           : parent object name or null
      children         : list of child object names
      floor_contact    : True if resting on/near z=0 plane
      supported_by     : name of object directly below (from raycast), or null
      nearest          : top 5 nearest objects with distance + direction
      intersecting     : list of objects whose geometry overlaps this one
      spatial_sentence : one plain-English sentence summarising the object's
                         spatial context — ready to paste into a prompt
    """
    script = r"""
import bpy, json, math, re
from mathutils import Vector
from mathutils.bvhtree import BVHTree

OBJ_NAME = '{OBJ_NAME}'
obj = bpy.data.objects.get(OBJ_NAME)
if obj is None:
    print(json.dumps({"error": f"Object not found: {OBJ_NAME}"}))
else:
    depsgraph = bpy.context.evaluated_depsgraph_get()
    center    = obj.matrix_world.to_translation()
    dims      = obj.dimensions

    # ── Semantic role inference ──────────────────────────────────────────
    name_lower = obj.name.lower()
    role = "mesh object"
    role_map = [
        (["chair","seat","stool","bench","throne"],        "seat / furniture"),
        (["table","desk","counter","shelf","ledge"],       "surface / furniture"),
        (["lamp","light","lantern","torch","sconce"],      "light fixture"),
        (["floor","ground","terrain","plane"],             "floor / ground surface"),
        (["wall","partition","divider"],                   "wall / partition"),
        (["door","gate","portal","hatch"],                 "door / opening"),
        (["window","glass","pane"],                        "window"),
        (["box","crate","container","chest","bin"],        "container"),
        (["sword","weapon","gun","rifle","blade","bow"],   "weapon"),
        (["tree","bush","plant","flower","grass"],         "vegetation"),
        (["rock","stone","boulder","cliff"],               "rock / terrain"),
        (["car","truck","vehicle","wheel","tire"],         "vehicle"),
        (["character","human","person","npc","enemy",
          "zombie","monster","creature","undead","boss","villain"], "character"),
        (["pillar","column","post","pole"],                "structural column"),
        (["roof","ceiling","canopy"],                      "ceiling / roof"),
    ]
    for keywords, label in role_map:
        if any(k in name_lower for k in keywords):
            role = label
            break
    # Geometry-based role fallback
    if role == "mesh object":
        h, w, d = dims.z, dims.x, dims.y
        if h < 0.15 and w > 1.0:
            role = "floor / flat surface"
        elif w > 5.0 and h < 1.0:
            role = "large flat surface (floor/terrain)"
        elif h > 2.0 and w < 0.5:
            role = "vertical structure (wall/pillar)"
        elif h > 0.5 and w < 2.0 and d < 2.0:
            role = "mid-scale prop"

    # ── Nearest objects ──────────────────────────────────────────────────
    def dir_label(fc, tc):
        dx, dy, dz = tc.x-fc.x, tc.y-fc.y, tc.z-fc.z
        adx, ady, adz = abs(dx), abs(dy), abs(dz)
        if adz > max(adx, ady) * 1.5:
            return "above" if dz > 0 else "below"
        return ("right" if dx > 0 else "left") if adx >= ady else ("forward" if dy > 0 else "behind")

    meshes   = [o for o in bpy.context.scene.objects if o.type == 'MESH' and o is not obj and not o.hide_viewport]
    nearest  = []
    for o in meshes:
        oc   = o.matrix_world.to_translation()
        dist = (center - oc).length
        nearest.append({"name": o.name, "distance": round(dist,3), "direction": dir_label(center,oc)})
    nearest.sort(key=lambda x: x["distance"])
    nearest = nearest[:5]

    # ── Floor contact ────────────────────────────────────────────────────
    bbox_pts  = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    z_min     = min(v.z for v in bbox_pts)
    floor_contact = z_min < 0.12

    # ── Supported by (raycast down) ──────────────────────────────────────
    ray_origin = Vector((center.x, center.y, z_min - 0.001))
    hit, loc, _, _, hit_obj, _ = bpy.context.scene.ray_cast(depsgraph, ray_origin, Vector((0,0,-1)))
    supported_by = hit_obj.name if (hit and hit_obj and (ray_origin - loc).length < 0.5) else None

    # ── Intersecting objects (BVH) ───────────────────────────────────────
    intersecting = []
    try:
        eval_a = obj.evaluated_get(depsgraph)
        mesh_a = eval_a.to_mesh()
        bvh_a  = BVHTree.FromPolygons(
            [obj.matrix_world @ v.co for v in mesh_a.vertices],
            [list(p.vertices) for p in mesh_a.polygons]
        )
        eval_a.to_mesh_clear()
        for other in meshes:
            dist = (center - other.matrix_world.to_translation()).length
            if dist > 5.0:
                continue
            try:
                eval_b = other.evaluated_get(depsgraph)
                mesh_b = eval_b.to_mesh()
                bvh_b  = BVHTree.FromPolygons(
                    [other.matrix_world @ v.co for v in mesh_b.vertices],
                    [list(p.vertices) for p in mesh_b.polygons]
                )
                eval_b.to_mesh_clear()
                if bvh_a.overlap(bvh_b):
                    intersecting.append(other.name)
            except Exception:
                pass
    except Exception:
        pass

    # ── Spatial sentence ─────────────────────────────────────────────────
    parts = [f"{OBJ_NAME} ({role})"]
    if supported_by:
        parts.append(f"resting on {supported_by}")
    elif floor_contact:
        parts.append("on/near the floor")
    elif not supported_by:
        parts.append("floating (nothing directly below)")
    if nearest:
        nn = nearest[0]
        # "above"/"below" read naturally on their own; other directions need "of"
        joiner = "" if nn['direction'] in ("above", "below") else " of"
        parts.append(f"{nn['distance']}m {nn['direction']}{joiner} {nn['name']}")
    if len(nearest) > 1:
        others_str = ", ".join(f"{n['name']} ({n['distance']}m)" for n in nearest[1:3])
        parts.append(f"near {others_str}")
    cols = [c.name for c in obj.users_collection]
    if cols:
        parts.append(f"in collection '{', '.join(cols)}'")
    if intersecting:
        parts.append(f"WARN: intersecting {', '.join(intersecting)}")
    spatial_sentence = "; ".join(parts) + "."

    print(json.dumps({
        "object_name":     OBJ_NAME,
        "semantic_role":   role,
        "position":        [round(center.x,3), round(center.y,3), round(center.z,3)],
        "dimensions":      {"w": round(dims.x,3), "d": round(dims.y,3), "h": round(dims.z,3)},
        "collections":     [c.name for c in obj.users_collection],
        "parent":          obj.parent.name if obj.parent else None,
        "children":        [c.name for c in obj.children if c.type == 'MESH'],
        "floor_contact":   floor_contact,
        "supported_by":    supported_by,
        "nearest":         nearest,
        "intersecting":    intersecting,
        "spatial_sentence": spatial_sentence,
        "vertex_count":    len(obj.data.vertices) if obj.data else 0,
    }))
""".replace("{OBJ_NAME}", object_name)

    try:
        blender = get_blender_connection()
        raw = blender.send_command("execute_code_safe", {"code": script, "required_mode": "OBJECT", "push_undo": False})
        if "error" in raw:
            return json.dumps({"error": raw["error"]})
        output = raw.get("result", "")
        for line in output.strip().splitlines():
            line = line.strip()
            if line.startswith("{"):
                return json.dumps(json.loads(line), indent=2, default=str)
        return json.dumps({"error": "No JSON output from describe_object_context", "raw": output})
    except Exception as e:
        logger.error(f"Error in describe_object_context: {e}")
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

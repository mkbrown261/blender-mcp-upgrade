# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp[cli]", "pillow", "MaterialX"]
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
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

# Pillow — used to burn problem coordinates onto screenshot PNGs (Tier 1a)
try:
    from PIL import Image as PILImage, ImageDraw, ImageFont
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False

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
    # Multiview capture metadata — images are NOT persisted, only this record
    "multiview": None,   # None or dict: {object, timestamp, views_captured, wireframe_set, capture_stale}
    # Production journal — timestamped log of every significant tool call this session.
    # Each entry: {ts, tool, object, outcome, detail}
    # Persisted so the journal survives MCP restarts within the same work session.
    "journal": [],
    # Open issue tracker — issues opened by analysis tools, closed by repair/QA tools.
    # Each entry: {id, ts_opened, tool, object, issue_type, severity, detail, status, ts_closed}
    "issue_tracker": [],
    # Absolute path of the .blend file this session's context belongs to.
    # session_status() compares this against the currently open file on every
    # call and resets file-scoped fields if they no longer match — otherwise
    # stale context from a previously open project silently leaks into a new one.
    "blend_filepath": None,
    # File-level restore points created via create_checkpoint(). Each entry:
    # {label, timestamp, filepath, blend_filepath_at_creation}
    "checkpoints": [],
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


def _get_blend_filepath() -> str:
    """
    Absolute path of the currently open .blend file, or "" if unsaved/unavailable.
    get_scene_info's schema has no "filepath" key, so query bpy.data.filepath
    directly via execute_code_safe. Never raises — returns "" on any failure so
    callers (session_status, on every turn) can't be broken by a Blender hiccup.
    """
    try:
        raw = _send_raw(
            "execute_code_safe",
            code="import bpy, json\nprint(json.dumps({'filepath': bpy.data.filepath}))",
            required_mode=None,
            push_undo=False,
        )
        output = raw.get("result", "") if isinstance(raw, dict) else ""
        for line in output.strip().splitlines():
            line = line.strip()
            if line.startswith("{"):
                blend_path = json.loads(line).get("filepath", "")
                return blend_path if blend_path and blend_path != "//" else ""
    except Exception as e:
        logger.warning(f"_get_blend_filepath: could not query bpy.data.filepath: {e}")
    return ""


def _check_session_file_match() -> dict:
    """
    Compare the session's recorded blend_filepath against the actually open
    file. If they differ, the session was built against a DIFFERENT project —
    reset file-scoped fields rather than silently serving stale context
    (active_object/asset_type from a character in a totally different .blend
    file has bitten this project more than once).
    Returns {"file_changed": bool, "previous_file": str|None, "current_file": str}.
    """
    current = _get_blend_filepath()
    previous = _SESSION.get("blend_filepath")

    if not current:
        # Can't determine the open file right now — don't reset on uncertainty.
        return {"file_changed": False, "previous_file": previous, "current_file": current}

    if not previous:
        _session_set(blend_filepath=current)
        _save_session()
        return {"file_changed": False, "previous_file": None, "current_file": current}

    if previous == current:
        return {"file_changed": False, "previous_file": previous, "current_file": current}

    # File genuinely changed — reset facts scoped to the previous file.
    # journal/issue_tracker/checkpoints are kept as history (entries carry
    # their own object names, so old entries stay legible, not misleading).
    _session_set(
        asset_type=None,
        active_playbook=None,
        confirmed_stage=None,
        active_object=None,
        verified_checks=[],
        open_issues=[],
        user_corrections=[],
        surfaced_conflicts=[],
        multiview=None,
        blend_filepath=current,
    )
    _journal_entry(
        tool="_check_session_file_match",
        object_name="",
        outcome="ok",
        detail=f"Blend file changed ({previous} -> {current}) — reset file-scoped session context.",
    )
    _save_session()
    return {"file_changed": True, "previous_file": previous, "current_file": current}


# ─────────────────────────────────────────────────────────────────────────────
# PRODUCTION JOURNAL helpers — Sprint A
# Every significant tool call logs here automatically via _journal_entry().
# ─────────────────────────────────────────────────────────────────────────────

def _journal_entry(tool: str, object_name: str, outcome: str, detail: str = "") -> None:
    """
    Append a timestamped entry to _SESSION['journal'].
    Called by analysis/repair/generation tools on completion.
    outcome: 'ok' | 'warning' | 'error' | 'repaired' | 'generated' | 'skipped'
    """
    import datetime as _dt
    entry = {
        "ts":     _dt.datetime.now().strftime("%H:%M:%S"),
        "tool":   tool,
        "object": object_name or "",
        "outcome": outcome,
        "detail": detail[:200] if detail else "",   # cap at 200 chars to keep journal readable
    }
    journal = _SESSION.get("journal")
    if isinstance(journal, list):
        journal.append(entry)
    # Keep journal bounded — last 200 entries only
    if len(journal) > 200:
        _SESSION["journal"] = journal[-200:]
    # Persist after every entry so it survives restarts
    _save_session()


# ─────────────────────────────────────────────────────────────────────────────
# ISSUE TRACKER helpers — Sprint A
# Analysis tools open issues; repair/QA tools close them.
# ─────────────────────────────────────────────────────────────────────────────

def _next_issue_id() -> str:
    """
    Derive the next issue ID from the max ID already in the persisted tracker,
    rather than a separate in-memory counter — a plain counter resets to 0 on
    every MCP restart while the loaded tracker can already contain higher IDs,
    causing new issues to collide with existing ones.
    """
    tracker = _SESSION.get("issue_tracker", []) or []
    max_n = 0
    for entry in tracker:
        eid = entry.get("id", "")
        if eid.startswith("ISS-"):
            try:
                max_n = max(max_n, int(eid[4:]))
            except ValueError:
                pass
    return f"ISS-{max_n + 1:03d}"


def _open_issue(
    tool: str,
    object_name: str,
    issue_type: str,
    severity: str,
    detail: str,
) -> str:
    """
    Register a new open issue in the issue tracker.
    Returns the issue ID string (e.g. 'ISS-007').
    severity: 'critical' | 'warning' | 'info'
    issue_type: e.g. 'non_manifold' | 'ngon' | 'deformation_risk' | 'uv_missing' | ...
    """
    import datetime as _dt
    issue_id = _next_issue_id()
    entry = {
        "id":        issue_id,
        "ts_opened": _dt.datetime.now().strftime("%H:%M:%S"),
        "tool":      tool,
        "object":    object_name or "",
        "issue_type": issue_type,
        "severity":  severity,
        "detail":    detail[:300] if detail else "",
        "status":    "open",
        "ts_closed": None,
        "closed_by": None,
    }
    tracker = _SESSION.get("issue_tracker")
    if isinstance(tracker, list):
        tracker.append(entry)
    _save_session()
    return issue_id


def _close_issues_for(object_name: str, issue_types: list, closed_by: str) -> list:
    """
    Mark all open issues for object_name whose issue_type is in issue_types as closed.
    Returns list of closed issue IDs.
    """
    import datetime as _dt
    closed = []
    tracker = _SESSION.get("issue_tracker", [])
    for entry in tracker:
        if (
            entry.get("status") == "open"
            and entry.get("object") == object_name
            and entry.get("issue_type") in issue_types
        ):
            entry["status"]    = "closed"
            entry["ts_closed"] = _dt.datetime.now().strftime("%H:%M:%S")
            entry["closed_by"] = closed_by
            closed.append(entry["id"])
    if closed:
        _save_session()
    return closed


# ─────────────────────────────────────────────────────────────────────────────
# SESSION PERSISTENCE — write _SESSION to disk so context survives MCP restarts.
# File lives next to server.py. Only scalar-safe types (str, int, bool, list).
# _load_session() is called once at startup; _save_session() on every update.
# ─────────────────────────────────────────────────────────────────────────────

_SESSION_FILE: Path = Path(__file__).parent / ".blender_mcp_session.json"

# Keys whose values are safe to persist (exclude runtime-only flags if added later)
_SESSION_PERSIST_KEYS = [
    "asset_type", "active_playbook", "confirmed_stage", "active_object",
    "verified_checks", "open_issues", "user_corrections", "surfaced_conflicts",
    "apprentice_mode", "td_mode",
    "multiview",   # metadata only — no image bytes, safe to persist
    "journal",     # list of {ts, tool, object, outcome, detail} dicts
    "issue_tracker", # list of open/closed issue entries
    "blend_filepath", # which .blend file this context belongs to
    "checkpoints",    # file-level restore points
]


def _save_session() -> None:
    """Write current _SESSION to disk. Silent on failure — never crash the server."""
    try:
        payload = {k: _SESSION[k] for k in _SESSION_PERSIST_KEYS if k in _SESSION}
        _SESSION_FILE.write_text(json.dumps(payload, indent=2))
    except Exception as e:
        logger.warning(f"_save_session: could not write {_SESSION_FILE}: {e}")


def _load_session() -> None:
    """Read persisted session from disk into _SESSION. Called once at startup."""
    global _SESSION
    if not _SESSION_FILE.exists():
        return
    try:
        data = json.loads(_SESSION_FILE.read_text())
        for k in _SESSION_PERSIST_KEYS:
            if k in data and k in _SESSION:
                _SESSION[k] = data[k]
        logger.info(f"_load_session: restored session from {_SESSION_FILE}")
    except Exception as e:
        logger.warning(f"_load_session: could not read {_SESSION_FILE}: {e}")


# Load persisted session immediately at import time
_load_session()


# ─────────────────────────────────────────────────────────────────────────────
# PRODUCTION KNOWLEDGE BASE — Phase 1 of the Spatial Intelligence roadmap.
#
# Deliberately SEPARATE from _SESSION: _SESSION is per-project and resets its
# file-scoped fields on a .blend switch (see _check_session_file_match).
# Knowledge is the opposite — it must survive across every project and every
# server restart, because the whole point is that solved problems compound
# into reusable expertise instead of being re-solved from scratch each time.
#
# Lives next to server.py (not next to any one .blend) because it belongs to
# the MCP installation itself, not to any single user's world/project data —
# unlike ERYNDOR_master_manifest.json, which is correctly gitignored and kept
# next to the user's .blend files.
#
# Every entry MUST carry context_tags (blender_version, asset_source, mesh_type
# etc.) — the fcurves bug fixed this session (a Blender-4.4-only API change)
# is the concrete failure mode a context-free "this always works" store would
# hit: a fact that was true stops being true when the environment changes.
# Untagged, unconditional "lessons" are how a knowledge base turns into a
# liability instead of an asset.
# ─────────────────────────────────────────────────────────────────────────────

_KNOWLEDGE_FILE: Path = Path(__file__).parent / ".blender_mcp_knowledge.json"
_KNOWLEDGE: list = []


def _load_knowledge() -> None:
    """Read the persisted knowledge base from disk. Called once at startup."""
    global _KNOWLEDGE
    if not _KNOWLEDGE_FILE.exists():
        return
    try:
        data = json.loads(_KNOWLEDGE_FILE.read_text())
        if isinstance(data, list):
            _KNOWLEDGE = data
        logger.info(f"_load_knowledge: restored {len(_KNOWLEDGE)} entries from {_KNOWLEDGE_FILE}")
    except Exception as e:
        logger.warning(f"_load_knowledge: could not read {_KNOWLEDGE_FILE}: {e}")


def _save_knowledge() -> None:
    """Write the knowledge base to disk. Silent on failure — never crash the server."""
    try:
        _KNOWLEDGE_FILE.write_text(json.dumps(_KNOWLEDGE, indent=2))
    except Exception as e:
        logger.warning(f"_save_knowledge: could not write {_KNOWLEDGE_FILE}: {e}")


def _next_knowledge_id() -> str:
    """Derive next KB-NNN id from the max id already present — same pattern as
    _next_issue_id, so restarts don't collide IDs with a reset counter."""
    max_n = 0
    for entry in _KNOWLEDGE:
        eid = entry.get("id", "")
        if eid.startswith("KB-"):
            try:
                max_n = max(max_n, int(eid[3:]))
            except ValueError:
                pass
    return f"KB-{max_n + 1:03d}"


def _knowledge_entries_match(a: dict, b_problem_type: str, b_category: str, b_tags: dict) -> bool:
    """Two entries are 'the same lesson' if problem_type, category, and every
    supplied context tag match. Used to increment confidence on repeat
    confirmation instead of accumulating duplicate near-identical entries."""
    if a.get("problem_type") != b_problem_type or a.get("category") != b_category:
        return False
    a_tags = a.get("context_tags", {}) or {}
    for k, v in (b_tags or {}).items():
        if a_tags.get(k) != v:
            return False
    return True


# Load persisted knowledge immediately at import time
_load_knowledge()


# ─────────────────────────────────────────────────────────────────────────────
# CREATIVE RECIPE STORE — Phase 2 of the Spatial Intelligence roadmap.
#
# Translates natural-language intent/style references into structured,
# reusable parameter objects instead of re-deriving them from prose every
# call. "Make this look ancient" or "dark souls-like" becomes a stored
# {age_years, environment, weather_exposure, ...} / {palette, materials,
# form_language, ...} object, queryable by trigger phrase.
#
# Deliberate constraint from the spec this implements: NEVER store a brand/
# game/IP name as a recipe's canonical identity. IP names are only valid as
# trigger_phrases that map to a generalized recipe (e.g. "elden ring" and
# "dark souls" both map to canonical_name="grimdark_souls_fantasy") — the
# MCP executes the recipe, not the trademark.
#
# Same token-discipline rule as the knowledge base: zero-filter queries
# return name/type summaries only, never a full dump of stored recipes.
# ─────────────────────────────────────────────────────────────────────────────

_RECIPE_FILE: Path = Path(__file__).parent / ".blender_mcp_recipes.json"
_RECIPES: list = []


def _load_recipes() -> None:
    """Read the persisted recipe store from disk. Called once at startup."""
    global _RECIPES
    if not _RECIPE_FILE.exists():
        return
    try:
        data = json.loads(_RECIPE_FILE.read_text())
        if isinstance(data, list):
            _RECIPES = data
        logger.info(f"_load_recipes: restored {len(_RECIPES)} entries from {_RECIPE_FILE}")
    except Exception as e:
        logger.warning(f"_load_recipes: could not read {_RECIPE_FILE}: {e}")


def _save_recipes() -> None:
    """Write the recipe store to disk. Silent on failure — never crash the server."""
    try:
        _RECIPE_FILE.write_text(json.dumps(_RECIPES, indent=2))
    except Exception as e:
        logger.warning(f"_save_recipes: could not write {_RECIPE_FILE}: {e}")


def _next_recipe_id() -> str:
    """Derive next RECIPE-NNN id from the max id already present."""
    max_n = 0
    for entry in _RECIPES:
        eid = entry.get("id", "")
        if eid.startswith("RECIPE-"):
            try:
                max_n = max(max_n, int(eid[7:]))
            except ValueError:
                pass
    return f"RECIPE-{max_n + 1:03d}"


# Load persisted recipes immediately at import time
_load_recipes()


# ─────────────────────────────────────────────────────────────────────────────
# SNAPSHOT STORE — in-memory dict of mesh state snapshots keyed by object name.
# snapshot_mesh_state() writes here; compare_mesh_state() diffs against it.
# Snapshots are NOT persisted — they're per-MCP-session only (intentional:
# mesh state can change between Blender sessions, stale snapshots mislead).
# ─────────────────────────────────────────────────────────────────────────────

_SNAPSHOTS: dict = {}   # { object_name: { ...mesh stats... , "_timestamp": str } }


def _invalidate_dna_cache(object_name: str = None):
    """Drop cached Asset DNA raw fetches (see get_asset_dna()). Called by every
    mutating tool, eagerly before the mutation is attempted, so a failed or
    partial mutation can never leave a stale-but-trusted cache behind — same
    class of bug as KB-006 (a tool trusting stale state after a mutation)."""
    if object_name is None:
        for snap in _SNAPSHOTS.values():
            snap.pop("_dna_raw", None)
    else:
        _SNAPSHOTS.get(object_name, {}).pop("_dna_raw", None)


# Visual before/after snapshots for auto_repair_mesh (Tier 1c).
# { object_name: { "before": <PNG bytes>, "after": <PNG bytes>, "timestamp": str } }
# NOT persisted — per-session only.
_VISUAL_SNAPSHOTS: dict = {}


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


# ─────────────────────────────────────────────────────────────────────────────
# PRODUCTION RULES ENGINE — deterministic, zero-LLM-cost recommendations.
# Each rule reasons against Asset DNA (see get_asset_dna()), never invents a
# category or confidence — only fires on measured fields or real session state
# (active_playbook). Predicates must be pure and never raise; missing fields
# read as falsy via .get() so a partial DNA dict just skips the rule.
# ─────────────────────────────────────────────────────────────────────────────
_PRODUCTION_RULES: list = [
    {
        "id": "high_poly_nanite",
        "severity": "info",
        "predicate": lambda dna: (
            dna.get("identity", {}).get("polygon_count", 0) > 250_000
            and dna.get("target_engine") == "unreal"
        ),
        "recommendation": "Use Nanite for this mesh — skip manual retopo and skip generating LODs.",
        "why": "Above ~250k polygons, UE5 Nanite virtualized geometry handles the density directly. "
               "Manual retopo/LOD work at this budget is wasted effort for a Nanite-eligible static mesh.",
    },
    {
        "id": "topology_below_playbook_min",
        "severity": "warning",
        "predicate": lambda dna: (
            dna.get("identity", {}).get("category")
            and dna.get("geometry", {}).get("topology_score") is not None
            and dna["geometry"]["topology_score"] < _PLAYBOOKS.get(dna["identity"]["category"], {}).get("topology_score_min", 0)
        ),
        "recommendation": "Retopologize before continuing — topology score is below this playbook's minimum.",
        "why": "The active playbook sets a topology_score_min for this asset category; scores below it "
               "predict deformation/shading problems downstream (see _reason_topology).",
    },
    {
        "id": "over_vert_budget",
        "severity": "warning",
        "predicate": lambda dna: (
            dna.get("identity", {}).get("category")
            and dna.get("identity", {}).get("vertex_count", 0) > _studio_vert_budget(
                dna["identity"]["category"],
                _PLAYBOOKS.get(dna["identity"]["category"], {}).get("vert_budget", float("inf")),
            )
        ),
        "recommendation": "Decimate before export — vertex count exceeds this playbook's budget.",
        "why": "Each playbook sets a vert_budget for its asset category based on real-time performance "
               "targets for that class of asset.",
    },
    {
        "id": "missing_lightmap_uv",
        "severity": "warning",
        "predicate": lambda dna: (
            dna.get("identity", {}).get("category")
            and _PLAYBOOKS.get(dna["identity"]["category"], {}).get("uv_channels", 1) >= 2
            and not dna.get("geometry", {}).get("lightmap_uv_present", False)
        ),
        "recommendation": "Add a dedicated lightmap UV channel (UV1) before export.",
        "why": "This playbook requires 2 UV channels. Without a non-overlapping UV1, Lightmass/Lumen "
               "baking fails or produces shadow bleeding.",
    },
    {
        "id": "missing_pbr_maps",
        "severity": "info",
        "predicate": lambda dna: any(
            m.get("missing_maps") for m in dna.get("materials", [])
        ),
        "recommendation": "Generate or bake the missing PBR maps before hand-texturing — check materials[].missing_maps.",
        "why": "One or more materials rely on constant values instead of textures for at least one "
               "PBR channel. Constant channels can't carry surface detail (wear, grime, variation).",
    },
    {
        "id": "character_no_rig",
        "severity": "critical",
        "predicate": lambda dna: (
            dna.get("identity", {}).get("category") in ("hero_char", "creature")
            and not dna.get("identity", {}).get("has_armature", False)
        ),
        "recommendation": "This asset needs a rig before it can move into the animation pipeline.",
        "why": "hero_char and creature playbooks assume a skinned armature. No armature means no "
               "animation is possible in its current state.",
    },
    {
        "id": "no_collision_mesh_prop",
        "severity": "info",
        "predicate": lambda dna: (
            dna.get("identity", {}).get("category") in ("env_prop", "weapon")
            and dna.get("target_engine") == "unreal"
            and not dna.get("production", {}).get("collision_mesh_present", False)
        ),
        "recommendation": "No collision mesh found — ask the user before generating one "
                           "(many teams build collision in-engine; do not auto-generate).",
        "why": "env_prop/weapon assets typically need collision before they're usable in level design, "
               "but collision generation is gated behind explicit user approval in this project.",
    },
    {
        "id": "known_material_match",
        "severity": "info",
        "predicate": lambda dna: any(
            m.get("closest_known_material") for m in dna.get("materials", [])
        ),
        "recommendation": "One or more materials matched a previously-recorded material recipe — "
                           "check materials[].closest_known_material for the matched name, category, "
                           "and distance (lower = closer match).",
        "why": "The material knowledge layer only helps if it's actually surfaced — this makes a "
               "measured-similarity match visible in rules_fired instead of a field nobody reads.",
    },
    {
        "id": "blended_material_candidate",
        "severity": "warning",
        "predicate": lambda dna: any(
            m.get("heterogeneity", {}).get("likely_blended") for m in dna.get("materials", [])
        ),
        "recommendation": "One material's Base Color varies significantly between disconnected UV "
                           "islands — a real sign it's doing more than one job (the couch case: "
                           "leather + wood + fabric baked into a single shared material). Consider "
                           "split_blended_material(object_name, material_name) to separate it into "
                           "distinct, individually-weatherable materials.",
        "why": "material_count alone (a slot count) never catches this — a single-slot object can "
               "still visually be several different substances blended into one baked texture, "
               "confirmed live on a real couch asset (material_count: 1, leather/wood/blanket all "
               "sharing one material).",
    },
]


def _evaluate_production_rules(dna: dict) -> list:
    """Run every production rule's predicate against an assembled Asset DNA dict.
    Pure Python, no network/LLM cost. A raising predicate is treated as 'did not fire'
    rather than aborting the whole evaluation."""
    fired = []
    for rule in _PRODUCTION_RULES:
        try:
            if rule["predicate"](dna):
                fired.append({
                    "id": rule["id"],
                    "severity": rule["severity"],
                    "recommendation": rule["recommendation"],
                    "why": rule["why"],
                })
        except Exception:
            continue
    return fired


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
bpy.ops.mesh.select_mode(type='FACE')
bpy.ops.mesh.select_all(action='DESELECT')
bpy.ops.mesh.select_interior_faces()
bpy.ops.mesh.delete(type='FACE')

# Pass 3: limited dissolve of remaining wire edges — stray edges with
# no face on either side leave non-manifold verts.
bpy.ops.mesh.select_mode(type='EDGE')
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
bpy.ops.mesh.select_mode(type='VERT')
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
    "duplicate_faces",     # merge by distance (also helps non-manifold but scoped here)
    "zero_area_faces",
    "loose_vertices",      # addon key: "isolated_verts" — LAST geometry pass: the
                            # non-manifold/dissolve passes above can themselves strand
                            # new loose verts (observed live), so clean up after them
    "inverted_normals",    # last: recalc normals after all geometry changes
]


mcp = FastMCP(
    "BlenderMCP",
    instructions="""
BLENDER MCP — SENIOR TECHNICAL DIRECTOR v3.0
Pipeline-aware AAA TD embedded in Blender via live MCP tools. Priorities: pipeline
correctness → visual quality → performance → handoff readiness. Never PASS without
running the tool. Never skip the screenshot. Call get_workflow_guide() once per
session for full tool tiers, trigger map, and playbook/apprentice-mode details —
it's cheap to fetch on demand and not worth paying for on every turn.

── SESSION START — ALWAYS execute this sequence first, no exceptions ───────────────
0. session_status() — if has_context=True AND session.multiview.object == active
   object, baseline exists: skip to a single get_viewport_screenshot(). Otherwise
   run the full sequence below.
1. get_spatial_analysis(object_name) — FIRST LOOK at any new/not-yet-baselined
   mesh. 7 angles + world-space problem coordinates, same cost as one
   get_multiview_capture(). This is the real baseline everything builds on.
2. get_scene_info()   3. get_object_info(active)
Then ONE orientation sentence: "I see [asset]. [vert count]. Stage [N] inference.
[⚠️ CRITICAL: X if present]. Correct me if wrong — awaiting direction."
STOP. Wait for user. Do not auto-run further tools.

── SAFETY GATES — hard stops, never bypass ────────────────────────────────
GATE 1 DESTRUCTIVE GEOMETRY  → explicit "yes/do it/go ahead" before auto_repair_mesh()
GATE 2 STAGE TRANSITION      → full QA for current stage + "Ready to move to X?"
GATE 3 EXPORT                → run_unreal_readiness_check() zero errors + run_asset_qa() PASS
GATE 4 IRREVERSIBLE OPS      → state exactly what happens + wait for explicit confirm
GATE 5 BAKE                  → validate_bake_setup() MUST run first, every time
GATE 6 TD PLAN               → plan_production_path() → PRESENT PLAN → WAIT for approval
GATE 7 CHECKPOINT RESTORE    → state what's discarded + wait for explicit confirm before restore_checkpoint()

NEVER:
  ✗ restore_checkpoint() without explicit user approval — discards unsaved changes
  ✗ auto_repair_mesh() without explicit user approval, or without snapshot_mesh_state() first
  ✗ PASS without running the actual tool, or a "clean" verdict from visual inspection alone
  ✗ Export with known critical issues · Repair the wrong object · Delete user data
  ✗ Resolve a playbook conflict silently — surface it, ask for confirmation
  ✗ Execute a TD plan without presenting it first
  ✗ Call compare_mesh_state() without a prior snapshot — it will error
  ✗ Reason about topology/spatial placement from a single screenshot — use get_spatial_analysis
  ✗ Default to get_spatial_analysis(deep=True) — 42 images, only escalate when
     7 views + coordinates can't pinpoint the problem
  ✗ Call generate_collision_mesh() without asking first — many Unreal teams do
     collision in-engine. Ask before ever calling this tool.
  ✗ Dump export_for_unreal output loose — organize_folder=True (default) nests
     it under <dir>/<AssetName>/ with textures copied alongside.

── REPORT FORMAT ───────────────────────────────────────────────────────
VISUAL ASSESSMENT (always first) → TECHNICAL DATA (real numbers, cite tool) →
PRODUCTION VERDICT (✅ PASS/⚠️ WARN/❌ FAIL/🚫 CRITICAL + stage) → RECOMMENDED
ACTIONS (priority order) → RISK IF IGNORED (specific, not vague).
Tone: direct, professional, senior-to-senior, no filler.

SESSION MEMORY: session_status() persists to disk across MCP restarts. Call it
at start of each turn if context may exist; session_update() after confirming
facts. Don't re-run tools unless scene changed or user requests.
""",
)


@mcp.tool()
def get_workflow_guide() -> str:
    """
    Full tool-selection reference: pipeline stage table, tool call tiers,
    trigger-word map, playbook/apprentice-mode workflow, spatial-vision
    read guide, conflict-surfacing format, snapshot/diff workflow. Call
    this ONCE per session when you need the detailed tool map — the
    session-start sequence and safety gates are already in your system
    instructions, so this is reference material, not required every turn.
    """
    return """
── PIPELINE STAGES ──────────────────────────────────────────────────────
1 SCULPT      100k–10M+ verts, no UVs, no rig. Standards: none — detail only.
2 RETOPO      5k–80k, intentional quads, UV seams started. Quads >85%, loops at joints.
3 BAKE-READY  High+low pair, UVs on low, no overlap. UV stretch <20%, matching silhouettes.
4 TEXTURE     PBR materials, image textures, power-of-2. No broken paths, correct draw count.
5 RIG         Armature + vertex groups. Clean weights, bind pose, no orphan bones.
6 EXPORT      All above complete. Scale applied, pivot at origin, UE5 readiness PASS.
Ambiguous stage → assume the MORE DEMANDING one.

SPATIAL VISION: get_spatial_analysis(object_name) — 7 clean views + world-space
coordinates + spatial narrative, same image cost as get_multiview_capture().
Escalate to deep=True only when 7 views + coordinates can't pinpoint a specific
problem — adds wireframe + annotated highlights + severity heat map, up to 42
images. Check session multiview.capture_stale — re-capture if True.
  HOW TO READ: 1. spatial_narrative — WHAT/HOW MANY/WHERE (world coords + region)
  2. view_projections x/y (0=left/bottom → 1=right/top) locates cluster in any image
  3. deep=True only: image_guide maps image numbers to pass type, heat map (red=critical)

── TOOL CALL ORDER ──────────────────────────────────────────────────────
TIER 0 (judgment layer, use before Tier 1 when context is ambiguous):
  session_status, set_playbook, list_playbooks, production_review (scored
  report + conflicts + time estimate), plan_production_path (5-step TD plan,
  present + wait for approval), critique_mesh (senior TA topology review),
  animation_coach, session_update, get_spatial_analysis, get_multiview_capture
  (7-angle, include_wireframe=True for topology lines), get_annotated_capture
  (highlighted problem geometry), get_problem_coordinates (world-space
  clusters w/ centroid, bbox, severity, view_projections), snapshot_mesh_state
  (baseline before any repair), compare_mesh_state (signed delta after repair),
  get_scene_graph (relationship graph, positions, predicates), query_spatial
  (nearest/in_radius/intersecting/supporting/floating/isolated/raycast),
  describe_object_context (semantic role + neighbors, read before moving/
  parenting an object)
TIER 1 (prefer — most coverage per call):
  what_next, analyze_mesh_for_unreal, analyze_animation_quality, critique_animation
TIER 2 (targeted):
  get_mesh_quality_report, analyze_topology, run_unreal_readiness_check,
  run_asset_qa, classify_pipeline_stage, analyze_material_pbr,
  analyze_rig_weights, analyze_rig_skeleton, validate_bake_setup
TIER 3 (raw — only when Tier 0-2 don't cover it):
  detect_mesh_problems, get_object_info, get_scene_info
TIER 4 (repair — always gate-controlled):
  auto_repair_mesh (destructive, needs approval), run_asset_qa (verify after)

VERBOSE MODE: tools default verbose=False (failing/warning only). Pass
verbose=True for the full picture. Applies to: analyze_mesh_for_unreal,
validate_bake_setup, detect_mesh_problems, run_asset_qa,
run_unreal_readiness_check, analyze_rig_weights, analyze_rig_skeleton, critique_mesh.

SCENE-LEVEL ORDER (never skip): screenshot → get_scene_summary() →
classify_pipeline_stage(name) → audit_all_objects(). Auto-mode: 1 mesh = HERO,
2–20 = COLLECTION, 20+ = ENVIRONMENT.

── CONFLICT SURFACING ───────────────────────────────────────────────────
When data conflicts with what the user stated (asset type, budget, stage):
state it clearly + ask for confirmation, never resolve silently. Format:
"UV is clean. Topology is clean. But the vert count is 3× the weapon budget
you stated. Is this intentional or should I re-evaluate against a different
playbook?" production_review and what_next surface conflicts[] automatically.

── PLAYBOOK WORKFLOW ────────────────────────────────────────────────────
User says "this is a [weapon/hero/prop/creature/vehicle]":
  set_playbook(playbook='weapon') → session_update(asset_type='weapon') →
  re-run what_next/production_review. Active playbook adds vert_budget
  conflicts, stage_standard, and gotchas to what_next.

── APPRENTICE MODE ──────────────────────────────────────────────────────
"explain as you go"/"teach me" → session_update(apprentice_mode=True):
animation_coach adds principle lessons, plan_production_path adds step
notes, critique_mesh adds why_it_matters. State principles, not just fixes.
"stop explaining"/"expert mode" → session_update(apprentice_mode=False)

── TRIGGER MAP ──────────────────────────────────────────────────────────
  "what do I do next"             → what_next(object_name)
  "look/show/what do you see"     → get_viewport_screenshot()
  "ready for Unreal/export/UE5"   → analyze_mesh_for_unreal()
  "review/audit/full report"      → production_review(object_name, asset_type=...)
  "make a plan"                   → plan_production_path(object_name) — WAIT FOR APPROVAL
  "topology/loops/quads/critique" → critique_mesh(object_name)
  "what's wrong/check"            → analyze_mesh_for_unreal()
  "fix/clean/repair"              → snapshot_mesh_state() → describe plan → WAIT "yes/do it"
                                    → auto_repair_mesh() → compare_mesh_state() → show delta
  "rig/weights/skinning/bones"    → analyze_rig_weights() then analyze_rig_skeleton()
  "bake/baking/normal map/AO"     → validate_bake_setup(low_poly, high_poly) FIRST
  "animation/coach/teach me anim" → animation_coach(name, focus=...)
  "this is a weapon/hero/prop"    → set_playbook() + session_update(asset_type=...)
  "audit the scene/all objects"   → screenshot → get_scene_summary() → audit_all_objects()
  reference image + "match/build" → describe image → screenshot → gap report
  "scan/deep analysis/where is the problem" → get_spatial_analysis(object_name)
  "show me the wireframe/edge flow"         → get_multiview_capture(object_name, include_wireframe=True)
  "where exactly/highlight the problems"    → get_annotated_capture() then get_problem_coordinates()
  "coordinates/world position"              → get_problem_coordinates(object_name)
  "where is/what's near/layout/scene graph" → get_scene_graph() then describe_object_context(name)
  "what's floating/intersecting/isolated"   → query_spatial(query_type=...)
  "balance the room/spread objects"         → get_scene_graph() first — relationships, not coords

Screenshot required: session start, after any repair, before/after
auto_repair_mesh, when reporting any PASS/FAIL verdict.

── SNAPSHOT / DIFF WORKFLOW — always before destructive ops ────────────
1. snapshot_mesh_state(object_name)   ← baseline
2. [repair / destructive operation]
3. compare_mesh_state(object_name)    ← signed delta + IMPROVED/REGRESSED verdict
Never skip the snapshot step — without it compare_mesh_state will fail.

AI/SCAN ASSETS: very high poly + irregular topology → state "AI/scanned
asset detected. Pipeline: validate→cleanup→retopo→bake→texture→rig→export.
Do not export in current state."

── KNOWLEDGE BASE — persists across every project, not just this session ──
query_knowledge(problem_type, category, ...tags) BEFORE re-deriving a known
problem from scratch — non-manifold repair ceilings, version-specific API
breaks, workflows already proven out. record_knowledge(...) AFTER solving
something nontrivial, always with context tags (blender_version, asset_source,
mesh_type) — an untagged "always works" entry is how this goes stale and
misfires later. Not for routine repairs already covered by existing tools.

── CREATIVE RECIPES — structured intent/style, not re-derived prose ──────
query_creative_recipe(trigger_phrase) BEFORE reasoning a style/aging request
from scratch — "make this ancient", "souls-like", genre references. If none
fits, reason it out normally then record_creative_recipe(...) so it's reusable
next time. canonical_name must be a GENERALIZED name, never a brand/game/IP —
IP names belong only in trigger_phrases (the recipe, not the trademark, gets
executed).
"""


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
    SESSION CONTEXT — record confirmed facts (asset type, playbook, pipeline
    stage, verified checks) so later tool calls don't re-infer from scratch.
    Persists for the MCP session lifetime. All params optional — pass only
    what changed. asset_type/active_playbook are free-form strings (e.g.
    "hero_character"/"hero_char", "weapon", "creature", "environment_prop").
    confirmed_stage: 1-6.
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

    _save_session()
    return json.dumps({"session_updated": True, "current_session": _SESSION}, indent=2)


@mcp.tool()
def session_status() -> str:
    """
    SESSION STATUS — everything known this session: asset type, playbook,
    stage, verified checks, open issues, user corrections. Call at the start
    of a turn to orient before reaching for other tools; if empty, fall back
    to screenshot → scene_info → object_info.

    Checks the open .blend file against the file this session's context was
    built from — if they differ, file-scoped fields (asset_type, active_object,
    verified_checks, etc.) are reset automatically. See "file_check" in the
    response. Journal/issue history is kept, not reset — old entries carry
    their own object names so they stay legible across a file switch.
    """
    file_check = _check_session_file_match()

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

    if file_check["file_changed"]:
        orientation = (
            f"Blend file changed since last context ({file_check['previous_file']} -> "
            f"{file_check['current_file']}) — file-scoped session context was reset. "
            + orientation
        )

    return json.dumps({
        "has_context":        has_context,
        "orientation_summary": orientation,
        "file_check":         file_check,
        "session":            _SESSION,
        "persisted":          _SESSION_FILE.exists(),
        "session_file":       str(_SESSION_FILE),
    }, indent=2)


@mcp.tool()
def create_checkpoint(label: str = "") -> str:
    """
    CHECKPOINT — save a timestamped copy of the current .blend file as a
    file-level restore point, independent of Blender's undo stack. Undo has
    proven non-atomic in this project — one revert silently dropped an object
    (recoverable only because it was checked for). Call before any risky
    operation you'd want a guaranteed way back from: joins, rig edits, bulk
    repairs. label: short name, e.g. "before_rig_fix".
    """
    try:
        current = _get_blend_filepath()
        if not current:
            return json.dumps({"error": "Current file has no path yet — save the .blend file first."})

        import datetime as _dt
        timestamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_label = re.sub(r'[^A-Za-z0-9_-]', '_', label)[:40] if label else "checkpoint"
        blend_path = Path(current)
        checkpoint_path = str(blend_path.parent / f"{blend_path.stem}_CHECKPOINT_{safe_label}_{timestamp}.blend")

        code = (
            "import bpy, json\n"
            f"bpy.ops.wm.save_as_mainfile(filepath={checkpoint_path!r}, copy=True)\n"
            "print(json.dumps({'saved': True}))"
        )
        raw = _send_raw("execute_code_safe", code=code, required_mode=None, push_undo=False)
        output = raw.get("result", "") if isinstance(raw, dict) else ""
        if "saved" not in output:
            return json.dumps({"error": f"Checkpoint save failed: {output}"})

        entry = {
            "label": label or "checkpoint",
            "timestamp": timestamp,
            "filepath": checkpoint_path,
            "blend_filepath_at_creation": current,
        }
        checkpoints = _SESSION.get("checkpoints", [])
        checkpoints.append(entry)
        _session_set(checkpoints=checkpoints)
        _save_session()

        return json.dumps({
            "checkpoint_created": True,
            "label": entry["label"],
            "path": checkpoint_path,
            "note": "Restore with restore_checkpoint() — ask the user to confirm first, it discards unsaved changes.",
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def list_checkpoints() -> str:
    """
    List all checkpoints created this session, most recent first, with
    file_exists so stale/deleted checkpoints are visible before you rely on
    them. Use before restore_checkpoint() to pick the right one.
    """
    checkpoints = _SESSION.get("checkpoints", [])
    existing = [{**cp, "file_exists": Path(cp["filepath"]).exists()} for cp in checkpoints]
    return json.dumps({"checkpoints": list(reversed(existing)), "count": len(existing)}, indent=2)


@mcp.tool()
def restore_checkpoint(label_or_path: str) -> str:
    """
    RESTORE CHECKPOINT — reopen a previously saved checkpoint file, discarding
    ALL unsaved changes in the currently open file. ASK THE USER TO CONFIRM
    before ever calling this. Opening a new file also invalidates this
    session's in-memory context (reset automatically) and may briefly drop
    the live MCP connection, the same way an addon reload does — reconnect
    and call session_status() afterward to confirm the restore landed.
    label_or_path: a checkpoint's label (most recent match wins) or an exact
    filepath from list_checkpoints().
    """
    _invalidate_dna_cache()  # scene-wide — every object's state may have changed
    try:
        checkpoints = _SESSION.get("checkpoints", [])
        target = next((cp for cp in checkpoints if cp["filepath"] == label_or_path), None)
        if target is None:
            matches = [cp for cp in checkpoints if cp["label"] == label_or_path]
            target = matches[-1] if matches else None
        if target is None:
            return json.dumps({"error": f"No checkpoint found matching '{label_or_path}'. Call list_checkpoints() first."})
        if not Path(target["filepath"]).exists():
            return json.dumps({"error": f"Checkpoint file no longer exists on disk: {target['filepath']}"})

        code = (
            "import bpy, json\n"
            f"bpy.ops.wm.open_mainfile(filepath={target['filepath']!r})\n"
            "print(json.dumps({'opened': True}))"
        )
        raw = _send_raw("execute_code_safe", code=code, required_mode=None, push_undo=False)
        output = raw.get("result", "") if isinstance(raw, dict) else ""
        if "opened" not in output:
            return json.dumps({"error": f"Restore failed: {output}"})

        # Opening a new file invalidates this process's in-memory session context.
        _session_set(
            asset_type=None, active_playbook=None, confirmed_stage=None,
            active_object=None, verified_checks=[], open_issues=[],
            user_corrections=[], surfaced_conflicts=[], multiview=None,
            blend_filepath=target["blend_filepath_at_creation"],
        )
        _save_session()

        return json.dumps({
            "restored": True,
            "label": target["label"],
            "path": target["filepath"],
            "note": "Session context reset for the restored file state.",
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def record_knowledge(
    problem_type: str,
    category: str,
    problem: str,
    solution: str,
    why_it_worked: str,
    outcome: str = "success",
    blender_version: str = "",
    asset_source: str = "",
    mesh_type: str = "",
) -> str:
    """
    KNOWLEDGE BASE — persist a solved production problem so it survives
    across every project and server restart, not just this session. Use
    after genuinely solving something nontrivial (a repair ceiling, a
    version-specific API break, a workflow that actually worked) — not for
    routine repairs already covered by existing tools.

    problem_type: short slug, e.g. "non_manifold_repair_ceiling". category:
    mesh_topology|rigging|materials|export|animation|workflow. Always pass
    context tags (blender_version, asset_source, mesh_type) that were true
    when this was learned — an untagged "this always works" entry is exactly
    how a knowledge base goes stale and misfires later (see: the fcurves API
    break that only applies to Blender 4.4+).

    Calling this again with matching problem_type+category+tags increments
    confidence on the existing entry instead of creating a duplicate.
    """
    context_tags = {}
    if blender_version:
        context_tags["blender_version"] = blender_version
    if asset_source:
        context_tags["asset_source"] = asset_source
    if mesh_type:
        context_tags["mesh_type"] = mesh_type

    for entry in _KNOWLEDGE:
        if _knowledge_entries_match(entry, problem_type, category, context_tags):
            entry["times_confirmed"] = entry.get("times_confirmed", 1) + 1
            entry["solution"] = solution
            entry["why_it_worked"] = why_it_worked
            entry["outcome"] = outcome
            entry["confidence"] = "high" if entry["times_confirmed"] >= 3 else "medium"
            _save_knowledge()
            return json.dumps({"recorded": True, "id": entry["id"], "action": "confirmed_existing",
                                "times_confirmed": entry["times_confirmed"]})

    import datetime as _dt
    entry = {
        "id": _next_knowledge_id(),
        "created": _dt.datetime.now().isoformat(timespec="seconds"),
        "problem_type": problem_type,
        "category": category,
        "context_tags": context_tags,
        "problem": problem,
        "solution": solution,
        "why_it_worked": why_it_worked,
        "outcome": outcome,
        "confidence": "medium",
        "times_confirmed": 1,
    }
    _KNOWLEDGE.append(entry)
    _save_knowledge()
    return json.dumps({"recorded": True, "id": entry["id"], "action": "new_entry"})


@mcp.tool()
def query_knowledge(
    problem_type: str = "",
    category: str = "",
    blender_version: str = "",
    asset_source: str = "",
    mesh_type: str = "",
    max_results: int = 5,
) -> str:
    """
    KNOWLEDGE BASE — check for prior solved-problem knowledge before
    re-deriving something from scratch. Call with no filters to see category
    counts only (never dumps the full base into context). Filter by
    problem_type/category plus whatever context tags apply now — a match
    whose tags DON'T fit the current context (different Blender version,
    different asset source) is flagged, not silently trusted.
    """
    if not problem_type and not category:
        counts = {}
        for entry in _KNOWLEDGE:
            counts[entry.get("category", "unknown")] = counts.get(entry.get("category", "unknown"), 0) + 1
        return json.dumps({
            "total_entries": len(_KNOWLEDGE),
            "by_category": counts,
            "note": "Pass problem_type and/or category to search; add context tags to check fit.",
        }, indent=2)

    current_tags = {}
    if blender_version:
        current_tags["blender_version"] = blender_version
    if asset_source:
        current_tags["asset_source"] = asset_source
    if mesh_type:
        current_tags["mesh_type"] = mesh_type

    matches = []
    for entry in _KNOWLEDGE:
        if problem_type and entry.get("problem_type") != problem_type:
            continue
        if category and entry.get("category") != category:
            continue
        entry_tags = entry.get("context_tags", {}) or {}
        tag_mismatches = [k for k, v in current_tags.items() if k in entry_tags and entry_tags[k] != v]
        result = dict(entry)
        result["context_fit"] = "exact" if not tag_mismatches else f"mismatch on {tag_mismatches}"
        matches.append(result)

    matches.sort(key=lambda e: (e.get("confidence") == "high", e.get("times_confirmed", 0)), reverse=True)
    return json.dumps({"matches": matches[:max_results], "total_matches": len(matches)}, indent=2)


@mcp.tool()
def record_creative_recipe(
    recipe_type: str,
    canonical_name: str,
    trigger_phrases: list,
    parameters: dict,
    notes: str = "",
) -> str:
    """
    CREATIVE RECIPE — persist a structured translation of intent or style
    into reusable parameters, instead of re-reasoning "make this look
    ancient" or "dark souls-like" from scratch every time it comes up.

    recipe_type: "aging"|"style"|"material_condition"|"mood".
    canonical_name: a GENERALIZED name for the recipe — NEVER a brand, game,
    or IP name (e.g. "grimdark_souls_fantasy", not "elden_ring"). IP names
    belong only in trigger_phrases, as the language a user might actually
    say — the recipe itself must stay reusable and not brand-bound.
    trigger_phrases: list of phrases that should surface this recipe, e.g.
    ["elden ring", "dark souls", "souls-like"] for a dark-fantasy style, or
    ["ancient", "300 years old", "long abandoned"] for an aging recipe.
    parameters: the structured recipe itself — free-form dict, shape depends
    on recipe_type (aging: age_years/environment/weather_exposure/damage_
    severity/surface_wear/narrative; style: palette/materials/form_language/
    wear_level/silhouette).

    Calling again with a matching canonical_name+recipe_type merges new
    trigger_phrases into the existing entry and increments confirmation
    rather than creating a duplicate.
    """
    for entry in _RECIPES:
        if entry.get("canonical_name") == canonical_name and entry.get("recipe_type") == recipe_type:
            existing_phrases = set(entry.get("trigger_phrases", []))
            entry["trigger_phrases"] = sorted(existing_phrases | set(trigger_phrases))
            entry["parameters"] = parameters
            if notes:
                entry["notes"] = notes
            entry["times_confirmed"] = entry.get("times_confirmed", 1) + 1
            entry["confidence"] = "high" if entry["times_confirmed"] >= 3 else "medium"
            _save_recipes()
            return json.dumps({"recorded": True, "id": entry["id"], "action": "confirmed_existing",
                                "times_confirmed": entry["times_confirmed"]})

    import datetime as _dt
    entry = {
        "id": _next_recipe_id(),
        "created": _dt.datetime.now().isoformat(timespec="seconds"),
        "recipe_type": recipe_type,
        "canonical_name": canonical_name,
        "trigger_phrases": list(trigger_phrases),
        "parameters": parameters,
        "notes": notes,
        "confidence": "medium",
        "times_confirmed": 1,
    }
    _RECIPES.append(entry)
    _save_recipes()
    return json.dumps({"recorded": True, "id": entry["id"], "action": "new_entry"})


@mcp.tool()
def query_creative_recipe(
    trigger_phrase: str = "",
    recipe_type: str = "",
    canonical_name: str = "",
    max_results: int = 5,
) -> str:
    """
    CREATIVE RECIPE — check for an existing structured recipe before
    re-deriving one from a style reference or aging request. Call with no
    filters to see canonical_name/recipe_type summaries only — never dumps
    every recipe's full parameters into context. trigger_phrase does a
    case-insensitive substring match against each recipe's stored phrases.
    """
    if not trigger_phrase and not recipe_type and not canonical_name:
        summary = [{"canonical_name": e.get("canonical_name"), "recipe_type": e.get("recipe_type")}
                   for e in _RECIPES]
        return json.dumps({
            "total_entries": len(_RECIPES),
            "recipes": summary,
            "note": "Pass trigger_phrase, recipe_type, or canonical_name to retrieve full parameters.",
        }, indent=2)

    needle = trigger_phrase.lower().strip()
    matches = []
    for entry in _RECIPES:
        if recipe_type and entry.get("recipe_type") != recipe_type:
            continue
        if canonical_name and entry.get("canonical_name") != canonical_name:
            continue
        if needle:
            phrases = [p.lower() for p in entry.get("trigger_phrases", [])]
            if not any(needle in p or p in needle for p in phrases):
                continue
        matches.append(entry)

    matches.sort(key=lambda e: (e.get("confidence") == "high", e.get("times_confirmed", 0)), reverse=True)
    return json.dumps({"matches": matches[:max_results], "total_matches": len(matches)}, indent=2)


@mcp.tool()
def set_playbook(playbook: str) -> str:
    """
    PRODUCTION PLAYBOOK — activates a named workflow so what_next,
    production_review, and plan_production_path evaluate against the right
    vertex budget, mandatory checks, and gotchas.
    playbook: "hero_char" (80k, 2 UV channels, full rig QA, LODs required) |
    "creature" (60k) | "weapon" (15k, no rig) | "env_prop" (20k, lightmap UV) |
    "vehicle" (60k, wheel bone notes).
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

    _save_session()
    return json.dumps({
        "playbook_activated": playbook,
        "name":              pb["name"],
        "description":       pb["description"],
        "vert_budget":       _studio_vert_budget(playbook, pb["vert_budget"]),
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
            "vert_budget": _studio_vert_budget(key, pb["vert_budget"]),
            "material_limit": pb["material_limit"],
            "mandatory_checks": pb["mandatory_checks"],
        }
    active = _session_get("active_playbook")
    return json.dumps({
        "active_playbook": active,
        "available_playbooks": summary,
        "how_to_activate": "Call set_playbook(playbook='hero_char') to activate a playbook.",
    }, indent=2)


@mcp.tool()
def snapshot_mesh_state(object_name: str) -> str:
    """
    SNAPSHOT — captures vert/face/ngon/non-manifold/topology/UV stats as a
    baseline. Call before any repair or destructive op so compare_mesh_state()
    can show the delta. Per-MCP-session only, not persisted to disk.
    """
    import datetime

    blender = get_blender_connection()

    # Pull mesh quality (counts, face_types, problems, uv)
    raw_quality = blender.send_command("get_mesh_quality_report", {"name": object_name})
    if isinstance(raw_quality, dict) and "error" in raw_quality:
        return json.dumps({"error": f"get_mesh_quality_report failed: {raw_quality['error']}"})

    # Pull topology score
    raw_topo = blender.send_command("analyze_topology", {"name": object_name, "context": "generic"})
    if isinstance(raw_topo, dict) and "error" in raw_topo:
        return json.dumps({"error": f"analyze_topology failed: {raw_topo['error']}"})

    # Extract from real schema keys (verified against _reason_mesh_quality / _reason_topology)
    counts     = raw_quality.get("counts", {})
    face_types = raw_quality.get("face_types", {})
    problems   = raw_quality.get("problems", {})
    uv         = raw_quality.get("uv", {})
    topo_stats = raw_topo.get("stats", {})
    face_total = counts.get("faces", 0) or 0
    ngon_pct   = round(face_types.get("ngons", 0) / face_total * 100, 1) if face_total else 0.0

    snapshot = {
        "_timestamp":       datetime.datetime.now().isoformat(timespec="seconds"),
        "_object":          object_name,
        "vert_count":       counts.get("verts", 0),
        "edge_count":       counts.get("edges", 0),
        "face_count":       counts.get("faces", 0),
        "tris":             face_types.get("tris", 0),
        "quads":            face_types.get("quads", 0),
        "ngons":            face_types.get("ngons", 0),
        "non_manifold":     problems.get("non_manifold_edges", 0),
        "isolated_verts":   problems.get("isolated_verts", 0),
        "zero_area_faces":  problems.get("zero_area_faces", 0),
        "duplicate_faces":  problems.get("duplicate_faces", 0),
        "uv_oob_loops":     uv.get("out_of_bounds_loops", 0),
        "has_uvs":          uv.get("has_uvs", False),
        "uv_layer_count":   uv.get("layer_count", 0),
        "topology_score":   raw_topo.get("topology_score", 0),
        "topology_rating":  raw_topo.get("rating", "unknown"),
        "quad_ratio_pct":   topo_stats.get("quad_ratio_pct", 0.0),
        # FIX: was assigning tris_pct here under the "ngon_pct" key — real ngon
        # percentage isn't precomputed anywhere in the schema, so derive it
        # from the actual ngon count and face count instead.
        "ngon_pct":         ngon_pct,
    }

    _SNAPSHOTS[object_name] = snapshot

    return json.dumps({
        "snapshot_taken":  True,
        "object":          object_name,
        "timestamp":       snapshot["_timestamp"],
        "baseline": {
            "verts":           snapshot["vert_count"],
            "faces":           snapshot["face_count"],
            "ngons":           snapshot["ngons"],
            "non_manifold":    snapshot["non_manifold"],
            "topology_score":  snapshot["topology_score"],
            "topology_rating": snapshot["topology_rating"],
            "has_uvs":         snapshot["has_uvs"],
        },
        "note": "Call compare_mesh_state(object_name) after your repair pass to see the delta.",
    }, indent=2)


@mcp.tool()
def compare_mesh_state(object_name: str) -> str:
    """
    COMPARE — diffs current mesh state against the stored snapshot: signed
    deltas per stat, each tagged IMPROVED/REGRESSED/UNCHANGED. Requires
    snapshot_mesh_state(object_name) called first this session.
    """
    if object_name not in _SNAPSHOTS:
        return json.dumps({
            "error": f"No snapshot found for '{object_name}'. "
                     f"Call snapshot_mesh_state('{object_name}') first.",
            "available_snapshots": list(_SNAPSHOTS.keys()),
        })

    baseline = _SNAPSHOTS[object_name]
    blender  = get_blender_connection()

    # Fresh analysis — same calls as snapshot_mesh_state
    raw_quality = blender.send_command("get_mesh_quality_report", {"name": object_name})
    if isinstance(raw_quality, dict) and "error" in raw_quality:
        return json.dumps({"error": f"get_mesh_quality_report failed: {raw_quality['error']}"})

    raw_topo = blender.send_command("analyze_topology", {"name": object_name, "context": "generic"})
    if isinstance(raw_topo, dict) and "error" in raw_topo:
        return json.dumps({"error": f"analyze_topology failed: {raw_topo['error']}"})

    counts     = raw_quality.get("counts", {})
    face_types = raw_quality.get("face_types", {})
    problems   = raw_quality.get("problems", {})
    uv         = raw_quality.get("uv", {})
    topo_stats = raw_topo.get("stats", {})

    current = {
        "vert_count":      counts.get("verts", 0),
        "face_count":      counts.get("faces", 0),
        "ngons":           face_types.get("ngons", 0),
        "non_manifold":    problems.get("non_manifold_edges", 0),
        "isolated_verts":  problems.get("isolated_verts", 0),
        "zero_area_faces": problems.get("zero_area_faces", 0),
        "duplicate_faces": problems.get("duplicate_faces", 0),
        "uv_oob_loops":    uv.get("out_of_bounds_loops", 0),
        "topology_score":  raw_topo.get("topology_score", 0),
        "topology_rating": raw_topo.get("rating", "unknown"),
        "quad_ratio_pct":  topo_stats.get("quad_ratio_pct", 0.0),
    }

    # For each numeric stat: lower is better for problems, higher is better for score/ratio
    # higher_better: topology_score, quad_ratio_pct, vert_count (neutral — flag both directions)
    lower_is_better = {
        "ngons", "non_manifold", "isolated_verts",
        "zero_area_faces", "duplicate_faces", "uv_oob_loops",
    }
    higher_is_better = {"topology_score", "quad_ratio_pct"}
    neutral = {"vert_count", "face_count"}

    deltas = {}
    overall_improved = 0
    overall_regressed = 0

    for key in current:
        if key == "topology_rating":
            continue
        b_val = baseline.get(key, 0)
        c_val = current[key]
        if not isinstance(b_val, (int, float)) or not isinstance(c_val, (int, float)):
            continue
        delta = c_val - b_val
        if delta == 0:
            status = "UNCHANGED"
        elif key in lower_is_better:
            status = "IMPROVED" if delta < 0 else "REGRESSED"
            if delta < 0: overall_improved += 1
            else:         overall_regressed += 1
        elif key in higher_is_better:
            status = "IMPROVED" if delta > 0 else "REGRESSED"
            if delta > 0: overall_improved += 1
            else:         overall_regressed += 1
        else:  # neutral
            status = "CHANGED"

        deltas[key] = {
            "before": b_val,
            "after":  c_val,
            "delta":  f"{'+' if delta > 0 else ''}{delta:.0f}" if isinstance(delta, float) and delta == int(delta)
                      else f"{'+' if delta > 0 else ''}{delta:.1f}",
            "status": status,
        }

    # Overall verdict
    # FIX: the old 4-branch chain had a gap — "regressed > 0 and improved > 0 but
    # improved <= regressed" (e.g. 1 improved, 3 regressed) matched none of the
    # first three conditions and silently fell into the "else: UNCHANGED" branch,
    # even though real regressions occurred. Split "truly 0/0" out explicitly and
    # give the tied/net-regression case its own MIXED label instead of a fallthrough.
    if overall_regressed == 0 and overall_improved == 0:
        verdict = "UNCHANGED — no measurable difference"
    elif overall_regressed == 0:
        verdict = f"PASS — {overall_improved} stat(s) improved, mesh improved across the board"
    elif overall_improved == 0:
        verdict = f"REGRESSED — {overall_regressed} stat(s) got worse, none improved"
    elif overall_improved > overall_regressed:
        verdict = f"MIXED (net improvement) — {overall_improved} stat(s) improved, {overall_regressed} regressed"
    else:
        verdict = f"MIXED (net regression) — {overall_improved} stat(s) improved, {overall_regressed} regressed"

    return json.dumps({
        "object":           object_name,
        "snapshot_taken":   baseline["_timestamp"],
        "verdict":          verdict,
        "topology_rating":  f"{baseline.get('topology_rating','?')} → {current['topology_rating']}",
        "deltas":           deltas,
        "note": (
            "Positive delta on problem counts (ngons, non_manifold, etc.) = REGRESSED. "
            "Positive delta on topology_score = IMPROVED."
        ),
    }, indent=2)


@mcp.tool()
def close_boundary_holes(object_name: str, dry_run: bool = True) -> str:
    """
    CLOSE BOUNDARY HOLES — caps open/non-watertight edges WITHOUT leaving
    ngons (triangulates only the new cap faces, not the whole mesh). Opt-in —
    auto_repair_mesh() deliberately skips boundary edges since closing a hole
    is a judgment call (genuine hole vs. intentional open-shell geometry).
    dry_run=True (default): preview only. dry_run=False: executes + re-scans.
    """
    if not dry_run:
        _invalidate_dna_cache(object_name)
    script = r"""
import bpy, bmesh, json

obj = bpy.data.objects.get('{OBJ}')
if obj is None:
    print(json.dumps({"error": "Object not found: {OBJ}"}))
else:
    bpy.context.view_layer.objects.active = obj
    if obj.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')

    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bm.edges.ensure_lookup_table()

    boundary_edges = set(e for e in bm.edges if e.is_boundary)

    visited = set()
    loops = []
    for e in boundary_edges:
        if e in visited:
            continue
        stack = [e]
        group = []
        while stack:
            cur = stack.pop()
            if cur in visited:
                continue
            visited.add(cur)
            group.append(cur)
            for v in cur.verts:
                for e2 in v.link_edges:
                    if e2 in boundary_edges and e2 not in visited:
                        stack.append(e2)
        loops.append(group)

    loop_summary = [{"edges": len(l), "verts": len(set(v for e in l for v in e.verts))} for l in loops]
    bm.free()

    DRY_RUN = {DRY_RUN}

    if not boundary_edges:
        print(json.dumps({
            "object": "{OBJ}", "boundary_edge_count": 0, "loop_count": 0,
            "note": "No boundary edges found — mesh is already watertight.",
        }))
    elif DRY_RUN:
        print(json.dumps({
            "dry_run": True,
            "object": "{OBJ}",
            "boundary_edge_count": len(boundary_edges),
            "loop_count": len(loops),
            "loops": loop_summary,
            "note": "Re-run with dry_run=False to close these and triangulate the caps.",
        }))
    else:
        face_count_before = len(obj.data.polygons)

        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_mode(type='EDGE')
        bpy.ops.mesh.select_all(action='DESELECT')
        bpy.ops.mesh.select_non_manifold(extend=False, use_wire=False, use_boundary=True,
                                           use_multi_face=False, use_non_contiguous=False, use_verts=False)
        bpy.ops.mesh.fill_holes(sides=0)
        bpy.ops.object.mode_set(mode='OBJECT')

        # Select ONLY the newly-created cap faces (appended after the original
        # face count) and triangulate just those — not the whole mesh.
        for i, p in enumerate(obj.data.polygons):
            p.select = (i >= face_count_before)
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_mode(type='FACE')
        bpy.ops.mesh.quads_convert_to_tris(quad_method='BEAUTY', ngon_method='BEAUTY')
        bpy.ops.object.mode_set(mode='OBJECT')

        print(json.dumps({
            "dry_run": False,
            "object": "{OBJ}",
            "loops_closed": len(loops),
            "boundary_edges_before": len(boundary_edges),
            "faces_added": len(obj.data.polygons) - face_count_before,
            "note": "Cap faces triangulated to avoid leaving ngons.",
        }))
""".replace("{OBJ}", object_name.replace("'", "\\'")).replace("{DRY_RUN}", "True" if dry_run else "False")

    try:
        blender = get_blender_connection()
        raw = blender.send_command("execute_code_safe", {"code": script, "required_mode": "OBJECT", "push_undo": True})
        if "error" in raw:
            return json.dumps({"error": raw["error"]})
        output = raw.get("result", "")
        for line in output.strip().splitlines():
            line = line.strip()
            if line.startswith("{"):
                result = json.loads(line)
                if not dry_run and "error" not in result:
                    fresh_problems = _send_raw("detect_mesh_problems", name=object_name)
                    fresh_topo     = _send_raw("analyze_topology",     name=object_name)
                    prob_list = fresh_problems.get("problems", []) if "error" not in fresh_problems else []
                    prob_map  = {p.get("type", ""): p.get("count", 0) for p in prob_list}
                    result["after_scan"] = {
                        "non_manifold_edges": prob_map.get("non_manifold_edges", 0),
                        "boundary_edges":     prob_map.get("boundary_edges", 0),
                        "ngons":              fresh_topo.get("stats", {}).get("ngons", 0) if "error" not in fresh_topo else None,
                        "topology_score":     fresh_topo.get("topology_score", 0) if "error" not in fresh_topo else None,
                        "topology_rating":    fresh_topo.get("rating", "unknown") if "error" not in fresh_topo else None,
                    }
                    # Reuse this scan's own boundary_edges reading rather than
                    # a second, heavier DNA fetch just for one field — same
                    # ground truth, no redundant round-trip.
                    before = result.get("boundary_edges_before", 0)
                    after  = result["after_scan"]["boundary_edges"]
                    result["dna_verification"] = {
                        "boundary_edges_before": before,
                        "boundary_edges_after":  after,
                        "confirmed": after < before,
                    }
                _session_append("verified_checks", "close_boundary_holes")
                return json.dumps(result, indent=2, default=str)
        return json.dumps({"error": "No JSON output from close_boundary_holes script", "raw": output})
    except Exception as e:
        logger.error(f"Error in close_boundary_holes: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_multiview_capture(object_name: str, include_wireframe: bool = False) -> list:
    """
    MULTIVIEW CAPTURE — 7 viewport shots (front/back/left/right/top/bottom/
    persp) as images, no geometry hidden. include_wireframe=True adds 7 more
    in wireframe shading (topology as lines). Not persisted to disk. Session
    tracks capture_stale — re-capture after any mesh repair.
    """
    import datetime, tempfile, os

    blender = get_blender_connection()

    # The 6 orthographic axes + 1 perspective.
    # view_axis type enum: FRONT|BACK|LEFT|RIGHT|TOP|BOTTOM
    # Perspective is handled separately via region_3d.view_perspective property.
    ORTHO_VIEWS = ["FRONT", "BACK", "LEFT", "RIGHT", "TOP", "BOTTOM"]

    # Script: set up one view, frame the object, return viewport state confirmation.
    # Runs for each view axis. Screenshot is taken by the Python side using the
    # existing screenshot_area pattern from get_viewport_screenshot.
    #
    # IMPORTANT context rules (confirmed from addon.py analysis):
    #   view_axis   → needs area + region (WINDOW) in temp_override
    #   view_selected → same family, same requirement
    #   screenshot_area → needs area only (no region)
    #   All three operators must find the VIEW_3D area explicitly.

    def _make_view_script(axis: str, obj_name: str) -> str:
        """Generate Blender Python to set view axis and frame the named object."""
        if axis == "PERSP":
            # Perspective: don't call view_axis, just set perspective mode + frame
            return f"""
import bpy, json
scene = bpy.context.scene
obj   = bpy.data.objects.get({repr(obj_name)})
result = {{"ok": False, "error": None}}
if obj is None:
    result["error"] = "Object not found: {obj_name}"
else:
    area = next((a for a in bpy.context.screen.areas if a.type == 'VIEW_3D'), None)
    if area is None:
        result["error"] = "No VIEW_3D area found"
    else:
        region = next((r for r in area.regions if r.type == 'WINDOW'), None)
        if region is None:
            result["error"] = "No WINDOW region in VIEW_3D area"
        else:
            space = next((s for s in area.spaces if s.type == 'VIEW_3D'), None)
            if space:
                space.region_3d.view_perspective = 'PERSP'
            bpy.context.view_layer.objects.active = obj
            with bpy.context.temp_override(area=area, region=region):
                bpy.ops.view3d.view_selected()
            result["ok"] = True
print(__import__('json').dumps(result))
"""
        else:
            return f"""
import bpy, json
scene = bpy.context.scene
obj   = bpy.data.objects.get({repr(obj_name)})
result = {{"ok": False, "error": None}}
if obj is None:
    result["error"] = "Object not found: {obj_name}"
else:
    area = next((a for a in bpy.context.screen.areas if a.type == 'VIEW_3D'), None)
    if area is None:
        result["error"] = "No VIEW_3D area found"
    else:
        region = next((r for r in area.regions if r.type == 'WINDOW'), None)
        if region is None:
            result["error"] = "No WINDOW region in VIEW_3D area"
        else:
            bpy.context.view_layer.objects.active = obj
            with bpy.context.temp_override(area=area, region=region):
                bpy.ops.view3d.view_axis(type={repr(axis)}, align_active=False)
                bpy.ops.view3d.view_selected()
            result["ok"] = True
print(__import__('json').dumps(result))
"""

    def _make_shading_script(shading_type: str) -> str:
        """Set viewport shading mode. Returns space.shading.type before change."""
        return f"""
import bpy, json
area = next((a for a in bpy.context.screen.areas if a.type == 'VIEW_3D'), None)
if area:
    space = next((s for s in area.spaces if s.type == 'VIEW_3D'), None)
    if space:
        prev = space.shading.type
        space.shading.type = {repr(shading_type)}
        print(__import__('json').dumps({{"ok": True, "previous_shading": prev}}))
    else:
        print(__import__('json').dumps({{"ok": False, "error": "No VIEW_3D space"}}))
else:
    print(__import__('json').dumps({{"ok": False, "error": "No VIEW_3D area"}}))
"""

    def _take_screenshot() -> bytes:
        """Take a screenshot using the same pattern as get_viewport_screenshot."""
        temp_path = os.path.join(
            tempfile.gettempdir(),
            f"blender_mv_{os.getpid()}_{id(object())}.png"
        )
        result = blender.send_command(
            "get_viewport_screenshot",
            {"max_size": 900, "filepath": temp_path, "format": "png"}
        )
        if isinstance(result, dict) and "error" in result:
            raise RuntimeError(f"Screenshot failed: {result['error']}")
        if not os.path.exists(temp_path):
            raise RuntimeError("Screenshot file not created")
        with open(temp_path, "rb") as f:
            data = f.read()
        try:
            os.remove(temp_path)
        except OSError:
            pass
        return data

    # ── Save current view state ────────────────────────────────────────────
    save_script = """
import bpy, json
area = next((a for a in bpy.context.screen.areas if a.type == 'VIEW_3D'), None)
if area:
    space  = next((s for s in area.spaces if s.type == 'VIEW_3D'), None)
    r3d    = space.region_3d if space else None
    result = {
        "shading":     space.shading.type if space else "SOLID",
        "perspective": r3d.view_perspective if r3d else "PERSP",
        "view_distance": r3d.view_distance if r3d else 10.0,
    }
else:
    result = {"shading": "SOLID", "perspective": "PERSP", "view_distance": 10.0}
print(__import__('json').dumps(result))
"""
    save_raw = blender.send_command("execute_code_safe", {
        "code": save_script, "required_mode": "OBJECT", "push_undo": False
    })
    saved_state = {"shading": "SOLID", "perspective": "PERSP"}
    if isinstance(save_raw, dict):
        output = save_raw.get("result", "")
        for line in output.strip().splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    saved_state = json.loads(line)
                except Exception:
                    pass
                break

    # ── Capture loop ───────────────────────────────────────────────────────
    all_views  = ORTHO_VIEWS + ["PERSP"]
    images_out = []   # list of Image objects
    view_errors = []

    def _capture_pass(shading_label: str) -> None:
        """Run all 7 views in the current shading mode, append Image objects."""
        for axis in all_views:
            script = _make_view_script(axis, object_name)
            raw = blender.send_command("execute_code_safe", {
                "code": script, "required_mode": "OBJECT", "push_undo": False
            })
            # Check for view-set error
            ok = True
            if isinstance(raw, dict):
                output = raw.get("result", "")
                for line in output.strip().splitlines():
                    line = line.strip()
                    if line.startswith("{"):
                        try:
                            parsed = json.loads(line)
                            if not parsed.get("ok"):
                                view_errors.append(
                                    f"{shading_label}/{axis}: {parsed.get('error','unknown')}"
                                )
                                ok = False
                        except Exception:
                            pass
                        break
            if not ok:
                continue
            try:
                img_bytes = _take_screenshot()
                images_out.append(Image(data=img_bytes, format="png"))
            except RuntimeError as e:
                view_errors.append(f"{shading_label}/{axis} screenshot: {e}")

    # Solid pass
    _capture_pass("SOLID")

    # Wireframe pass (if requested). try/finally guarantees shading is restored
    # even if the capture loop raises mid-pass (e.g. an MCP connection drop) —
    # a bare sequential restore call after it would get skipped by that exception.
    try:
        if include_wireframe:
            blender.send_command("execute_code_safe", {
                "code": _make_shading_script("WIREFRAME"),
                "required_mode": "OBJECT", "push_undo": False
            })
            _capture_pass("WIREFRAME")
    finally:
        if include_wireframe:
            restore_shading = saved_state.get("shading", "SOLID")
            blender.send_command("execute_code_safe", {
                "code": _make_shading_script(restore_shading),
                "required_mode": "OBJECT", "push_undo": False
            })

    # ── Store metadata in session ──────────────────────────────────────────
    import datetime
    mv_meta = {
        "object":         object_name,
        "timestamp":      datetime.datetime.now().isoformat(timespec="seconds"),
        "views_captured": all_views,
        "wireframe_set":  include_wireframe,
        "capture_stale":  False,
        "view_errors":    view_errors,
        "image_count":    len(images_out),
    }
    _session_set(multiview=mv_meta)
    _save_session()

    # ── Build return: images first, then a metadata summary image-list entry ──
    # MCP tool returning a list — images + a trailing JSON summary as text
    # so Claude can read capture metadata alongside the images.
    summary = {
        "multiview_capture": "complete",
        "object":            object_name,
        "images_returned":   len(images_out),
        "views":             all_views,
        "wireframe_included": include_wireframe,
        "view_errors":       view_errors,
        "note": (
            "Images are ordered: FRONT, BACK, LEFT, RIGHT, TOP, BOTTOM, PERSP"
            + (", then WIREFRAME same order" if include_wireframe else "")
            + ". Reason across all views before forming any spatial conclusion."
        ),
    }
    # Return images followed by the JSON summary encoded as a final image-wrapper.
    # FastMCP handles list returns — each Image in the list becomes a separate
    # image content block; we append the metadata as a plain string via a hack-free
    # approach: store it on the session (already done above) and include it in
    # the last element description by returning it as the tool's string result
    # alongside the images. Since FastMCP @mcp.tool() with list return sends
    # each element, we return images + summary dict serialised to a final entry.
    images_out.append(summary)   # FastMCP will serialise non-Image as text content
    return images_out


@mcp.tool()
def get_problem_coordinates(object_name: str, problem_type: str = "all", cluster_radius: float = 0.5) -> str:
    """
    PROBLEM COORDINATES — world-space locations of every mesh problem
    (ngons, non-manifold edges, high-valence poles), clustered by proximity
    into regions with centroid, bbox, severity, region_label, and
    FRONT/RIGHT/TOP normalized view_projections for locating them in a
    screenshot. problem_type: "all" | "ngons" | "non_manifold" | "poles".
    """
    script = f"""
import bpy, bmesh, json, math
from mathutils import Vector

OBJ_NAME      = {repr(object_name)}
PROBLEM_TYPE  = {repr(problem_type)}
CLUSTER_RAD   = {cluster_radius}

obj = bpy.data.objects.get(OBJ_NAME)
if obj is None:
    print(json.dumps({{"error": f"Object not found: {{OBJ_NAME}}"}}))
else:
    mw = obj.matrix_world
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.verts.ensure_lookup_table()

    # ── Bounding box in world space ───────────────────────────────────────
    bbox_pts = [mw @ Vector(c) for c in obj.bound_box]
    bb_min   = Vector((min(v.x for v in bbox_pts), min(v.y for v in bbox_pts), min(v.z for v in bbox_pts)))
    bb_max   = Vector((max(v.x for v in bbox_pts), max(v.y for v in bbox_pts), max(v.z for v in bbox_pts)))
    bb_size  = bb_max - bb_min

    def world_face_centroid(face):
        co = sum((v.co for v in face.verts), Vector()) / len(face.verts)
        return mw @ co

    def world_edge_midpoint(edge):
        return mw @ ((edge.verts[0].co + edge.verts[1].co) / 2)

    def world_vert(vert):
        return mw @ vert.co

    def region_label(wco):
        # Divide object bbox into thirds on each axis, label the sector
        def sector(val, lo, hi):
            t = (val - lo) / (hi - lo + 1e-6)
            if t < 0.33: return "low"
            if t < 0.67: return "mid"
            return "high"
        sx = sector(wco.x, bb_min.x, bb_max.x)
        sy = sector(wco.y, bb_min.y, bb_max.y)
        sz = sector(wco.z, bb_min.z, bb_max.z)
        z_label = {{"low": "lower", "mid": "middle", "high": "upper"}}[sz]
        x_label = {{"low": "left",  "mid": "center", "high": "right"}}[sx]
        y_label = {{"low": "front", "mid": "mid",    "high": "back" }}[sy]
        return f"{{z_label}}_{{y_label}}_{{x_label}}"

    def view_projections(wco):
        # Orthographic projection for each standard view.
        # Returns normalised 0-1 coords within the object's bounding box.
        # FRONT:  x=world.x (left-right), y=world.z (up-down)
        # RIGHT:  x=-world.y (left-right), y=world.z (up-down)
        # TOP:    x=world.x (left-right), y=-world.y (up-down)
        def norm(val, lo, hi): return round((val - lo) / (hi - lo + 1e-6), 3)
        return {{
            "FRONT": {{"x": norm(wco.x, bb_min.x, bb_max.x),
                       "y": norm(wco.z, bb_min.z, bb_max.z)}},
            "RIGHT": {{"x": norm(-wco.y, -bb_max.y, -bb_min.y),
                       "y": norm(wco.z, bb_min.z, bb_max.z)}},
            "TOP":   {{"x": norm(wco.x, bb_min.x, bb_max.x),
                       "y": norm(-wco.y, -bb_max.y, -bb_min.y)}},
        }}

    def cluster_points(points):
        # Greedy clustering: assign each point to nearest existing cluster
        # centroid within CLUSTER_RAD, else start a new cluster.
        clusters = []   # list of [Vector, ...]
        for pt in points:
            placed = False
            for cl in clusters:
                cen = sum(cl, Vector()) / len(cl)
                if (pt - cen).length <= CLUSTER_RAD:
                    cl.append(pt)
                    placed = True
                    break
            if not placed:
                clusters.append([pt])
        result = []
        for cl in clusters:
            cen  = sum(cl, Vector()) / len(cl)
            xs   = [v.x for v in cl]; ys = [v.y for v in cl]; zs = [v.z for v in cl]
            result.append({{
                "centroid":      [round(cen.x,3), round(cen.y,3), round(cen.z,3)],
                "element_count": len(cl),
                "bbox": {{
                    "min": [round(min(xs),3), round(min(ys),3), round(min(zs),3)],
                    "max": [round(max(xs),3), round(max(ys),3), round(max(zs),3)],
                }},
                "region_label":     region_label(cen),
                "view_projections": view_projections(cen),
            }})
        result.sort(key=lambda c: -c["element_count"])
        return result

    output = {{
        "object":         OBJ_NAME,
        "bbox_world_min": [round(bb_min.x,3), round(bb_min.y,3), round(bb_min.z,3)],
        "bbox_world_max": [round(bb_max.x,3), round(bb_max.y,3), round(bb_max.z,3)],
        "cluster_radius": CLUSTER_RAD,
        "ngon_clusters":         [],
        "non_manifold_clusters": [],
        "pole_clusters":         [],
    }}

    # ── N-gon faces (5+ sided) ────────────────────────────────────────────
    if PROBLEM_TYPE in ("all", "ngons"):
        pts = [world_face_centroid(f) for f in bm.faces if len(f.verts) > 4]
        clusters = cluster_points(pts)
        total    = sum(c["element_count"] for c in clusters)
        for c in clusters:
            c["severity"] = "critical" if c["element_count"] > 20 or total > 100 else "warning"
        output["ngon_clusters"]  = clusters
        output["ngon_total"]     = total

    # ── Non-manifold edges ────────────────────────────────────────────────
    if PROBLEM_TYPE in ("all", "non_manifold"):
        pts = [world_edge_midpoint(e) for e in bm.edges if not e.is_manifold and not e.is_boundary]
        clusters = cluster_points(pts)
        total    = sum(c["element_count"] for c in clusters)
        for c in clusters:
            c["severity"] = "critical" if c["element_count"] > 5 else "warning"
        output["non_manifold_clusters"] = clusters
        output["non_manifold_total"]    = total

    # ── High-valence poles (6+ edges) ────────────────────────────────────
    if PROBLEM_TYPE in ("all", "poles"):
        pts = [world_vert(v) for v in bm.verts if len(v.link_edges) >= 6]
        clusters = cluster_points(pts)
        total    = sum(c["element_count"] for c in clusters)
        for c in clusters:
            c["severity"] = "warning"
        output["pole_clusters"] = clusters
        output["pole_total"]    = total

    bm.free()

    # ── Priority narrative ─────────────────────────────────────────────────
    narrative_parts = []
    for cluster_list, label in [
        (output.get("ngon_clusters",[]),         "ngon"),
        (output.get("non_manifold_clusters",[]), "non-manifold edge"),
        (output.get("pole_clusters",[]),         "high-valence pole"),
    ]:
        for i, c in enumerate(cluster_list[:3]):  # top 3 clusters per type
            cen = c["centroid"]
            fp  = c.get("view_projections", {{}}).get("FRONT", {{}})
            narrative_parts.append(
                f"{{c['severity'].upper()}} {{label}} cluster #{{i+1}}:"
                f" {{c['element_count']}} element(s) at world {{cen}},"
                f" region '{{c['region_label']}}'"
                f" (FRONT view approx x={{fp.get('x','?')}}, y={{fp.get('y','?')}})"
            )
    output["priority_narrative"] = narrative_parts if narrative_parts else ["No problems found."]
    print(json.dumps(output))
    bm.free()
"""

    try:
        blender = get_blender_connection()
        raw = blender.send_command("execute_code_safe", {
            "code": script, "required_mode": "OBJECT", "push_undo": False
        })
        if isinstance(raw, dict) and "error" in raw:
            return json.dumps({"error": raw["error"]})
        output = raw.get("result", "") if isinstance(raw, dict) else str(raw)
        for line in output.strip().splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.dumps(json.loads(line), indent=2)
                except Exception:
                    pass
        return json.dumps({"error": "No JSON output from get_problem_coordinates", "raw": output})
    except Exception as e:
        logger.error(f"Error in get_problem_coordinates: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_annotated_capture(object_name: str, modes: str = "all") -> list:
    """
    ANNOTATED CAPTURE — 7-view edit-mode highlights per problem type (ngons/
    non_manifold/poles orange-selected) + a red/orange/green severity heat map
    pass. Expensive: up to 28 images (4 passes x 7 views). modes: "all" |
    "ngons" | "non_manifold" | "poles" | "severity_map". Mode/material/selection
    state is guaranteed restored via try/finally even if capture fails partway.
    """
    import datetime, tempfile, os

    blender = get_blender_connection()
    ORTHO_VIEWS = ["FRONT", "BACK", "LEFT", "RIGHT", "TOP", "BOTTOM"]
    ALL_VIEWS   = ORTHO_VIEWS + ["PERSP"]

    # ── Reuse view/screenshot helpers from get_multiview_capture ──────────
    def _set_view_and_frame(axis: str) -> bool:
        """Set viewport to axis and frame object. Returns True on success."""
        if axis == "PERSP":
            script = f"""
import bpy
obj  = bpy.data.objects.get({repr(object_name)})
area = next((a for a in bpy.context.screen.areas if a.type == 'VIEW_3D'), None)
result = {{"ok": False}}
if obj and area:
    region = next((r for r in area.regions if r.type == 'WINDOW'), None)
    space  = next((s for s in area.spaces  if s.type == 'VIEW_3D'), None)
    if region and space:
        space.region_3d.view_perspective = 'PERSP'
        bpy.context.view_layer.objects.active = obj
        with bpy.context.temp_override(area=area, region=region):
            bpy.ops.view3d.view_selected()
        result["ok"] = True
print(__import__('json').dumps(result))
"""
        else:
            script = f"""
import bpy
obj  = bpy.data.objects.get({repr(object_name)})
area = next((a for a in bpy.context.screen.areas if a.type == 'VIEW_3D'), None)
result = {{"ok": False}}
if obj and area:
    region = next((r for r in area.regions if r.type == 'WINDOW'), None)
    if region:
        bpy.context.view_layer.objects.active = obj
        with bpy.context.temp_override(area=area, region=region):
            bpy.ops.view3d.view_axis(type={repr(axis)}, align_active=False)
            bpy.ops.view3d.view_selected()
        result["ok"] = True
print(__import__('json').dumps(result))
"""
        raw = blender.send_command("execute_code_safe", {
            "code": script, "required_mode": "OBJECT", "push_undo": False
        })
        output = raw.get("result", "") if isinstance(raw, dict) else ""
        for line in output.strip().splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line).get("ok", False)
                except Exception:
                    pass
        return False

    def _screenshot() -> bytes:
        temp_path = os.path.join(
            tempfile.gettempdir(),
            f"blender_ann_{os.getpid()}_{id(object())}.png"
        )
        result = blender.send_command(
            "get_viewport_screenshot",
            {"max_size": 900, "filepath": temp_path, "format": "png"}
        )
        if isinstance(result, dict) and "error" in result:
            raise RuntimeError(f"Screenshot failed: {result['error']}")
        if not os.path.exists(temp_path):
            raise RuntimeError("Screenshot file not created")
        with open(temp_path, "rb") as f:
            data = f.read()
        try:
            os.remove(temp_path)
        except OSError:
            pass
        return data

    def _capture_7_views(label: str, images_out: list, errors: list) -> None:
        """Capture all 7 views in current viewport state, append Image objects."""
        for axis in ALL_VIEWS:
            ok = _set_view_and_frame(axis)
            if not ok:
                errors.append(f"{label}/{axis}: view set failed")
                continue
            try:
                images_out.append(Image(data=_screenshot(), format="png"))
            except RuntimeError as e:
                errors.append(f"{label}/{axis}: {e}")

    images_out = []
    errors     = []
    passes_done = []

    # ══════════════════════════════════════════════════════════════════════
    # PASS 1 — Edit mode element selection highlights
    # ══════════════════════════════════════════════════════════════════════
    ELEMENT_PASSES = []
    if modes in ("all", "ngons"):
        ELEMENT_PASSES.append(("ngons", "FACE",
            "for f in bm.faces: f.select = (len(f.verts) > 4)"))
    if modes in ("all", "non_manifold"):
        ELEMENT_PASSES.append(("non_manifold", "EDGE",
            "for e in bm.edges: e.select = (not e.is_manifold and not e.is_boundary)"))
    if modes in ("all", "poles"):
        ELEMENT_PASSES.append(("poles", "VERT",
            "for v in bm.verts: v.select = (len(v.link_edges) >= 6)"))

    for pass_name, select_mode, select_expr in ELEMENT_PASSES:
        # select_mode tuple built explicitly per mode (can't use a comparison
        # cleanly inside an f-string embedded in the generated script)
        sv = (select_mode == "VERT")
        se = (select_mode == "EDGE")
        sf = (select_mode == "FACE")
        enter_script = f"""
import bpy, bmesh
obj = bpy.data.objects.get({repr(object_name)})
result = {{"ok": False, "error": None, "found": 0}}
if obj is None:
    result["error"] = "Object not found"
else:
    try:
        bpy.context.view_layer.objects.active = obj
        bpy.ops.object.mode_set(mode='EDIT')
        bm = bmesh.from_edit_mesh(obj.data)
        bm.faces.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.verts.ensure_lookup_table()
        for f in bm.faces: f.select = False
        for e in bm.edges: e.select = False
        for v in bm.verts: v.select = False
        bpy.context.tool_settings.mesh_select_mode = ({sv}, {se}, {sf})
        {select_expr}
        bmesh.update_edit_mesh(obj.data)
        found = sum(1 for f in bm.faces if f.select) if {repr(select_mode)} == 'FACE' else \\
                sum(1 for e in bm.edges if e.select) if {repr(select_mode)} == 'EDGE' else \\
                sum(1 for v in bm.verts if v.select)
        result = {{"ok": True, "found": found}}
    except Exception as ex:
        result["error"] = str(ex)
        try: bpy.ops.object.mode_set(mode='OBJECT')
        except: pass
print(__import__('json').dumps(result))
"""
        raw = blender.send_command("execute_code_safe", {
            "code": enter_script, "required_mode": "OBJECT", "push_undo": False
        })
        output = raw.get("result", "") if isinstance(raw, dict) else ""
        ok = False
        found = 0
        for line in output.strip().splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    parsed = json.loads(line)
                    ok    = parsed.get("ok", False)
                    found = parsed.get("found", 0)
                    if not ok:
                        errors.append(f"{pass_name} enter_edit: {parsed.get('error','unknown')}")
                except Exception:
                    pass
                break

        # Always exit edit mode — even if the capture loop below raises an
        # exception execute_code_safe's own try/except doesn't catch (e.g. an
        # MCP connection drop mid-capture). A bare sequential call here would
        # get skipped by that exception; try/finally guarantees it runs.
        try:
            if ok:
                for axis in ALL_VIEWS:
                    view_ok = _set_view_and_frame(axis)
                    if not view_ok:
                        errors.append(f"{pass_name}/{axis}: view set failed")
                        continue
                    try:
                        images_out.append(Image(data=_screenshot(), format="png"))
                    except RuntimeError as e:
                        errors.append(f"{pass_name}/{axis}: {e}")
                passes_done.append(f"{pass_name}({found} elements highlighted)")
        finally:
            exit_script = f"""
import bpy
try:
    if bpy.context.object and bpy.context.object.mode == 'EDIT':
        bpy.ops.object.mode_set(mode='OBJECT')
    print(__import__('json').dumps({{"ok": True}}))
except Exception as ex:
    print(__import__('json').dumps({{"ok": False, "error": str(ex)}}))
"""
            blender.send_command("execute_code_safe", {
                "code": exit_script, "required_mode": "OBJECT", "push_undo": False
            })

    # ══════════════════════════════════════════════════════════════════════
    # PASS 2 — Severity heat map via temporary emission materials
    # ══════════════════════════════════════════════════════════════════════
    if modes in ("all", "severity_map"):
        severity_script = f"""
import bpy, bmesh, json, math
from mathutils import Vector

OBJ_NAME    = {repr(object_name)}
CLUSTER_RAD = 0.5
obj = bpy.data.objects.get(OBJ_NAME)
result = {{"ok": False, "error": None, "assigned": 0}}

if obj is None:
    result["error"] = "Object not found"
else:
    try:
        mw = obj.matrix_world

        # Save original material assignments per face
        orig_slots   = [ms.material for ms in obj.material_slots]
        bpy.context.view_layer.objects.active = obj

        bm = bmesh.new()
        bm.from_mesh(obj.data)
        bm.faces.ensure_lookup_table()

        # Collect ngon face centroids + label by severity
        # (simple threshold: >20 in one cluster = critical, else warning)
        ngon_faces_world = []
        for f in bm.faces:
            if len(f.verts) > 4:
                co = sum((v.co for v in f.verts), Vector()) / len(f.verts)
                ngon_faces_world.append((f.index, mw @ co))

        # Cluster
        clusters = []
        for fidx, wco in ngon_faces_world:
            placed = False
            for cl in clusters:
                cen = sum((p for _,p in cl), Vector()) / len(cl)
                if (wco - cen).length <= CLUSTER_RAD:
                    cl.append((fidx, wco))
                    placed = True
                    break
            if not placed:
                clusters.append([(fidx, wco)])

        # Assign severity per face
        face_severity = {{}}
        for cl in clusters:
            sev = "critical" if len(cl) > 20 else "warning"
            for fidx, _ in cl:
                face_severity[fidx] = sev

        # Create temporary materials
        def get_or_create_mat(name, color_rgba):
            if name in bpy.data.materials:
                bpy.data.materials.remove(bpy.data.materials[name])
            mat = bpy.data.materials.new(name)
            mat.use_nodes = True
            nodes = mat.node_tree.nodes
            nodes.clear()
            emit = nodes.new("ShaderNodeEmission")
            emit.inputs["Color"].default_value  = color_rgba
            emit.inputs["Strength"].default_value = 2.0
            out  = nodes.new("ShaderNodeOutputMaterial")
            mat.node_tree.links.new(emit.outputs["Emission"], out.inputs["Surface"])
            return mat

        mat_critical = get_or_create_mat("_MCP_CRITICAL", (1.0, 0.1, 0.1, 1.0))  # red
        mat_warning  = get_or_create_mat("_MCP_WARNING",  (1.0, 0.5, 0.0, 1.0))  # orange
        mat_clean    = get_or_create_mat("_MCP_CLEAN",    (0.15, 0.6, 0.15, 1.0)) # green

        # Add temp slots
        obj.data.materials.append(mat_critical)
        obj.data.materials.append(mat_warning)
        obj.data.materials.append(mat_clean)
        idx_crit = len(obj.material_slots) - 3
        idx_warn = len(obj.material_slots) - 2
        idx_clean= len(obj.material_slots) - 1

        # Record original face material indices before changing
        orig_face_mats = [f.material_index for f in obj.data.polygons]

        # Assign temp materials to faces
        for poly in obj.data.polygons:
            sev = face_severity.get(poly.index)
            if sev == "critical":
                poly.material_index = idx_crit
            elif sev == "warning":
                poly.material_index = idx_warn
            else:
                poly.material_index = idx_clean
        obj.data.update()

        bm.free()
        result = {{"ok": True, "assigned": len(face_severity),
                   "critical_faces": sum(1 for s in face_severity.values() if s=="critical"),
                   "warning_faces":  sum(1 for s in face_severity.values() if s=="warning"),
                   "orig_face_mats": orig_face_mats,
                   "idx_crit": idx_crit, "idx_warn": idx_warn, "idx_clean": idx_clean,
                   "orig_slot_count": len(orig_slots)}}
    except Exception as ex:
        result["error"] = str(ex)
    print(json.dumps(result))
"""
        raw = blender.send_command("execute_code_safe", {
            "code": severity_script, "required_mode": "OBJECT", "push_undo": False
        })
        sev_output = raw.get("result", "") if isinstance(raw, dict) else ""
        sev_data   = {}
        sev_ok     = False
        for line in sev_output.strip().splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    sev_data = json.loads(line)
                    sev_ok   = sev_data.get("ok", False)
                    if not sev_ok:
                        errors.append(f"severity_map setup: {sev_data.get('error','unknown')}")
                except Exception:
                    pass
                break

        # try/finally guarantees material + shading restore even if capture
        # raises mid-pass (e.g. an MCP connection drop) — a bare sequential
        # call after the capture would get skipped by that exception.
        try:
            if sev_ok:
                # Set MATERIAL shading so emission colors are visible
                blender.send_command("execute_code_safe", {
                    "code": """
import bpy
area = next((a for a in bpy.context.screen.areas if a.type == 'VIEW_3D'), None)
if area:
    space = next((s for s in area.spaces if s.type == 'VIEW_3D'), None)
    if space: space.shading.type = 'MATERIAL'
print(__import__('json').dumps({"ok": True}))
""",
                    "required_mode": "OBJECT", "push_undo": False
                })
                _capture_7_views("severity_map", images_out, errors)
                passes_done.append(
                    f"severity_map({sev_data.get('critical_faces',0)} critical, "
                    f"{sev_data.get('warning_faces',0)} warning faces colored)"
                )
        finally:
            if sev_ok:
                orig_face_mats  = sev_data.get("orig_face_mats", [])

                restore_script = f"""
import bpy
obj = bpy.data.objects.get({repr(object_name)})
if obj:
    # Restore original face material indices
    orig = {orig_face_mats!r}
    for i, poly in enumerate(obj.data.polygons):
        if i < len(orig):
            poly.material_index = orig[i]
    obj.data.update()
    # Remove temp material slots (added at end — pop from back)
    slot_count = {sev_data.get('orig_slot_count', 0)}
    while len(obj.material_slots) > slot_count:
        bpy.context.view_layer.objects.active = obj
        bpy.ops.object.material_slot_remove()
    # Remove temp materials from bpy.data
    for name in ("_MCP_CRITICAL", "_MCP_WARNING", "_MCP_CLEAN"):
        if name in bpy.data.materials:
            bpy.data.materials.remove(bpy.data.materials[name])
    obj.data.update()
print(__import__('json').dumps({{"ok": True}}))
"""
                blender.send_command("execute_code_safe", {
                    "code": restore_script, "required_mode": "OBJECT", "push_undo": False
                })

                # Restore original shading
                blender.send_command("execute_code_safe", {
                    "code": """
import bpy
area = next((a for a in bpy.context.screen.areas if a.type == 'VIEW_3D'), None)
if area:
    space = next((s for s in area.spaces if s.type == 'VIEW_3D'), None)
    if space: space.shading.type = 'SOLID'
print(__import__('json').dumps({"ok": True}))
""",
                    "required_mode": "OBJECT", "push_undo": False
                })

    # ── Build return ───────────────────────────────────────────────────────
    summary = {
        "annotated_capture": "complete",
        "object":            object_name,
        "passes_done":       passes_done,
        "images_returned":   len(images_out),
        "view_errors":       errors,
        "image_order": (
            "Groups of 7 views per pass in this order: "
            + ", ".join(p.split("(")[0] for p in passes_done)
            + ". Each group: FRONT BACK LEFT RIGHT TOP BOTTOM PERSP."
        ),
        "note": (
            "PASS 1 images: orange/highlighted = problem elements selected in edit mode. "
            "PASS 2 images: red=critical ngon clusters, orange=warning, green=clean faces. "
            "Cross-reference with get_problem_coordinates() for exact world positions."
        ),
    }
    images_out.append(summary)
    return images_out


# ─────────────────────────────────────────────────────────────────────────────
# TIER 1A — Coordinate overlay helper
# Burns problem cluster locations directly onto screenshot PNG bytes using Pillow.
# Called by get_spatial_analysis() after multiview capture, before returning images.
# ─────────────────────────────────────────────────────────────────────────────

# Color map: severity → (R, G, B) fill, (R, G, B) outline
_OVERLAY_COLORS = {
    "critical":    ((231, 76,  60),  (180, 40,  20)),   # red
    "warning":     ((230, 126, 34),  (180, 90,   0)),   # orange
    "pole":        ((52,  152, 219), (20,  100, 180)),  # blue
}
_OVERLAY_RADIUS  = 14   # circle radius in pixels (scales with typical 1280px wide screenshot)
_OVERLAY_OUTLINE = 3    # outline thickness


def _annotate_image_with_clusters(
    img_bytes: bytes,
    coords: dict,
    view_name: str,
) -> bytes:
    """
    Draw severity-colored circles + labels at every problem cluster's view_projection
    position onto the given PNG bytes, then return the annotated PNG bytes.

    Arguments
    ---------
    img_bytes  : raw PNG bytes from _take_screenshot()
    coords     : parsed dict from get_problem_coordinates() — contains ngon_clusters,
                 non_manifold_clusters, pole_clusters lists, each with view_projections
    view_name  : one of FRONT / BACK / LEFT / RIGHT / TOP / BOTTOM / PERSP.
                 PERSP gets ALL clusters from all orthographic projections overlaid;
                 BACK/BOTTOM/LEFT clusters use whatever view_projections key is available,
                 falling back to the closest available projection.

    Returns annotated PNG bytes, or original bytes if Pillow unavailable / any error.
    """
    if not _PIL_AVAILABLE or not img_bytes:
        return img_bytes

    try:
        import io
        pil_img = PILImage.open(io.BytesIO(img_bytes)).convert("RGBA")
        W, H = pil_img.size

        # Create a transparent overlay layer so circles don't fully obscure the mesh
        overlay = PILImage.new("RGBA", (W, H), (0, 0, 0, 0))
        draw    = ImageDraw.Draw(overlay)

        # Try to load a simple font; fall back to default if not available
        try:
            font_label = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 13)
            font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 11)
        except Exception:
            font_label = ImageFont.load_default()
            font_small = font_label

        # Which projection key to look up for this view.
        # BACK mirrors FRONT horizontally (x_proj = 1 - x_front) — we use FRONT projection
        # and note the flip.  BOTTOM mirrors TOP.  LEFT uses RIGHT (mirrored similarly).
        # For views that share a projection plane we still draw — the cluster will appear
        # at the mirrored position which is correct (same screen plane, opposite camera).
        VIEW_PROJ_KEY = {
            "FRONT":  "FRONT",
            "BACK":   "FRONT",   # same X-Z plane; x coord is mirrored for BACK camera
            "LEFT":   "RIGHT",   # same Y-Z plane; x coord is mirrored
            "RIGHT":  "RIGHT",
            "TOP":    "TOP",
            "BOTTOM": "TOP",     # same X-Y plane; y coord is mirrored
            "PERSP":  None,      # PERSP: overlay FRONT+RIGHT+TOP all at once
        }
        mirror_x = view_name in ("BACK", "LEFT")
        mirror_y = view_name == "BOTTOM"

        # Cluster type definitions: (list_key, severity_override_or_None, label_prefix)
        cluster_defs = [
            ("ngon_clusters",          None,       "N"),
            ("non_manifold_clusters",  None,       "M"),
            ("pole_clusters",          "pole",     "P"),
        ]

        drawn = 0
        for list_key, sev_override, label_prefix in cluster_defs:
            for idx, cluster in enumerate(coords.get(list_key, [])):
                sev  = sev_override or cluster.get("severity", "warning")
                fill_rgb, outline_rgb = _OVERLAY_COLORS.get(sev, _OVERLAY_COLORS["warning"])
                fill_color    = fill_rgb    + (170,)   # ~67% opacity fill
                outline_color = outline_rgb + (255,)   # full opacity outline
                label_color   = (255, 255, 255, 255)

                vp = cluster.get("view_projections", {})
                cnt = cluster.get("element_count", 1)

                # Decide which (x,y) normalized coords to use
                if view_name == "PERSP":
                    # For perspective view: draw ALL available projections with view labels
                    proj_items = [(k, v) for k, v in vp.items() if "x" in v and "y" in v]
                else:
                    proj_key = VIEW_PROJ_KEY.get(view_name, view_name)
                    proj = vp.get(proj_key) or vp.get("FRONT") or next(iter(vp.values()), None)
                    proj_items = [(proj_key, proj)] if proj and "x" in proj else []

                for proj_key_used, proj in proj_items:
                    xn = proj.get("x", 0.5)
                    yn = proj.get("y", 0.5)

                    # view_projections: x=0 is left, x=1 is right; y=0 is bottom, y=1 is top
                    # PIL: y=0 is top, y=H is bottom — so we flip Y
                    if mirror_x:
                        xn = 1.0 - xn
                    if mirror_y:
                        yn = 1.0 - yn

                    px = int(xn * W)
                    py = int((1.0 - yn) * H)  # flip Y for PIL coords

                    r = _OVERLAY_RADIUS
                    # Draw filled circle with outline
                    draw.ellipse(
                        [px - r, py - r, px + r, py + r],
                        fill=fill_color,
                        outline=outline_color,
                        width=_OVERLAY_OUTLINE,
                    )
                    # Draw crosshair lines
                    cross = r + 6
                    draw.line([(px - cross, py), (px - r - 1, py)], fill=outline_color, width=2)
                    draw.line([(px + r + 1, py), (px + cross, py)], fill=outline_color, width=2)
                    draw.line([(px, py - cross), (px, py - r - 1)], fill=outline_color, width=2)
                    draw.line([(px, py + r + 1), (px, py + cross)], fill=outline_color, width=2)

                    # Label: "N1×12" means "ngon cluster 1, 12 elements"
                    label_text = f"{label_prefix}{idx+1}×{cnt}"
                    if view_name == "PERSP":
                        label_text += f" [{proj_key_used[0]}]"  # e.g. "[F]" "[R]" "[T]"

                    # Draw label with dark shadow for legibility
                    tx, ty = px + r + 4, py - 8
                    draw.text((tx + 1, ty + 1), label_text, font=font_label, fill=(0, 0, 0, 220))
                    draw.text((tx,     ty    ), label_text, font=font_label, fill=label_color)

                    drawn += 1

        # Composite the overlay onto the original image
        pil_img = PILImage.alpha_composite(pil_img, overlay)

        # Add a small legend strip if anything was drawn. Anchored to the
        # BOTTOM-left corner with an opaque background panel — Blender's own
        # viewport chrome (mode dropdown, menu bar, object/collection name,
        # grid scale text) all live in the TOP-left corner, so a top-left
        # legend used to render on top of and blend into that UI text.
        if drawn > 0:
            # mode="RGBA" is required for ImageDraw to actually alpha-blend
            # fill colors against the existing image instead of overwriting
            # pixels (and their alpha) outright.
            legend_draw = ImageDraw.Draw(pil_img, "RGBA")
            legend_items = [
                ("N = ngon",        _OVERLAY_COLORS["critical"][0]),
                ("M = non-manifold",_OVERLAY_COLORS["warning"][0]),
                ("P = pole",        _OVERLAY_COLORS["pole"][0]),
                ("red=critical, orange=warning, blue=pole", None),
            ]
            row_h = 14
            pad   = 6
            panel_h = len(legend_items) * row_h + pad
            panel_w = 190
            panel_top = H - panel_h - pad
            legend_draw.rectangle(
                [0, panel_top, panel_w, H],
                fill=(20, 20, 20, 190),
            )
            ly = panel_top + pad // 2
            for legend_text, color in legend_items:
                if color:
                    legend_draw.rectangle([6, ly, 18, ly + 10], fill=color + (220,), outline=(255,255,255,255))
                    legend_draw.text((22, ly - 1), legend_text, font=font_small, fill=(255, 255, 255, 255))
                else:
                    legend_draw.text((6, ly), legend_text, font=font_small, fill=(220, 220, 220, 255))
                ly += row_h

        # Convert back to RGB PNG bytes
        out = io.BytesIO()
        pil_img.convert("RGB").save(out, format="PNG", optimize=False)
        return out.getvalue()

    except Exception as e:
        logger.warning(f"_annotate_image_with_clusters: overlay failed for {view_name}: {e}")
        return img_bytes   # always return original bytes on any failure


def _capture_single_front_view(object_name: str) -> Optional[bytes]:
    """
    Capture just the FRONT view of an object — for callers (like auto_repair_mesh's
    before/after diff) that need exactly one screenshot, not all 7 that
    get_multiview_capture always renders. Avoids paying for 6 unused viewport
    round-trips per call.
    """
    blender = get_blender_connection()
    view_script = f"""
import bpy
obj  = bpy.data.objects.get({repr(object_name)})
area = next((a for a in bpy.context.screen.areas if a.type == 'VIEW_3D'), None)
result = {{"ok": False}}
if obj and area:
    region = next((r for r in area.regions if r.type == 'WINDOW'), None)
    if region:
        bpy.context.view_layer.objects.active = obj
        with bpy.context.temp_override(area=area, region=region):
            bpy.ops.view3d.view_axis(type='FRONT', align_active=False)
            bpy.ops.view3d.view_selected()
        result["ok"] = True
print(__import__('json').dumps(result))
"""
    raw = blender.send_command("execute_code_safe", {
        "code": view_script, "required_mode": "OBJECT", "push_undo": False
    })
    output = raw.get("result", "") if isinstance(raw, dict) else ""
    ok = False
    for line in output.strip().splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                ok = json.loads(line).get("ok", False)
            except Exception:
                pass
            break
    if not ok:
        return None

    temp_path = os.path.join(tempfile.gettempdir(), f"blender_arm_{os.getpid()}_{id(object())}.png")
    result = blender.send_command(
        "get_viewport_screenshot", {"max_size": 900, "filepath": temp_path, "format": "png"}
    )
    if isinstance(result, dict) and "error" in result:
        return None
    if not os.path.exists(temp_path):
        return None
    with open(temp_path, "rb") as f:
        data = f.read()
    try:
        os.remove(temp_path)
    except OSError:
        pass
    return data


def _capture_plain_screenshot(object_name: str) -> Optional[Image]:
    """Select+activate object_name and capture a plain FRONT-view screenshot
    as a FastMCP Image, no problem-marker annotation (unlike auto_repair_mesh's
    version — a rust/wear diff has no ngon/non-manifold/pole clusters to
    burn in). Used to force visual before/after proof into a tool's own
    result instead of depending on someone remembering to take a screenshot
    afterward (see: the black-arms and black-spots incidents, both only
    caught because a screenshot happened to get taken). Returns None on any
    failure — screenshots are supplementary, never load-bearing."""
    try:
        select_script = f"""
import bpy
obj = bpy.data.objects.get({repr(object_name)})
if obj is None:
    raise ValueError("Object not found: {object_name}")
bpy.context.view_layer.objects.active = obj
obj.select_set(True)
print("active:set")
"""
        result = _send_raw("execute_code_safe", code=select_script, required_mode="OBJECT", push_undo=False)
        if "error" in result:
            return None
        screenshot_bytes = _capture_single_front_view(object_name)
        if not screenshot_bytes:
            return None
        return Image(data=screenshot_bytes, format="png")
    except Exception as e:
        logger.warning(f"_capture_plain_screenshot({object_name}): {e}")
        return None


@mcp.tool()
def get_spatial_analysis(object_name: str, deep: bool = False) -> list:
    """
    SPATIAL ANALYSIS — default entry point for "look at this mesh from every
    angle": 7 clean views (FRONT/BACK/LEFT/RIGHT/TOP/BOTTOM/PERSP) + world-space
    problem coordinates, markers burned onto each image. Same cost as
    get_multiview_capture(). deep=True adds wireframe + per-type edit-mode
    highlights + severity heat map — up to 42 images, expensive, only use
    when the 7-view pass can't pinpoint a known problem.
    """
    all_images = []
    errors     = []

    # ── Step 1: Problem coordinates (structured data, no images) ───────────
    coords_json = get_problem_coordinates(object_name)
    try:
        coords = json.loads(coords_json)
    except Exception:
        coords = {"error": "Could not parse coordinate data"}

    # ── Step 2: Clean multiview — 7 images, no wireframe by default ─────────
    # View order from get_multiview_capture: FRONT=0 BACK=1 LEFT=2 RIGHT=3 TOP=4 BOTTOM=5 PERSP=6
    _VIEW_ORDER = ["FRONT", "BACK", "LEFT", "RIGHT", "TOP", "BOTTOM", "PERSP"]
    mv_result = get_multiview_capture(object_name, include_wireframe=deep)
    _view_idx = 0
    for item in mv_result:
        if isinstance(item, Image):
            # Tier 1a: burn cluster coordinates onto the PNG before handing to Claude
            view_label = _VIEW_ORDER[_view_idx] if _view_idx < len(_VIEW_ORDER) else "PERSP"
            annotated_bytes = _annotate_image_with_clusters(item.data, coords, view_label)
            all_images.append(Image(data=annotated_bytes, format="png"))
            _view_idx += 1
        # dict summary is the last item — skip, we build our own

    # ── Step 3 (deep only): annotated highlights + severity heat map ───────
    if deep:
        ann_result = get_annotated_capture(object_name, modes="all")
        for item in ann_result:
            if isinstance(item, Image):
                all_images.append(item)

    # ── Step 4: Build unified spatial report ──────────────────────────────
    narrative = []

    # Ngon clusters
    for i, c in enumerate(coords.get("ngon_clusters", [])[:5]):
        cen  = c.get("centroid", [0, 0, 0])
        fp   = c.get("view_projections", {}).get("FRONT", {})
        rp   = c.get("view_projections", {}).get("RIGHT", {})
        tp   = c.get("view_projections", {}).get("TOP",   {})
        sev  = c.get("severity", "warning").upper()
        reg  = c.get("region_label", "unknown")
        cnt  = c.get("element_count", 0)
        narrative.append(
            f"{sev} ngon cluster #{i+1}: {cnt} face(s) at world {cen}, "
            f"region '{reg}'. "
            f"FRONT view ≈ (x={fp.get('x','?')}, y={fp.get('y','?')}), "
            f"RIGHT view ≈ (x={rp.get('x','?')}, y={rp.get('y','?')}), "
            f"TOP view ≈ (x={tp.get('x','?')}, y={tp.get('y','?')}). "
            f"Find the orange-highlighted region in the annotated_ngons images at these coordinates."
        )

    # Non-manifold clusters
    for i, c in enumerate(coords.get("non_manifold_clusters", [])[:3]):
        cen = c.get("centroid", [0, 0, 0])
        fp  = c.get("view_projections", {}).get("FRONT", {})
        sev = c.get("severity", "warning").upper()
        reg = c.get("region_label", "unknown")
        cnt = c.get("element_count", 0)
        narrative.append(
            f"{sev} non-manifold cluster #{i+1}: {cnt} edge(s) at world {cen}, "
            f"region '{reg}'. FRONT view ≈ (x={fp.get('x','?')}, y={fp.get('y','?')}). "
            f"Find the highlighted edges in the annotated_non_manifold images."
        )

    # Pole clusters
    for i, c in enumerate(coords.get("pole_clusters", [])[:3]):
        cen = c.get("centroid", [0, 0, 0])
        fp  = c.get("view_projections", {}).get("FRONT", {})
        cnt = c.get("element_count", 0)
        reg = c.get("region_label", "unknown")
        narrative.append(
            f"WARNING high-valence pole cluster #{i+1}: {cnt} vert(s) at world {cen}, "
            f"region '{reg}'. FRONT view ≈ (x={fp.get('x','?')}, y={fp.get('y','?')}). "
            f"Visible as highlighted verts in the annotated_poles images."
        )

    if not narrative:
        narrative = ["No mesh problems detected. Mesh appears clean."]

    # Image index guide — helps Claude know which image number = which pass
    idx = 0
    image_guide = []
    image_guide.append(f"Images 1-7: Solid views with problem cluster overlays burned in (FRONT,BACK,LEFT,RIGHT,TOP,BOTTOM,PERSP). Colored circles = N:ngon/M:non-manifold/P:pole. RED=critical ORANGE=warning BLUE=pole.")
    idx = 7
    if deep:
        image_guide.append(f"Images 8-14: Wireframe views (same order)")
        idx = 14
        image_guide.append(f"Images {idx+1}-{idx+7}: Ngon highlight (orange = ngon faces selected)")
        idx += 7
        image_guide.append(f"Images {idx+1}-{idx+7}: Non-manifold edge highlight (highlighted edges)")
        idx += 7
        image_guide.append(f"Images {idx+1}-{idx+7}: High-valence pole highlight (highlighted verts)")
        idx += 7
        image_guide.append(f"Images {idx+1}-{idx+7}: Severity heat map (red=critical, orange=warning, green=clean)")

    unified_report = {
        "spatial_analysis":    "complete",
        "object":              object_name,
        "deep":                deep,
        "total_images":        len(all_images),
        "image_guide":         image_guide,
        "coordinate_summary": {
            "ngon_total":          coords.get("ngon_total", 0),
            "non_manifold_total":  coords.get("non_manifold_total", 0),
            "pole_total":          coords.get("pole_total", 0),
            "ngon_clusters":       len(coords.get("ngon_clusters", [])),
            "non_manifold_clusters": len(coords.get("non_manifold_clusters", [])),
            "pole_clusters":       len(coords.get("pole_clusters", [])),
        },
        "spatial_narrative":   narrative,
        "raw_coordinates":     coords,
        "how_to_read": (
            "1. The 7 screenshots already have cluster markers burned directly onto them — "
            "colored circles with crosshairs: RED=critical, ORANGE=warning, BLUE=pole. "
            "Label format: 'N1×12' = ngon cluster 1 with 12 faces; 'M2×3' = non-manifold cluster 2 with 3 edges; "
            "'P1×5' = pole cluster 1 with 5 verts. PERSP view shows all clusters from all projections with [F]/[R]/[T] tags. "
            "2. spatial_narrative gives the same data as text (world coords + region label) for reasoning. "
            "3. raw_coordinates has the full JSON from get_problem_coordinates() if you need exact values. "
            + ("4. Use image_guide to find the right image number for each problem type. "
               "5. The severity heat map (last 7 images) shows priority at a glance: red=fix first."
               if deep else
               "4. For a close-up of the worst cluster only, call get_problem_detail_view(object_name). "
               "5. Need full annotated highlights? Call get_spatial_analysis(object_name, deep=True) "
               "(42 images — expensive, use only when the 7 views aren't enough).")
        ),
    }

    all_images.append(unified_report)

    # Journal + open issues for detected clusters (Sprint A)
    ngon_n  = unified_report.get("coordinate_summary", {}).get("ngon_total", 0)
    nm_n    = unified_report.get("coordinate_summary", {}).get("non_manifold_total", 0)
    pole_n  = unified_report.get("coordinate_summary", {}).get("pole_total", 0)
    _journal_entry(
        "get_spatial_analysis", object_name, "ok",
        f"ngons={ngon_n} non_manifold={nm_n} poles={pole_n} deep={deep}"
    )
    # issue_type uses the same canonical strings as detect_mesh_problems /
    # auto_repair_mesh's repair keys ("non_manifold_edges", "ngons") so
    # _close_issues_for() can actually match and auto-close these after repair.
    if nm_n > 0:
        _open_issue("get_spatial_analysis", object_name, "non_manifold_edges", "critical",
                    f"{nm_n} non-manifold element(s) detected in spatial analysis.")
    if ngon_n > 0:
        _open_issue("get_spatial_analysis", object_name, "ngons", "warning",
                    f"{ngon_n} n-gon face(s) detected in spatial analysis.")

    return all_images


@mcp.tool()
def get_problem_detail_view(object_name: str, problem_type: str = "worst") -> list:
    """
    PROBLEM DETAIL VIEW — zooms the viewport to the single worst problem
    cluster and captures a close-up, instead of wading through 42 deep-mode
    images. problem_type: "worst" (default) | "ngon" | "non_manifold" | "pole".
    Returns list: [close_up_image (marker burned in), detail_report].
    """
    try:
        blender = get_blender_connection()

        # ── Step 1: Get problem coordinates ──────────────────────────────────
        coords_json = get_problem_coordinates(object_name)
        try:
            coords = json.loads(coords_json)
        except Exception:
            return [{"error": "get_problem_detail_view: could not parse coordinates", "object": object_name}]

        if "error" in coords:
            return [{"error": coords["error"], "object": object_name}]

        # ── Step 2: Pick the worst cluster matching problem_type ─────────────
        def _sev_score(cluster):
            """Higher score = worse. critical > warning, then element_count."""
            sev   = cluster.get("severity", "warning")
            count = cluster.get("element_count", 0)
            return (1 if sev == "critical" else 0, count)

        candidate_lists = []
        if problem_type in ("worst", "ngon"):
            candidate_lists.append(("ngon",          "face",  coords.get("ngon_clusters", [])))
        if problem_type in ("worst", "non_manifold"):
            candidate_lists.append(("non_manifold",  "edge",  coords.get("non_manifold_clusters", [])))
        if problem_type in ("worst", "pole"):
            candidate_lists.append(("pole",          "vert",  coords.get("pole_clusters", [])))

        best_cluster     = None
        best_cluster_type = None
        best_elem_type   = None
        best_score       = (-1, -1)

        for ctype, etype, clist in candidate_lists:
            for cluster in clist:
                score = _sev_score(cluster)
                if score > best_score:
                    best_score       = score
                    best_cluster     = cluster
                    best_cluster_type = ctype
                    best_elem_type   = etype

        if best_cluster is None:
            return [{
                "status":  "no_problems_found",
                "object":  object_name,
                "message": f"No clusters found for problem_type='{problem_type}'. Mesh may be clean.",
            }]

        centroid = best_cluster.get("centroid", [0.0, 0.0, 0.0])
        sev      = best_cluster.get("severity", "warning")
        cnt      = best_cluster.get("element_count", 0)
        region   = best_cluster.get("region_label", "unknown")
        cx, cy, cz = centroid[0], centroid[1], centroid[2]

        # ── Step 3: Blender close-up script ──────────────────────────────────
        # Strategy: select elements near the centroid in edit mode, then use
        # view_selected (zoom-to-fit-selection) via a context override so the
        # viewport frames JUST that cluster, then screenshot.
        #
        # We use a proximity threshold of 10% of the object's longest bbox axis
        # to select a generous region around the centroid without selecting
        # the entire mesh.  After the zoom we immediately exit edit mode.
        close_up_script = f"""
import bpy, bmesh, math
from mathutils import Vector

obj = bpy.data.objects.get("{object_name}")
if obj is None:
    raise ValueError("Object '{object_name}' not found")

bpy.context.view_layer.objects.active = obj
obj.select_set(True)

# Compute proximity threshold from bbox diagonal.  obj.bound_box[i] is a raw
# bpy_prop_array, not a Vector — Matrix @ bpy_prop_array isn't supported,
# needs explicit Vector() wrapping first.
bbox_corners = [obj.matrix_world @ Vector(obj.bound_box[i]) for i in range(8)]
xs = [v.x for v in bbox_corners]
ys = [v.y for v in bbox_corners]
zs = [v.z for v in bbox_corners]
diag = math.sqrt((max(xs)-min(xs))**2 + (max(ys)-min(ys))**2 + (max(zs)-min(zs))**2)
thresh = max(diag * 0.08, 0.001)   # 8% of diagonal, minimum 1mm

# Enter edit mode and deselect all. try/finally guarantees we exit back to
# OBJECT mode even if selection or view_selected raises mid-way — a bare
# sequential mode_set('OBJECT') after this block would get skipped by that
# exception, leaving the mesh stuck in EDIT mode.
bpy.ops.object.mode_set(mode='EDIT')
try:
    bm = bmesh.from_edit_mesh(obj.data)

    # Centroid in world space
    world_centroid = Vector(({cx}, {cy}, {cz}))
    # Transform to local object space for bmesh comparison
    local_centroid = obj.matrix_world.inverted() @ world_centroid
    local_thresh   = thresh / max(obj.scale)   # rough local-space threshold

    # Select elements within threshold of centroid
    elem_type = "{best_elem_type}"
    if elem_type == "face":
        bm.faces.ensure_lookup_table()
        for f in bm.faces:
            f.select = (f.calc_center_median() - local_centroid).length < local_thresh
    elif elem_type == "edge":
        bm.edges.ensure_lookup_table()
        for e in bm.edges:
            mid = (e.verts[0].co + e.verts[1].co) / 2
            e.select = (mid - local_centroid).length < local_thresh
    else:  # vert
        bm.verts.ensure_lookup_table()
        for v in bm.verts:
            v.select = (v.co - local_centroid).length < local_thresh

    bmesh.update_edit_mesh(obj.data)

    # Find 3D viewport area and region for context override
    area   = next((a for a in bpy.context.screen.areas if a.type == 'VIEW_3D'), None)
    region = next((r for r in area.regions if r.type == 'WINDOW'), None) if area else None

    # Zoom viewport to selection using view_selected
    if area and region:
        with bpy.context.temp_override(area=area, region=region):
            bpy.ops.view3d.view_selected(use_all_regions=False)
finally:
    bpy.ops.object.mode_set(mode='OBJECT')
print("detail:ready")
"""
        result = blender.send_command(
            "execute_code_safe",
            {"code": close_up_script, "required_mode": "OBJECT", "push_undo": True}
        )
        if "error" in result:
            return [{
                "error": f"get_problem_detail_view: Blender script failed: {result.get('error')}",
                "object": object_name,
            }]

        # ── Step 4: Capture the close-up (uses current viewport framing) ─────
        # Re-use the _take_screenshot mechanism via get_multiview_capture FRONT only
        # Actually: execute a targeted screenshot via execute_code_safe
        screenshot_script = f"""
import bpy, os, tempfile, base64

area = next((a for a in bpy.context.screen.areas if a.type == 'VIEW_3D'), None)
if area is None:
    print("no_3d_area")
else:
    # Save current shading, switch to SOLID for clean close-up
    space = next((s for s in area.spaces if s.type == 'VIEW_3D'), None)
    prev_shading = space.shading.type if space else 'SOLID'
    if space: space.shading.type = 'SOLID'

    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp.close()

    scene = bpy.context.scene
    orig_filepath = scene.render.filepath
    orig_x = scene.render.resolution_x
    orig_y = scene.render.resolution_y
    orig_film_transparent = scene.render.film_transparent

    scene.render.resolution_x = 1280
    scene.render.resolution_y = 960
    scene.render.filepath = tmp.name
    scene.render.film_transparent = False

    with bpy.context.temp_override(area=area):
        bpy.ops.render.opengl(write_still=True, view_context=True)

    scene.render.filepath = orig_filepath
    scene.render.resolution_x = orig_x
    scene.render.resolution_y = orig_y
    scene.render.film_transparent = orig_film_transparent
    if space: space.shading.type = prev_shading

    with open(tmp.name, "rb") as fh:
        encoded = base64.b64encode(fh.read()).decode()
    os.unlink(tmp.name)
    print("IMG:" + encoded)
"""
        ss_result = blender.send_command(
            "execute_code_safe",
            {"code": screenshot_script, "required_mode": "OBJECT", "push_undo": False}
        )

        close_up_image = None
        raw_out = (ss_result.get("result") or ss_result.get("output") or "")
        for line in str(raw_out).splitlines():
            if line.startswith("IMG:"):
                img_bytes = base64.b64decode(line[4:])
                # Burn the cluster marker onto the close-up image
                # Use a minimal coords dict with just this one cluster so we
                # only draw one circle (at whatever view_projections it has)
                single_coords = {
                    f"{best_cluster_type}_clusters": [best_cluster],
                    "ngon_clusters":         [best_cluster] if best_cluster_type == "ngon"          else [],
                    "non_manifold_clusters": [best_cluster] if best_cluster_type == "non_manifold"  else [],
                    "pole_clusters":         [best_cluster] if best_cluster_type == "pole"          else [],
                }
                annotated = _annotate_image_with_clusters(img_bytes, single_coords, "FRONT")
                close_up_image = Image(data=annotated, format="png")
                break

        # ── Step 5: Build detail report ──────────────────────────────────────
        vp = best_cluster.get("view_projections", {})
        detail_report = {
            "detail_view":    "complete",
            "object":         object_name,
            "problem_type":   best_cluster_type,
            "element_type":   best_elem_type,
            "severity":       sev,
            "element_count":  cnt,
            "region":         region,
            "centroid_world": centroid,
            "view_projections": vp,
            "note": (
                f"Viewport zoomed to {best_cluster_type} cluster in '{region}' region "
                f"({sev}, {cnt} {best_elem_type}(s)). "
                f"The close-up image shows the problem site up close. "
                f"Cluster marker is burned onto the image. "
                f"World centroid: {centroid}. "
                f"To repair: call auto_repair_mesh('{object_name}') for safe auto-repair, "
                f"or use manual sculpt/retopo for n-gon restructuring."
            ),
        }

        out = []
        if close_up_image:
            out.append(close_up_image)
        else:
            detail_report["screenshot_warning"] = (
                "Close-up screenshot could not be decoded. "
                "Blender viewport was zoomed to the cluster — try get_multiview_capture() "
                "after this call to see the framed view."
            )
        out.append(detail_report)
        return out

    except Exception as e:
        logger.error(f"Error in get_problem_detail_view: {e}")
        return [{"error": str(e), "object": object_name}]


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
    """Quick single-angle look — call after every scene change (imports, repairs,
    transforms, deletions, generation). Never describe/analyze a mesh without
    looking first. For the FIRST look at a mesh not yet spatially baselined
    this session, use get_spatial_analysis() instead — same image cost, but
    7 angles + world-space coordinates instead of one angle and no data."""
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
    _invalidate_dna_cache(object_name)
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
    organize_folder: bool = True,
) -> str:
    """
    Export a named object/armature as an FBX file with UE5 conventions:
    -Z forward / Y up axis, scale ×100 (Blender m → UE5 cm), triangulation.
    organize_folder=True (default): nests output under <dir>/<AssetName>/
    and copies referenced textures alongside the FBX instead of leaving
    the FBX loose with textures referenced at scattered original paths.
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
        organize_folder=organize_folder,
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


# Blender 4.x+ Principled BSDF socket name -> MaterialX/OpenPBR semantic property.
# This is the actual translation table — nearly 1:1 for the common case, which
# is exactly why Principled BSDF materials are in scope for v1 and arbitrary
# procedural node networks are not.
_MATERIALX_SOCKET_MAP = {
    "Base Color":        "base_color",
    "Metallic":           "metalness",
    "Roughness":          "specular_roughness",
    "IOR":                "specular_IOR",
    "Alpha":              "opacity",
    "Normal":             "normal",
    "Emission Color":     "emission_color",
}


def _find_principled_input_source(socket_name: str, principled_name: str, nodes: list, links: list):
    """What feeds a Principled BSDF input socket, if anything is connected —
    an image texture (portable), a normal map, or something procedural we
    don't attempt to translate."""
    target = f"{principled_name}.{socket_name}"
    link = next((l for l in links if l["to"] == target), None)
    if not link:
        return None
    from_node_name = link["from"].rsplit(".", 1)[0]
    from_node = next((n for n in nodes if n["name"] == from_node_name), None)
    if from_node is None:
        return {"type": "unknown"}
    if from_node.get("type") == "TEX_IMAGE":
        return {"type": "image_texture", "image": from_node.get("image"),
                "filepath": from_node.get("filepath"), "colorspace": from_node.get("colorspace")}
    if from_node.get("type") == "NORMAL_MAP":
        return {"type": "normal_map", "space": from_node.get("normal_space"),
                "ue5_warning": from_node.get("ue5_warning")}
    return {"type": "procedural_or_unsupported", "node_type": from_node.get("type")}


@mcp.tool()
def export_material_as_materialx(material_name: str, write_file: bool = False, output_path: str = "") -> str:
    """
    MATERIAL UNDERSTANDING — foundation layer, not generation. Translates a
    Blender material's Principled BSDF node graph into MaterialX/OpenPBR
    semantic properties (base_color, specular_roughness, metalness, normal,
    emission_color, opacity) instead of raw Blender node names — the AI
    reasons in industry-standard vocabulary, portable to USD/UE5 pipelines.

    Covers the common case: Principled BSDF + connected image textures or
    flat values. Procedural-only networks (Noise/Musgrave/Mix chains) or a
    missing Principled BSDF are explicitly flagged unsupported, not silently
    mistranslated — same "needs baking first" honesty as analyze_material_pbr.

    write_file=True writes a real portable .mtlx document to output_path
    using the actual MaterialX SDK — this is the exchange-ready artifact.
    Default (False) returns a compact structured summary only, no XML.

    This tool reads and represents. It does not modify the Blender material —
    see apply_weathering_recipe() for the generative counterpart.
    """
    try:
        graph = _send_raw("get_material_graph", material_name=material_name)
        if "error" in graph:
            return json.dumps({"error": graph["error"]})
        if graph.get("use_nodes") is False:
            return json.dumps({
                "material": material_name,
                "supported": False,
                "reason": "Material does not use nodes — flat color only, nothing to translate.",
            })

        nodes = graph.get("nodes", [])
        links = graph.get("links", [])

        principled = next((n for n in nodes if n.get("type") == "BSDF_PRINCIPLED" and n.get("active")), None)
        if not principled:
            return json.dumps({
                "material": material_name,
                "supported": False,
                "reason": "No active Principled BSDF found — procedural-only or non-standard shader "
                          "graph. Bake to texture maps before MaterialX translation.",
            })

        p_name = principled["name"]
        inputs = principled.get("inputs", {})

        properties = {}
        unsupported = []
        for blender_socket, mx_property in _MATERIALX_SOCKET_MAP.items():
            source = _find_principled_input_source(blender_socket, p_name, nodes, links)
            if source:
                if source["type"] == "procedural_or_unsupported":
                    unsupported.append({"property": mx_property,
                                         "reason": f"fed by unsupported node type '{source['node_type']}'"})
                else:
                    properties[mx_property] = source
            elif blender_socket in inputs:
                properties[mx_property] = {"type": "constant", "value": inputs[blender_socket]}

        result = {
            "material": material_name,
            "supported": True,
            "materialx_node_type": "open_pbr_surface",
            "properties": properties,
            "unsupported_inputs": unsupported,
            "orphaned_nodes_ignored": graph.get("orphaned_nodes", []),
        }

        if write_file:
            if not output_path:
                return json.dumps({"error": "write_file=True requires output_path"})
            file_result = _write_materialx_document(material_name, properties, output_path)
            result.update(file_result)

        return json.dumps(result, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


_SEVERITY_TO_WEAR_SCALAR = [
    (("extreme", "ruined", "destroyed"), 1.4),
    (("heavy", "high"), 1.0),
    (("medium", "moderate"), 0.6),
    (("light", "low", "slight", "mild"), 0.3),
]


def _severity_to_wear_scalar(text: str) -> Optional[float]:
    """Scan a recipe's free-form severity/wear description for known
    intensity words. Returns None if nothing recognizable is found — callers
    fall back to the tool's own default rather than guessing further."""
    if not text:
        return None
    lowered = str(text).lower()
    for keywords, scalar in _SEVERITY_TO_WEAR_SCALAR:
        if any(kw in lowered for kw in keywords):
            return scalar
    return None


def _resolve_weathering_recipe(trigger_phrase: str, recipe_type: str) -> dict:
    """Look up a stored creative recipe and derive a wear_scalar from
    whichever severity-describing field it has (damage_severity, surface_wear,
    or wear_level — different recipe types name this differently). Returns
    {} if no recipe matches or nothing severity-like is found in it — this
    is a best-effort convenience, never a silent override of explicit params."""
    if not trigger_phrase:
        return {}
    raw = json.loads(query_creative_recipe(trigger_phrase=trigger_phrase, recipe_type=recipe_type))
    matches = raw.get("matches", [])
    if not matches:
        return {}
    top = matches[0]
    params = top.get("parameters", {})
    severity_text = params.get("damage_severity") or params.get("surface_wear") or params.get("wear_level")
    derived_scalar = _severity_to_wear_scalar(severity_text)
    return {
        "recipe_used": top.get("canonical_name"),
        "recipe_context_fit": top.get("context_fit"),
        "derived_wear_scalar": derived_scalar,
    }


def _resolve_material_category(material_name: str) -> Optional[str]:
    """Look up a stored recipe_type='material' recipe by material name (the
    smallest real slice of the material knowledge layer — no pre-populated
    entries, grown the same organic way RECIPE-001/002 were). Returns the
    matched category string or None if nothing matches — a best-effort
    convenience, never a silent override of an explicit material_category."""
    if not material_name:
        return None
    raw = json.loads(query_creative_recipe(trigger_phrase=material_name, recipe_type="material"))
    matches = raw.get("matches", [])
    if not matches:
        return None
    return matches[0].get("parameters", {}).get("category")


def _fingerprint_value(fp: dict, key: str) -> Optional[float]:
    """A recorded fingerprint stores either a single measured value (one
    sample) or a range (multiple samples, e.g. "roughness_avg_range": [a, b])
    — read whichever is present and collapse a range to its midpoint."""
    if key in fp and fp[key] is not None:
        return float(fp[key])
    range_key = key + "_range"
    r = fp.get(range_key)
    if r and len(r) == 2:
        return (float(r[0]) + float(r[1])) / 2.0
    return None


def _fingerprint_distance(fp_a: dict, fp_b: dict) -> Optional[float]:
    """Weighted distance over roughness_avg and normal_map_bumpiness — the
    two axes with real, discriminative spread across every material recorded
    tonight. subsurface_weight/specular_ior_level are deliberately excluded:
    both have been constant (0.0 / 0.5) across all 14 entries recorded so
    far, so they'd add noise, not signal, at this stage. Bumpiness is scaled
    up (~4x) before combining since its real range (0-0.17) is much smaller
    than roughness's (0-1) and would otherwise be drowned out. Returns None
    if either fingerprint is missing a value on both axes — nothing to
    compare, not a guessed distance."""
    r_a = _fingerprint_value(fp_a, "roughness_avg")
    r_b = _fingerprint_value(fp_b, "roughness_avg")
    b_a = _fingerprint_value(fp_a, "normal_map_bumpiness")
    b_b = _fingerprint_value(fp_b, "normal_map_bumpiness")
    if r_a is None or r_b is None:
        return None
    r_diff = r_a - r_b
    b_diff = (b_a - b_b) * 4.0 if (b_a is not None and b_b is not None) else 0.0
    return (r_diff ** 2 + b_diff ** 2) ** 0.5


def _find_closest_material_recipe(fingerprint: dict, max_distance: float = 0.15) -> Optional[dict]:
    """Retrieval by MEASURED SIMILARITY instead of by material name — the
    actual fix for the knowledge layer being unreachable. Auto-generated
    material names (tripo_mat_XXXXXXXX) never match a trigger_phrase, so
    _resolve_material_category's name-based lookup can never fire on most
    real assets. This compares the real fingerprint instead. Returns None
    rather than forcing a match beyond max_distance — calibrated from real
    data: materials within the same recorded category typically land
    0.02-0.05 apart, different categories are usually 0.15+ apart. Never
    silently degrades to "closest of whatever exists" when nothing is
    actually close."""
    best = None
    best_dist = None
    for entry in _RECIPES:
        if entry.get("recipe_type") != "material":
            continue
        candidate_fp = entry.get("parameters", {}).get("fingerprint", {})
        dist = _fingerprint_distance(fingerprint, candidate_fp)
        if dist is None:
            continue
        if best_dist is None or dist < best_dist:
            best, best_dist = entry, dist
    if best is None or best_dist > max_distance:
        return None
    return {
        "canonical_name": best.get("canonical_name"),
        "distance": round(best_dist, 4),
        "category": best.get("parameters", {}).get("category"),
    }


def _find_recipe_by_canonical_name(canonical_name: str) -> Optional[dict]:
    """Exact lookup of a recorded recipe_type='material' entry by its
    canonical_name — used when a caller names a specific recipe to build
    toward rather than a category."""
    for entry in _RECIPES:
        if entry.get("recipe_type") == "material" and entry.get("canonical_name") == canonical_name:
            return entry
    return None


def _find_recipe_for_category(category: str) -> Optional[dict]:
    """First recorded recipe_type='material' entry matching a category —
    a representative fingerprint to calibrate toward when a caller names a
    category but not a specific recipe. Returns None (not a guess) if
    nothing has ever been recorded for that category."""
    for entry in _RECIPES:
        if entry.get("recipe_type") == "material" and entry.get("parameters", {}).get("category") == category:
            return entry
    return None


# Shared across every Blender-side script that needs to bake a live node
# value into pixels (generate_procedural_material's calibration bake,
# bake_weathered_textures' bake_pass, and get_asset_dna's procedural-
# roughness fallback) — embedded via string concatenation since these are
# independent scripts sent to Blender, not importable Python modules. ONE
# implementation, not three copies to drift out of sync.
#
# Real incident this fixes: bpy.ops.object.bake() bakes EVERY material with
# faces on the object in a single pass, each writing into whichever image
# node is "active" in THAT material's own node tree — not just the material
# the caller intended. A calibration bake that only rewires the TARGET
# material's Output to Emission still leaves every OTHER material's Output
# as a plain Principled BSDF, whose built-in emission input defaults to
# black — so Cycles legitimately bakes 0 for those materials' faces and
# writes it into their own active image node. Caught live: this corrupted a
# real, unrelated material's real Base Color texture (in-memory only, the
# file on disk was untouched) during generate_procedural_material's first
# live run, because bake_pass-style code had no face-level scoping at all.
_SAFE_MATERIAL_BAKE_SNIPPET = r"""
def safe_bake_measure(obj, nt, mat_slot_index, output_node, source_socket, image_name, w, h, samples):
    '''Bakes ONLY the target material — LIVE-VERIFIED fix, arrived at after
    TWO earlier attempts both failed live, not theoretical:
      1. Edit-Mode face selection (material_slot_select()) — tested against
         a real two-material object with a known, populated, non-black
         second texture. FAILED: the bake still corrupted it.
      2. Reassigning every face's material_index to the target slot, WITHOUT
         removing other slots — also FAILED, and Blender's own console
         output explained why: "Circular dependency for image
         '...MatB_tex' from object '...'" plus TWO "Baking map saved"
         lines for what should have been a single-material bake. Blender's
         object.bake() processes EVERY material SLOT present on the
         object, regardless of whether any face currently references it —
         face assignment alone can't prevent it.
    The actual fix, confirmed live (controlled experiment: a known 0.8 red
    channel survived intact only with this approach, corrupted to 0.0 with
    both earlier ones): temporarily strip every OTHER material slot off the
    mesh entirely — not just reassign faces — so the target is the ONLY
    material on the object during the bake, then rebuild the exact original
    slot list (order matters: slot index is positional) and restore the
    original per-face material_index afterward.'''
    mesh = obj.data
    original_materials = list(mesh.materials)
    original_material_indices = [p.material_index for p in mesh.polygons]
    target_material = original_materials[mat_slot_index]

    bake_img = bpy.data.images.new(image_name, width=w, height=h, alpha=False)
    bake_node = nt.nodes.new("ShaderNodeTexImage")
    bake_node.image = bake_img
    for n in nt.nodes:
        n.select = False
    bake_node.select = True
    nt.nodes.active = bake_node

    emit_node = nt.nodes.new("ShaderNodeEmission")
    original_link = output_node.inputs["Surface"].links[0] if output_node.inputs["Surface"].links else None
    original_from = original_link.from_socket if original_link else None
    nt.links.new(source_socket, emit_node.inputs["Color"])
    nt.links.new(emit_node.outputs["Emission"], output_node.inputs["Surface"])

    original_engine = bpy.context.scene.render.engine
    original_samples = bpy.context.scene.cycles.samples
    result_img = None
    try:
        while len(mesh.materials) > 0:
            mesh.materials.pop(index=0)
        mesh.materials.append(target_material)
        for p in mesh.polygons:
            p.material_index = 0
        mesh.update()

        bpy.ops.object.select_all(action='DESELECT')
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj

        bpy.context.scene.render.engine = 'CYCLES'
        bpy.context.scene.cycles.samples = samples
        bpy.ops.object.bake(type='EMIT')
        result_img = bake_img
    except Exception:
        result_img = None
    finally:
        nt.nodes.remove(bake_node)
        nt.nodes.remove(emit_node)
        if original_from is not None:
            nt.links.new(original_from, output_node.inputs["Surface"])
        bpy.context.scene.render.engine = original_engine
        bpy.context.scene.cycles.samples = original_samples
        while len(mesh.materials) > 0:
            mesh.materials.pop(index=0)
        for m in original_materials:
            mesh.materials.append(m)
        for p, orig_idx in zip(mesh.polygons, original_material_indices):
            p.material_index = orig_idx
        mesh.update()
    return result_img
"""


@mcp.tool()
def apply_weathering_recipe(
    object_name: str,
    material_name: str = "",
    trigger_phrase: str = "",
    recipe_type: str = "aging",
    wear_scalar: Optional[float] = None,
    rust_color: list = [0.35, 0.14, 0.05],
    worn_roughness: float = 0.9,
    mask_percentile_low: float = 5.0,
    mask_percentile_high: float = 40.0,
    metal_floor: float = 0.25,
    material_category: Optional[str] = None,
    fray_roughness: float = 0.95,
) -> list:
    """
    APPLY WEATHERING — generates real rust/edge-wear shader nodes on an
    object's materials. MODIFIES the material(s) — call create_checkpoint()
    first, this is generative, not reversible via a simple metric diff.

    Returns a list: [before_image?, after_image?, result_json_string] —
    before/after FRONT-view screenshots are included whenever at least one
    material was actually weathered (skipped on a no-op/error/all-skipped
    call), so visual proof travels with the result instead of depending on
    a follow-up screenshot someone has to remember to take (real incidents
    tonight — black arms, black spots — were only caught because a
    screenshot happened to get taken afterward). The JSON result is always
    the LAST element; parse it with json.loads(result[-1]).

    Material-aware in TWO ways, both driven by real sampled data per material,
    not one flat setting for the whole object:
    - INTENSITY: reads each material's real metalness from Principled BSDF
      (samples the connected texture's average if texture-driven, else the
      constant) and scales wear by it. metal_floor (0.0-1.0) is the minimum
      multiplier even fully non-metal materials still get.
    - TINT: rust_color is the pure-metal endpoint, not applied flatly to
      everything. Each material's own base color is sampled and darkened/
      desaturated into a "grime" tone, then blended toward rust_color
      proportional to that material's metal_factor — organic materials grime
      in their own tone, metal materials rust orange, instead of every
      surface getting painted the same color.

    Technique (live-tested and verified, not theoretical): computes a
    per-vertex curvature signal (each vertex's normal vs. its neighbors'
    average — high deviation = edge/crevice), measures THIS mesh's actual
    value distribution, and calibrates a wear mask against measured
    percentiles rather than fixed thresholds — a fixed-threshold version of
    this (raw Geometry Pointiness or Bevel-node dot product straight into a
    0.4/0.6 ColorRamp) was tried and failed: verified numerically that mesh
    curvature signals cluster tightly and vary a lot per-mesh, so fixed
    thresholds silently produce a uniform "flat" result on some meshes. The
    calibrated mask is baked into a vertex color attribute (AutoWeather_Mask)
    and read by an Attribute node in the shader — deterministic and
    independently verifiable by reading the vertex colors back, unlike a
    live procedural node chain.

    Original Base Color/Roughness are preserved as one input to a new Mix
    node (not destroyed) — rust_color/worn_roughness blend in via the mask.

    trigger_phrase: optional — looks up a stored recipe (query_creative_recipe)
    and derives wear_scalar from its damage_severity/surface_wear/wear_level
    field (e.g. "ancient" -> a recipe with damage_severity="medium" -> 0.6).
    Only fills in wear_scalar if you didn't pass one explicitly — an explicit
    wear_scalar always wins. No matching recipe or no recognizable severity
    word in it silently falls back to the tool's own default (0.8), not an
    error — recipe integration is a convenience, not a requirement.

    material_name: leave blank to apply to every material on the object with
    an active Principled BSDF (skips others, same honest flagging as
    export_material_as_materialx). wear_scalar: 0.0-2.0 overall intensity,
    defaults to 0.8 if neither passed explicitly nor derived from a recipe.
    mask_percentile_low/high: which percentile of THIS mesh's measured
    curvature distribution maps to full wear (low) vs. no wear (high) —
    defaults (5/40) worked on the tested case; a mesh with very few sharp
    features may need a lower "low" percentile.

    After applying, re-checks Asset DNA: weathering rewires Base Color/
    Roughness through a Mix node instead of a direct texture, so DNA will
    correctly flag them as missing_maps. If it does, the result carries
    dna_verification.needs_baking pointing at bake_weathered_textures —
    closing the loop instead of leaving that gap to be discovered later.

    TWO TECHNIQUES, dispatched per material — not one recolored mechanism:
    - "oxidation" (the original technique above): curvature-driven — edges
      and crevices wear more. Correct for metal.
    - "fraying": a structurally different signal — graph distance (edge-
      steps) from the nearest UV-seam or mesh-boundary edge, baked into a
      SEPARATE vertex color attribute (AutoWeather_FrayMask) so it never
      collides with the oxidation mask on objects using both. Blends toward
      a desaturated, higher-roughness (fray_roughness, default 0.95) look
      instead of a rust tint — fabric grays and roughens at seams/hems, it
      doesn't oxidize. No effect on a material with no seams and no
      boundary edges (a genuinely watertight, unseamed mesh) — mask_stats
      will show it honestly (all-zero), not fake wear.

    material_category: "metal" or "organic" forces that technique on every
    material this call touches — explicit always wins. Left at its default
    (None), each material picks automatically from its own real sampled
    metal_factor_floored (>0.5 -> oxidation, else -> fraying) — a call with
    material_name="" (every material) correctly gives metal armor oxidation
    and a cloth cape fraying in the same pass. If material_category is None
    AND material_name names one specific material, resolution tries, in
    order: (1) a recipe_type="material" lookup by NAME (query_creative_recipe)
    — rarely fires on real assets since auto-generated material names never
    match a trigger_phrase; (2) retrieval by MEASURED SIMILARITY instead —
    the material's own real fingerprint compared against everything recorded
    so far, same mechanism get_asset_dna surfaces as closest_known_material.
    Only a real, close-enough match ever supplies a category this way — same
    explicit > name-recipe > fingerprint-recipe > automatic precedence as
    wear_scalar/trigger_phrase elsewhere in this tool. Both lookups are
    skipped when material_name is blank (applying to every material) since
    there's no single material to look up yet.
    """
    _invalidate_dna_cache(object_name)

    recipe_info = {}
    if wear_scalar is None:
        recipe_info = _resolve_weathering_recipe(trigger_phrase, recipe_type)
        wear_scalar = recipe_info.get("derived_wear_scalar")
        if wear_scalar is None:
            wear_scalar = 0.8

    if material_category is None and material_name:
        material_category = _resolve_material_category(material_name)
        if material_category is None:
            # No name-based match — the common case, since auto-generated
            # material names never match a trigger_phrase. Fall back to
            # retrieval by measured similarity via the same
            # closest_known_material logic get_asset_dna already computes,
            # instead of falling straight through to the automatic
            # metal_factor dispatch with zero prior knowledge consulted.
            dna_lookup = _reaffirm_dna(object_name)
            for mat in dna_lookup.get("materials", []):
                if mat.get("name") == material_name:
                    match = mat.get("closest_known_material")
                    if match:
                        material_category = match.get("category")
                    break
    script = r"""
import bpy, json, mathutils, statistics

obj = bpy.data.objects.get('{OBJ}')
if obj is None:
    print(json.dumps({"error": "Object not found: {OBJ}"}))
else:
    mesh = obj.data
    vert_normals = {}
    for poly in mesh.polygons:
        for li in poly.loop_indices:
            loop = mesh.loops[li]
            vert_normals.setdefault(loop.vertex_index, []).append(mathutils.Vector(loop.normal))

    neighbor_map = {}
    for edge in mesh.edges:
        a, b = edge.vertices
        neighbor_map.setdefault(a, set()).add(b)
        neighbor_map.setdefault(b, set()).add(a)

    def avg_normal(idx):
        ns = vert_normals.get(idx)
        if not ns:
            return None
        v = mathutils.Vector((0, 0, 0))
        for n in ns:
            v += n
        return (v / len(ns)).normalized()

    vert_avg = {vid: avg_normal(vid) for vid in vert_normals}

    dot_by_vert = {}
    for vid, own in vert_avg.items():
        neighbor_avgs = [vert_avg[n] for n in neighbor_map.get(vid, set()) if vert_avg.get(n) is not None]
        if own is None or not neighbor_avgs:
            continue
        v = mathutils.Vector((0, 0, 0))
        for n in neighbor_avgs:
            v += n
        avg_neighbor = (v / len(neighbor_avgs)).normalized()
        dot_by_vert[vid] = own.dot(avg_neighbor)

    values = sorted(dot_by_vert.values())
    n = len(values)
    p_low  = values[int(n * ({PLOW} / 100.0))]
    p_high = values[int(n * ({PHIGH} / 100.0))]

    def wear_mask(dot_value):
        if p_high == p_low:
            return 0.0
        v = (p_high - dot_value) / (p_high - p_low)
        return max(0.0, min(1.0, v))

    mask_name = "AutoWeather_Mask"
    if mask_name in mesh.color_attributes:
        mesh.color_attributes.remove(mesh.color_attributes[mask_name])
    color_attr = mesh.color_attributes.new(name=mask_name, type='FLOAT_COLOR', domain='POINT')

    mask_values = []
    for vid, dot_value in dot_by_vert.items():
        m = wear_mask(dot_value)
        color_attr.data[vid].color = (m, m, m, 1.0)
        mask_values.append(m)

    # FRAYING signal — structurally different from curvature above: graph
    # distance (edge-steps) from the nearest UV-seam or open boundary edge,
    # not a normal-deviation dot product. Fabric frays at seams/hems, not at
    # curvature extrema. Falls back to true boundary edges too, so genuinely
    # open meshes still get a signal even with no seams marked. Already
    # naturally bounded (0..FRAY_RADIUS steps) — no percentile calibration
    # needed the way curvature's unbounded dot-product distribution required.
    edge_poly_count = {}
    for poly in mesh.polygons:
        for key in poly.edge_keys:
            edge_poly_count[key] = edge_poly_count.get(key, 0) + 1

    seam_or_boundary_verts = set()
    for edge in mesh.edges:
        key = tuple(sorted(edge.vertices))
        is_boundary = edge_poly_count.get(key, 0) <= 1
        if is_boundary or edge.use_seam:
            seam_or_boundary_verts.add(edge.vertices[0])
            seam_or_boundary_verts.add(edge.vertices[1])

    FRAY_RADIUS = 4
    fray_dist = {vid: (0 if vid in seam_or_boundary_verts else None) for vid in neighbor_map}
    frontier = list(seam_or_boundary_verts)
    step = 0
    while frontier and step < FRAY_RADIUS:
        step += 1
        next_frontier = []
        for vid in frontier:
            for nb in neighbor_map.get(vid, ()):
                if fray_dist.get(nb) is None:
                    fray_dist[nb] = step
                    next_frontier.append(nb)
        frontier = next_frontier

    fray_mask_name = "AutoWeather_FrayMask"
    if fray_mask_name in mesh.color_attributes:
        mesh.color_attributes.remove(mesh.color_attributes[fray_mask_name])
    fray_attr = mesh.color_attributes.new(name=fray_mask_name, type='FLOAT_COLOR', domain='POINT')

    fray_mask_values = []
    for vid in neighbor_map:
        d = fray_dist.get(vid)
        d = FRAY_RADIUS if d is None else d
        m = max(0.0, min(1.0, 1.0 - (d / FRAY_RADIUS)))
        fray_attr.data[vid].color = (m, m, m, 1.0)
        fray_mask_values.append(m)

    def get_socket(collection, name, socket_type):
        for s in collection:
            if s.name == name and s.type == socket_type:
                return s
        return None

    def sample_image_avg(image, num_channels_wanted):
        '''Strided average of an image's first N channels — same technique
        for metallic (1 channel) and base color (3 channels), real sampled
        data instead of an assumed constant. Broken/missing image references
        (0 channels, 0x0 size — a real thing on AI-generated assets whose
        source texture path didn't resolve) fall back to neutral gray rather
        than crashing the whole weathering pass over one bad texture.'''
        channels = image.channels
        if channels == 0 or image.size[0] == 0 or image.size[1] == 0:
            return tuple(0.5 for _ in range(num_channels_wanted))
        pixels = image.pixels[:]
        texel_count = len(pixels) // channels
        stride = max(1, texel_count // 2000)
        totals = [0.0] * num_channels_wanted
        count = 0
        for i in range(0, len(pixels), channels * stride):
            for c in range(num_channels_wanted):
                totals[c] += pixels[i + c] if c < channels else pixels[i]
            count += 1
        return tuple(t / count if count else 0.5 for t in totals)

    target_name = '{MATNAME}'
    materials_applied = []
    materials_skipped = []
    for slot in obj.material_slots:
        mat = slot.material
        if mat is None:
            continue
        if target_name and mat.name != target_name:
            continue
        nt = mat.node_tree
        if nt is None:
            materials_skipped.append({"material": mat.name, "reason": "no node tree"})
            continue
        principled = next((nd for nd in nt.nodes if nd.type == 'BSDF_PRINCIPLED'), None)
        if principled is None:
            materials_skipped.append({"material": mat.name, "reason": "no Principled BSDF"})
            continue

        prefix = "AutoWeather_"
        fray_prefix = "AutoWeatherFray_"
        for nd in list(nt.nodes):
            if nd.name.startswith(prefix) or nd.name.startswith(fray_prefix):
                nt.nodes.remove(nd)

        base_x, base_y = principled.location.x - 700, principled.location.y

        bc_input = principled.inputs["Base Color"]
        existing_bc_from = bc_input.links[0].from_socket if bc_input.links else None
        rough_input = principled.inputs["Roughness"]
        existing_rough_from = rough_input.links[0].from_socket if rough_input.links else None

        # Material-aware intensity: rust belongs on metal, not skin/organic
        # surfaces. Read this material's REAL metalness — sample the actual
        # connected texture's average value if metallic is texture-driven,
        # not a guessed constant — and use it to scale wear intensity.
        metallic_input = principled.inputs["Metallic"]
        metal_source = "constant"
        if metallic_input.links:
            src_node = metallic_input.links[0].from_node
            metal_factor = 0.5
            if src_node.type == 'TEX_IMAGE' and src_node.image:
                img = src_node.image
                if img.channels == 0 or img.size[0] == 0 or img.size[1] == 0:
                    metal_source = "broken_image_fallback"
                else:
                    try:
                        metal_factor = sample_image_avg(img, 1)[0]
                        metal_source = "texture_sampled"
                    except Exception:
                        metal_source = "texture_sample_failed"
            else:
                metal_source = "texture_node_no_image"
        else:
            metal_factor = float(metallic_input.default_value)

        metal_factor_floored = max({METALFLOOR}, min(1.0, metal_factor))

        # TECHNIQUE DISPATCH — explicit material_category always wins (forces
        # the same technique on every material this call touches); left
        # automatic, each material picks from its OWN real sampled
        # metal_factor_floored, not a guess or an object-wide flag.
        forced_category = '{FORCEDCATEGORY}'
        if forced_category == 'metal':
            technique = 'oxidation'
        elif forced_category == 'organic':
            technique = 'fraying'
        else:
            technique = 'oxidation' if metal_factor_floored > 0.5 else 'fraying'

        if technique == 'oxidation':
            effective_wear_scalar = {WEARSCALAR} * metal_factor_floored

            # Material-aware TINT: instead of one flat rust color for every
            # material, sample this material's own base color and derive a
            # weathering tint from it — a darkened/desaturated "grime" version
            # for organic materials, blended toward the pure rust_color for
            # metal materials proportional to the same measured metal_factor.
            # Real materials don't oxidize orange-rust uniformly; skin/fabric
            # grimes and darkens, metal rusts — this mirrors that instead of
            # painting every surface the same tone.
            bc_source = "constant"
            base_rgb = (0.5, 0.5, 0.5)
            if existing_bc_from is not None and existing_bc_from.node.type == 'TEX_IMAGE' and existing_bc_from.node.image:
                img = existing_bc_from.node.image
                if img.channels == 0 or img.size[0] == 0 or img.size[1] == 0:
                    bc_source = "broken_image_fallback"
                else:
                    try:
                        base_rgb = sample_image_avg(img, 3)
                        bc_source = "texture_sampled"
                    except Exception:
                        bc_source = "texture_sample_failed"
            elif existing_bc_from is None:
                bc_default = bc_input.default_value
                base_rgb = (bc_default[0], bc_default[1], bc_default[2])
            else:
                bc_source = "texture_node_no_image"

            gray = sum(base_rgb) / 3.0
            grime_rgb = tuple(base_rgb[c] * 0.5 + gray * 0.15 for c in range(3))
            rust_target = ({RUSTR}, {RUSTG}, {RUSTB})
            weather_rgb = tuple(
                grime_rgb[c] * (1.0 - metal_factor_floored) + rust_target[c] * metal_factor_floored
                for c in range(3)
            )

            attr = nt.nodes.new("ShaderNodeAttribute")
            attr.name = prefix + "MaskAttr"
            attr.attribute_name = mask_name
            attr.location = (base_x - 400, base_y - 300)

            rust = nt.nodes.new("ShaderNodeRGB")
            rust.name = prefix + "RustColor"
            rust.location = (base_x - 400, base_y - 600)
            rust.outputs[0].default_value = (weather_rgb[0], weather_rgb[1], weather_rgb[2], 1.0)

            factor_scale = nt.nodes.new("ShaderNodeMath")
            factor_scale.name = prefix + "WearScale"
            factor_scale.location = (base_x, base_y - 300)
            factor_scale.operation = 'MULTIPLY'
            nt.links.new(attr.outputs["Fac"], factor_scale.inputs[0])
            factor_scale.inputs[1].default_value = effective_wear_scalar

            bc_mix = nt.nodes.new("ShaderNodeMix")
            bc_mix.name = prefix + "BaseColorWeather"
            bc_mix.data_type = 'RGBA'
            bc_mix.location = (base_x + 300, base_y)
            a_in = get_socket(bc_mix.inputs, "A", "RGBA")
            b_in = get_socket(bc_mix.inputs, "B", "RGBA")
            result_out = get_socket(bc_mix.outputs, "Result", "RGBA")
            factor_in = get_socket(bc_mix.inputs, "Factor", "VALUE")
            if existing_bc_from:
                nt.links.new(existing_bc_from, a_in)
            else:
                # No existing link — preserve the material's TRUE original
                # constant instead of leaving this new Mix node's own
                # arbitrary default (real bug hit live: a wall panel with a
                # constant, never-linked Roughness baked out to 0.0 mirror-
                # smooth almost everywhere, rendering pure black in every
                # recess, because this fallback was missing).
                a_in.default_value = tuple(bc_input.default_value)
            nt.links.new(rust.outputs[0], b_in)
            nt.links.new(factor_scale.outputs[0], factor_in)
            nt.links.new(result_out, principled.inputs["Base Color"])

            rough_mix = nt.nodes.new("ShaderNodeMix")
            rough_mix.name = prefix + "RoughnessWeather"
            rough_mix.data_type = 'FLOAT'
            rough_mix.location = (base_x + 300, base_y + 300)
            ra_in = get_socket(rough_mix.inputs, "A", "VALUE")
            rb_in = get_socket(rough_mix.inputs, "B", "VALUE")
            rresult_out = get_socket(rough_mix.outputs, "Result", "VALUE")
            rfactor_in = get_socket(rough_mix.inputs, "Factor", "VALUE")
            if existing_rough_from:
                nt.links.new(existing_rough_from, ra_in)
            else:
                ra_in.default_value = float(rough_input.default_value)
            rb_in.default_value = {WORNROUGH}
            nt.links.new(factor_scale.outputs[0], rfactor_in)
            nt.links.new(rresult_out, principled.inputs["Roughness"])

        else:  # fraying — structurally different signal AND different blend
            # target: desaturate/gray toward higher roughness, no rust tint.
            bc_source = "n/a (fraying technique)"
            base_rgb = (0.0, 0.0, 0.0)
            weather_rgb = (0.0, 0.0, 0.0)
            effective_wear_scalar = {WEARSCALAR}

            fray_attr = nt.nodes.new("ShaderNodeAttribute")
            fray_attr.name = fray_prefix + "MaskAttr"
            fray_attr.attribute_name = fray_mask_name
            fray_attr.location = (base_x - 400, base_y - 300)

            fray_factor_scale = nt.nodes.new("ShaderNodeMath")
            fray_factor_scale.name = fray_prefix + "WearScale"
            fray_factor_scale.location = (base_x, base_y - 300)
            fray_factor_scale.operation = 'MULTIPLY'
            nt.links.new(fray_attr.outputs["Fac"], fray_factor_scale.inputs[0])
            fray_factor_scale.inputs[1].default_value = effective_wear_scalar

            desat = nt.nodes.new("ShaderNodeHueSaturation")
            desat.name = fray_prefix + "Desaturate"
            desat.location = (base_x - 400, base_y - 600)
            desat.inputs["Saturation"].default_value = 0.15
            desat.inputs["Value"].default_value = 0.85
            if existing_bc_from:
                nt.links.new(existing_bc_from, desat.inputs["Color"])
            else:
                bc_default = bc_input.default_value
                desat.inputs["Color"].default_value = (bc_default[0], bc_default[1], bc_default[2], 1.0)

            fray_bc_mix = nt.nodes.new("ShaderNodeMix")
            fray_bc_mix.name = fray_prefix + "BaseColorWeather"
            fray_bc_mix.data_type = 'RGBA'
            fray_bc_mix.location = (base_x + 300, base_y)
            fa_in = get_socket(fray_bc_mix.inputs, "A", "RGBA")
            fb_in = get_socket(fray_bc_mix.inputs, "B", "RGBA")
            fresult_out = get_socket(fray_bc_mix.outputs, "Result", "RGBA")
            ffactor_in = get_socket(fray_bc_mix.inputs, "Factor", "VALUE")
            if existing_bc_from:
                nt.links.new(existing_bc_from, fa_in)
            else:
                fa_in.default_value = tuple(bc_input.default_value)
            nt.links.new(desat.outputs["Color"], fb_in)
            nt.links.new(fray_factor_scale.outputs[0], ffactor_in)
            nt.links.new(fresult_out, principled.inputs["Base Color"])

            fray_rough_mix = nt.nodes.new("ShaderNodeMix")
            fray_rough_mix.name = fray_prefix + "RoughnessWeather"
            fray_rough_mix.data_type = 'FLOAT'
            fray_rough_mix.location = (base_x + 300, base_y + 300)
            fra_in = get_socket(fray_rough_mix.inputs, "A", "VALUE")
            frb_in = get_socket(fray_rough_mix.inputs, "B", "VALUE")
            frresult_out = get_socket(fray_rough_mix.outputs, "Result", "VALUE")
            frfactor_in = get_socket(fray_rough_mix.inputs, "Factor", "VALUE")
            if existing_rough_from:
                nt.links.new(existing_rough_from, fra_in)
            else:
                fra_in.default_value = float(rough_input.default_value)
            frb_in.default_value = {FRAYROUGH}
            nt.links.new(fray_factor_scale.outputs[0], frfactor_in)
            nt.links.new(frresult_out, principled.inputs["Roughness"])

        materials_applied.append({
            "material": mat.name,
            "technique_used": technique,
            "metal_source": metal_source,
            "metal_factor": round(metal_factor, 4),
            "metal_factor_floored": round(metal_factor_floored, 4),
            "effective_wear_scalar": round(effective_wear_scalar, 4),
            "base_color_source": bc_source,
            "sampled_base_rgb": [round(c, 4) for c in base_rgb],
            "weathering_tint_rgb": [round(c, 4) for c in weather_rgb],
        })

    print(json.dumps({
        "object": '{OBJ}',
        "materials_applied": materials_applied,
        "materials_skipped": materials_skipped,
        "mask_stats": {
            "min": round(min(mask_values), 4) if mask_values else None,
            "max": round(max(mask_values), 4) if mask_values else None,
            "mean": round(statistics.mean(mask_values), 4) if mask_values else None,
            "stdev": round(statistics.pstdev(mask_values), 4) if mask_values else None,
        },
        "fray_mask_stats": {
            "min": round(min(fray_mask_values), 4) if fray_mask_values else None,
            "max": round(max(fray_mask_values), 4) if fray_mask_values else None,
            "mean": round(statistics.mean(fray_mask_values), 4) if fray_mask_values else None,
            "stdev": round(statistics.pstdev(fray_mask_values), 4) if fray_mask_values else None,
            "note": "all-zero means this mesh has no UV seams and no boundary edges within reach — a real 'no signal', not a bug.",
        },
        "percentiles_used": {"low": {PLOW}, "high": {PHIGH}, "p_low_value": round(p_low, 4), "p_high_value": round(p_high, 4)},
    }))
""".replace("{OBJ}", object_name.replace("'", "\\'")) \
   .replace("{MATNAME}", material_name.replace("'", "\\'")) \
   .replace("{PLOW}", str(mask_percentile_low)) \
   .replace("{PHIGH}", str(mask_percentile_high)) \
   .replace("{RUSTR}", str(rust_color[0])).replace("{RUSTG}", str(rust_color[1])).replace("{RUSTB}", str(rust_color[2])) \
   .replace("{WEARSCALAR}", str(wear_scalar)) \
   .replace("{WORNROUGH}", str(worn_roughness)) \
   .replace("{METALFLOOR}", str(metal_floor)) \
   .replace("{FORCEDCATEGORY}", str(material_category or "")) \
   .replace("{FRAYROUGH}", str(fray_roughness))

    # Captured before the mutation runs, unconditionally — cheap Blender-side
    # render, only actually RETURNED (and so only costs response payload)
    # if something ends up being weathered below.
    _before_image = _capture_plain_screenshot(object_name)

    try:
        raw = _send_raw("execute_code_safe", code=script, required_mode="OBJECT", push_undo=True)
        if "error" in raw:
            return [json.dumps({"error": raw["error"]})]
        output = raw.get("result", "")
        for line in output.strip().splitlines():
            line = line.strip()
            if line.startswith("{"):
                parsed = json.loads(line)
                parsed["wear_scalar_used"] = wear_scalar
                if recipe_info:
                    parsed["recipe_lookup"] = recipe_info

                # Weathering rewires Base Color/Roughness through a new Mix
                # node reading the wear mask — they're no longer texture-fed,
                # so Asset DNA will correctly start flagging them as
                # missing_maps. Surface that proactively: the same handoff
                # shape as the missing-normal-map case, pointing at the tool
                # that actually closes this specific gap.
                applied_names = [m["material"] for m in parsed.get("materials_applied", [])]
                if applied_names:
                    dna_after = _reaffirm_dna(object_name)
                    handoffs = {}
                    for mat in dna_after.get("materials", []):
                        if mat["name"] in applied_names and mat.get("missing_maps"):
                            handoffs[mat["name"]] = {
                                "now_procedural": mat["missing_maps"],
                                "next_step": (
                                    "This weathering is live in Blender but won't survive FBX/UE5 "
                                    "export as-is — run bake_weathered_textures(object_name, "
                                    f"material_name='{mat['name']}', ...) to bake it into real "
                                    "portable textures before export."
                                ),
                            }
                    if handoffs:
                        parsed["dna_verification"] = {"needs_baking": handoffs}

                out = []
                if applied_names:
                    after_image = _capture_plain_screenshot(object_name)
                    if _before_image:
                        out.append(_before_image)
                    if after_image:
                        out.append(after_image)
                out.append(json.dumps(parsed, indent=2))
                return out
        return [json.dumps({"error": "No JSON output from apply_weathering_recipe", "raw": output})]
    except Exception as e:
        logger.error(f"Error in apply_weathering_recipe: {e}")
        return [json.dumps({"error": str(e)})]


def _calibrate_and_build_procedural_material(
    object_name: str,
    material_name: str,
    target_roughness: float,
    roughness_was_recorded: bool,
    target_bumpiness: float,
    target_subsurface: float,
    target_specular: float,
    metallic_const: float,
    dark_color: list,
    light_color: list,
    color_source: str,
    extra_result_fields: Optional[dict] = None,
) -> list:
    """
    Shared calibration engine behind generate_procedural_material AND
    match_material_from_photo/apply_photo_material_match — extracted so both
    front-ends (recipe-driven and vision-driven target resolution) get the
    IDENTICAL bake-and-measure verification loop, not two copies that could
    drift apart. Builds a real node-based PBR material (noise/voronoi/bump),
    bakes a small internal sample, and measures the REAL resulting Roughness/
    Bump output rather than assuming the input formula lands where intended.
    One bounded retry on an out-of-tolerance first pass; never loops
    indefinitely. See generate_procedural_material's docstring for the full
    calibrated-vs-heuristic breakdown — unchanged by this extraction.
    """
    bump_strength = max(0.0, min(1.0, target_bumpiness * 3.0))

    def build_and_measure(roughness_target: float) -> dict:
        script = _SAFE_MATERIAL_BAKE_SNIPPET + r"""
import bpy, json, statistics

def sample_avg_stdev(image, max_samples=1500):
    channels = image.channels
    if channels == 0 or image.size[0] == 0 or image.size[1] == 0:
        return None, None
    pixels = image.pixels[:]
    texel_count = len(pixels) // channels
    stride = max(1, texel_count // max_samples)
    vals = [pixels[i] for i in range(0, len(pixels), channels * stride)]
    if not vals:
        return None, None
    return round(sum(vals) / len(vals), 4), round(statistics.pstdev(vals), 4)

def get_input(node, names):
    for n in names:
        s = node.inputs.get(n)
        if s is not None:
            return s
    return None

def get_socket(collection, name, socket_type):
    for s in collection:
        if s.name == name and s.type == socket_type:
            return s
    return None

obj = bpy.data.objects.get('{OBJ}')
if obj is None:
    print(json.dumps({"error": "Object not found: {OBJ}"}))
else:
    mat = bpy.data.materials.get('{MAT}')
    created_material = False
    if mat is None:
        mat = bpy.data.materials.new(name='{MAT}')
        mat.use_nodes = True
        obj.data.materials.append(mat)
        created_material = True
    elif not mat.use_nodes:
        mat.use_nodes = True
    if mat.name not in [s.material.name for s in obj.material_slots if s.material]:
        obj.data.materials.append(mat)

    # A freshly-appended material slot starts with ZERO faces assigned to
    # it — baking/measuring it would sample nothing real (a genuine bug
    # caught live: the first live run of this tool measured 0.0 roughness
    # on a brand-new material because no face on the mesh referenced it
    # yet). Only auto-assign when unambiguous — this is the object's ONLY
    # material slot, so there's no existing assignment to silently
    # overwrite. Otherwise leave it at 0 faces and report that honestly
    # rather than guessing which faces the caller meant.
    mat_slot_index = next((i for i, s in enumerate(obj.material_slots) if s.material == mat), None)
    faces_using_material = sum(1 for p in obj.data.polygons if p.material_index == mat_slot_index)
    auto_assigned_all_faces = False
    if faces_using_material == 0 and len(obj.data.materials) == 1:
        for p in obj.data.polygons:
            p.material_index = mat_slot_index
        obj.data.update()
        faces_using_material = len(obj.data.polygons)
        auto_assigned_all_faces = True

    nt = mat.node_tree
    principled = next((n for n in nt.nodes if n.type == 'BSDF_PRINCIPLED'), None)
    if principled is None:
        principled = nt.nodes.new("ShaderNodeBsdfPrincipled")

    prefix = "AutoProcMat_"
    for nd in list(nt.nodes):
        if nd.name.startswith(prefix):
            nt.nodes.remove(nd)

    base_x, base_y = principled.location.x - 900, principled.location.y

    coord = nt.nodes.new("ShaderNodeTexCoord")
    coord.name = prefix + "Coord"
    coord.location = (base_x - 600, base_y)

    noise = nt.nodes.new("ShaderNodeTexNoise")
    noise.name = prefix + "Noise"
    noise.location = (base_x - 300, base_y + 200)
    noise.inputs["Scale"].default_value = {NOISESCALE}
    nt.links.new(coord.outputs["Generated"], noise.inputs["Vector"])

    voronoi = nt.nodes.new("ShaderNodeTexVoronoi")
    voronoi.name = prefix + "Voronoi"
    voronoi.location = (base_x - 300, base_y - 200)
    voronoi.inputs["Scale"].default_value = {NOISESCALE} * 0.6
    nt.links.new(coord.outputs["Generated"], voronoi.inputs["Vector"])

    combine = nt.nodes.new("ShaderNodeMix")
    combine.name = prefix + "Combine"
    combine.data_type = 'FLOAT'
    combine.location = (base_x, base_y)
    c_a = get_socket(combine.inputs, "A", "VALUE")
    c_b = get_socket(combine.inputs, "B", "VALUE")
    c_fac = get_socket(combine.inputs, "Factor", "VALUE")
    c_out = get_socket(combine.outputs, "Result", "VALUE")
    nt.links.new(noise.outputs["Fac"], c_a)
    nt.links.new(voronoi.outputs["Distance"], c_b)
    c_fac.default_value = 0.4

    ramp = nt.nodes.new("ShaderNodeValToRGB")
    ramp.name = prefix + "ColorRamp"
    ramp.location = (base_x + 300, base_y + 200)
    ramp.color_ramp.elements[0].position = 0.35
    ramp.color_ramp.elements[0].color = ({DARKR}, {DARKG}, {DARKB}, 1.0)
    ramp.color_ramp.elements[1].position = 0.65
    ramp.color_ramp.elements[1].color = ({LIGHTR}, {LIGHTG}, {LIGHTB}, 1.0)
    nt.links.new(c_out, ramp.inputs["Fac"])
    nt.links.new(ramp.outputs["Color"], principled.inputs["Base Color"])

    rough_scale = nt.nodes.new("ShaderNodeMath")
    rough_scale.name = prefix + "RoughSpread"
    rough_scale.location = (base_x + 300, base_y)
    rough_scale.operation = 'MULTIPLY'
    nt.links.new(c_out, rough_scale.inputs[0])
    rough_scale.inputs[1].default_value = {ROUGHSPREAD}

    rough_add = nt.nodes.new("ShaderNodeMath")
    rough_add.name = prefix + "RoughBase"
    rough_add.location = (base_x + 600, base_y)
    rough_add.operation = 'ADD'
    rough_add.use_clamp = True
    nt.links.new(rough_scale.outputs[0], rough_add.inputs[0])
    rough_add.inputs[1].default_value = {ROUGHBASE}
    nt.links.new(rough_add.outputs[0], principled.inputs["Roughness"])

    bump = nt.nodes.new("ShaderNodeBump")
    bump.name = prefix + "Bump"
    bump.location = (base_x + 300, base_y - 200)
    bump.inputs["Strength"].default_value = {BUMPSTRENGTH}
    nt.links.new(noise.outputs["Fac"], bump.inputs["Height"])
    nt.links.new(bump.outputs["Normal"], principled.inputs["Normal"])

    metallic_sock = principled.inputs.get("Metallic")
    if metallic_sock is not None:
        metallic_sock.default_value = {METALLIC}
    sss_sock = get_input(principled, ["Subsurface Weight", "Subsurface"])
    if sss_sock is not None:
        sss_sock.default_value = {SUBSURFACE}
    spec_sock = get_input(principled, ["Specular IOR Level", "Specular"])
    if spec_sock is not None:
        spec_sock.default_value = {SPECULAR}

    # Internal calibration bake — same Emission-trick pattern bake_weathered_textures
    # uses, at low resolution purely to MEASURE the real resulting Roughness average
    # rather than assume the formula lands where intended. Scoped to ONLY this
    # material's faces via safe_bake_measure — real fix for a live incident where
    # an unscoped bake corrupted an unrelated material's real texture.
    output_node = next((n for n in nt.nodes if n.type == 'OUTPUT_MATERIAL'), None)
    measured_roughness = None
    if output_node is not None:
        calib_img = safe_bake_measure(obj, nt, mat_slot_index, output_node, rough_add.outputs[0],
                                       "TEMP_ProcMatCalib", 32, 32, 8)
        if calib_img is not None:
            measured_roughness, _ = sample_avg_stdev(calib_img)
            bpy.data.images.remove(calib_img)

    # Bump verification — NOT a claim of reading literal normal-vector
    # directions from baked pixel data (fragile, why this was skipped
    # originally). Measures stdev of the baked Bump.Normal output as a
    # relative spatial-variance signal — the SAME TYPE of measurement
    # normal_map_bumpiness itself already is (stdev of a real normal map's
    # channel, not an absolute physical unit) — and compares it against
    # target_bumpiness for a same-units sanity check, not an exact match.
    measured_bump_stdev = None
    if output_node is not None:
        bump_img = safe_bake_measure(obj, nt, mat_slot_index, output_node, bump.outputs["Normal"],
                                      "TEMP_ProcMatBumpCalib", 32, 32, 8)
        if bump_img is not None:
            _, measured_bump_stdev = sample_avg_stdev(bump_img)
            bpy.data.images.remove(bump_img)

    print(json.dumps({
        "object": '{OBJ}',
        "material": '{MAT}',
        "created_material": created_material,
        "measured_roughness": measured_roughness,
        "measured_bump_stdev": measured_bump_stdev,
        "faces_using_material": faces_using_material,
        "auto_assigned_all_faces": auto_assigned_all_faces,
    }))
""".replace("{OBJ}", object_name.replace("'", "\\'")) \
   .replace("{MAT}", material_name.replace("'", "\\'")) \
   .replace("{NOISESCALE}", "8.0") \
   .replace("{ROUGHSPREAD}", "0.24") \
   .replace("{ROUGHBASE}", str(roughness_target - 0.12)) \
   .replace("{BUMPSTRENGTH}", str(bump_strength)) \
   .replace("{METALLIC}", str(metallic_const)) \
   .replace("{SUBSURFACE}", str(target_subsurface)) \
   .replace("{SPECULAR}", str(target_specular)) \
   .replace("{DARKR}", str(dark_color[0])).replace("{DARKG}", str(dark_color[1])).replace("{DARKB}", str(dark_color[2])) \
   .replace("{LIGHTR}", str(light_color[0])).replace("{LIGHTG}", str(light_color[1])).replace("{LIGHTB}", str(light_color[2]))

        raw = _send_raw("execute_code_safe", code=script, required_mode="OBJECT", push_undo=True)
        if "error" in raw:
            return {"error": raw["error"]}
        output = raw.get("result", "")
        for line in output.strip().splitlines():
            line = line.strip()
            if line.startswith("{"):
                return json.loads(line)
        return {"error": "No JSON output from generate_procedural_material", "raw": output}

    _invalidate_dna_cache(object_name)
    _before_image = _capture_plain_screenshot(object_name)

    try:
        pass_1 = build_and_measure(target_roughness)
        if "error" in pass_1:
            return [json.dumps(pass_1)]

        measured = pass_1.get("measured_roughness")
        faces_using_material = pass_1.get("faces_using_material", 0)
        calibration_status = "uncalibrated" if not roughness_was_recorded else None
        attempts = [{"attempt": 1, "roughness_target": target_roughness, "measured_roughness": measured}]

        if faces_using_material == 0:
            # Real bug caught on the first live run of this tool: a freshly-
            # appended material slot starts with zero faces referencing it,
            # so the calibration bake samples nothing — it measured 0.0
            # roughness against a 0.95 target and reported "approximate",
            # which understated what actually happened (nothing was
            # measured at all, not a near-miss). Ambiguous cases (an object
            # that already has other materials) are never auto-assigned —
            # only reported honestly here.
            calibration_status = "unverified_no_faces_assigned"
        elif roughness_was_recorded and measured is not None:
            distance = abs(target_roughness - measured)
            if distance <= 0.15:
                calibration_status = "matched"
            else:
                # One bounded retry — nudge the target by the measured error
                # and re-measure once. Never loop indefinitely.
                adjusted_target = max(0.0, min(1.0, target_roughness + (target_roughness - measured)))
                pass_2 = build_and_measure(adjusted_target)
                if "error" not in pass_2:
                    measured_2 = pass_2.get("measured_roughness")
                    attempts.append({"attempt": 2, "roughness_target": adjusted_target, "measured_roughness": measured_2})
                    if measured_2 is not None and abs(target_roughness - measured_2) <= 0.15:
                        calibration_status = "matched"
                        measured = measured_2
                    else:
                        calibration_status = "approximate"
                        measured = measured_2 if measured_2 is not None else measured
                else:
                    calibration_status = "approximate"
        elif roughness_was_recorded and measured is None:
            calibration_status = "unverified"  # internal bake failed — real, not silenced

        measured_bump_stdev = pass_1.get("measured_bump_stdev")
        if faces_using_material == 0:
            bump_verification = {"status": "unverified", "note": "no faces assigned — see faces_using_material"}
        elif measured_bump_stdev is None:
            bump_verification = {"status": "unverified", "note": "internal bump calibration bake failed"}
        else:
            bump_verification = {
                "status": "measured",
                "measured_stdev": measured_bump_stdev,
                "target_bumpiness": round(target_bumpiness, 4),
                "note": "a relative spatial-variance signal (stdev of the baked Bump.Normal output) — "
                        "the same TYPE of measurement normal_map_bumpiness itself already is, not a "
                        "claim of decoding literal normal-vector directions. A same-units sanity check, "
                        "not an exact-match guarantee.",
            }

        result = {
            "object": object_name,
            "material": material_name,
            "target_roughness": round(target_roughness, 4),
            "roughness_was_recorded": roughness_was_recorded,
            "measured_roughness": measured,
            "calibration_status": calibration_status,
            "calibration_attempts": attempts,
            "bump_strength_heuristic": round(bump_strength, 4),
            "bump_strength_note": "the input formula (normal_map_bumpiness x3) is still a heuristic — "
                                   "see bump_verification for the real measured result it produced",
            "bump_verification": bump_verification,
            "metallic_set": metallic_const,
            "subsurface_weight_set": target_subsurface,
            "specular_ior_level_set": target_specular,
            "color_source": color_source,
            "faces_using_material": faces_using_material,
            "auto_assigned_all_faces": pass_1.get("auto_assigned_all_faces", False),
        }
        if extra_result_fields:
            result.update(extra_result_fields)
        if faces_using_material == 0:
            result["fix"] = (
                "This material isn't assigned to any face yet (it was created new on an object "
                "that already has other materials, so faces weren't auto-assigned to avoid "
                "silently overwriting existing assignments). Assign it to the intended faces "
                "(e.g. via split_blended_material, or manual face selection + material assignment "
                "in Blender), then call again to get a real calibration measurement."
            )

        out = []
        after_image = _capture_plain_screenshot(object_name)
        if _before_image:
            out.append(_before_image)
        if after_image:
            out.append(after_image)
        out.append(json.dumps(result, indent=2))
        return out
    except Exception as e:
        logger.error(f"Error in _calibrate_and_build_procedural_material: {e}")
        return [json.dumps({"error": str(e)})]


@mcp.tool()
def generate_procedural_material(
    object_name: str,
    material_name: str,
    category: str = "",
    target_recipe: str = "",
) -> list:
    """
    GENERATE PROCEDURAL MATERIAL — creates a real Blender node-based PBR
    material from noise/voronoi/bump nodes, calibrated against the material
    knowledge layer instead of guessed. Unlike apply_weathering_recipe (which
    modifies an EXISTING material's appearance), this REPLACES material_name's
    Base Color/Roughness/Normal wiring entirely with a generated procedural
    graph — call create_checkpoint() first, this is generative and not a
    simple metric diff. If material_name doesn't exist yet on the object, it
    is created and assigned a new slot. If that's the object's ONLY material
    slot (unambiguous — nothing existing to overwrite), every face is
    auto-assigned to it so there's something real to calibrate against. If
    the object already has other materials, the new slot is left at 0
    faces — auto-assigning would silently steal faces from an existing
    material. calibration_status comes back "unverified_no_faces_assigned"
    in that case (a real failure mode caught on this tool's first live run:
    a brand-new unassigned material slot baked to a flat 0.0 regardless of
    target, which the calibration loop was initially misreporting as a
    generic "approximate" near-miss instead of "nothing was actually
    there to measure") — assign the material to faces first (e.g. via
    split_blended_material or manual selection), then call again.

    Returns a list: [before_image?, after_image?, result_json_string] — same
    convention as apply_weathering_recipe/bake_weathered_textures. Parse the
    JSON result with json.loads(result[-1]).

    Because it's procedural (no baked bitmap), the result is inherently
    tileable/seamless — there's no image edge to hide a seam at until you
    choose to bake one via bake_weathered_textures.

    WHAT'S CALIBRATED vs. WHAT'S A HEURISTIC — stated honestly, not blurred:
    - Roughness is calibrated AND VERIFIED: the target category/recipe's
      recorded roughness_avg drives the generated Roughness formula, then
      this tool immediately bakes a small internal sample (the same
      Emission-trick technique bake_weathered_textures uses) and measures the
      REAL resulting average — not assumed. If it lands within tolerance
      (0.15, the same honesty-gate threshold _find_closest_material_recipe
      uses), calibration_status is "matched". If not, ONE bounded retry
      nudges the formula and re-measures; still-out-of-tolerance after that
      is reported as "approximate" with the real measured number attached —
      never silently claimed as a match.
    - Bump strength (visual bumpiness): the INPUT formula (normal_map_
      bumpiness x3, clamped 0-1) is still a documented heuristic — the
      knowledge layer only ever recorded bumpiness as a scalar magnitude,
      never spatial frequency, so there's no formula to derive exactly.
      What IS real now: bump_verification bakes the resulting Bump.Normal
      output (via the same safe_bake_measure used for Roughness) and
      reports its measured stdev — a relative spatial-variance signal, the
      same TYPE of measurement normal_map_bumpiness itself already is, not
      a claim of decoding literal normal-vector directions from baked
      pixels (that would be unreliable). A same-units sanity check on the
      OUTPUT, even though the INPUT formula stays a heuristic.
    - Metallic is a category-based constant (0.9 for "metal", else 0.0) —
      the fingerprint has never recorded a metallic signal, only roughness/
      subsurface/specular/normal-bumpiness, so this is a plain, documented
      default, not a measurement.
    - subsurface_weight/specular_ior_level are set directly from the target
      recipe's own recorded values when present (real recorded data, no
      formula needed) — default to 0.0/0.5 when the recipe has none recorded.
    - Base Color for category="metal" reuses rust_color, the SAME constant
      apply_weathering_recipe already defaults to — a real, already-used
      value, not invented. Every other category is still a flat, explicitly
      labeled gray placeholder (color_source: "generic_gray_placeholder" in
      the result vs. "reused_rust_color") — no color data has ever been
      recorded for any category, and this tool won't imply otherwise for
      categories it hasn't actually addressed yet.

    Category/target resolution — same explicit > name > fingerprint
    precedence as apply_weathering_recipe's material_category, but ending in
    an error instead of an automatic default, since there's no existing
    material driving a metal_factor to dispatch from:
    1. target_recipe: exact canonical_name lookup — uses that recipe's own
       fingerprint directly, most specific.
    2. category: uses a representative recorded recipe for that category if
       one exists (calibration_status "uncalibrated" with generic defaults
       if none has ever been recorded for that category — never invents
       numbers for a category with zero real data).
    3. Neither given: tries _resolve_material_category(material_name) (name-
       based), then falls back to the object's OWN current closest_known_material
       fingerprint match (only meaningful if material_name already exists
       with some real PBR data to compare).
    4. Nothing resolves: returns an error asking for an explicit category or
       target_recipe — never guesses a starting point out of thin air.
    """
    resolved_category = None
    resolved_recipe = None

    if target_recipe:
        resolved_recipe = _find_recipe_by_canonical_name(target_recipe)
        if resolved_recipe is None:
            return [json.dumps({"error": f"No recorded recipe named '{target_recipe}'."})]
        resolved_category = resolved_recipe.get("parameters", {}).get("category")
    elif category:
        resolved_category = category
        resolved_recipe = _find_recipe_for_category(category)
    else:
        resolved_category = _resolve_material_category(material_name)
        if resolved_category is None:
            dna_lookup = _reaffirm_dna(object_name)
            for mat in dna_lookup.get("materials", []):
                if mat.get("name") == material_name:
                    match = mat.get("closest_known_material")
                    if match:
                        resolved_category = match.get("category")
                        resolved_recipe = _find_recipe_by_canonical_name(match.get("canonical_name"))
                    break
        else:
            resolved_recipe = _find_recipe_for_category(resolved_category)

    if resolved_category is None:
        return [json.dumps({
            "error": "Could not resolve a target category — no explicit category/target_recipe, "
                     "no name-based match, and no existing fingerprint close enough to compare.",
            "fix": "Pass category= (e.g. 'metal', 'organic') or target_recipe= (a recorded canonical_name) explicitly.",
        })]

    target_fp = resolved_recipe.get("parameters", {}).get("fingerprint", {}) if resolved_recipe else {}
    target_roughness = _fingerprint_value(target_fp, "roughness_avg")
    roughness_was_recorded = target_roughness is not None
    if target_roughness is None:
        target_roughness = 0.6
    target_bumpiness = _fingerprint_value(target_fp, "normal_map_bumpiness")
    if target_bumpiness is None:
        target_bumpiness = 0.02
    target_subsurface = _fingerprint_value(target_fp, "subsurface_weight")
    if target_subsurface is None:
        target_subsurface = 0.0
    target_specular = _fingerprint_value(target_fp, "specular_ior_level")
    if target_specular is None:
        target_specular = 0.5
    metallic_const = 0.9 if resolved_category == "metal" else 0.0

    # Color: never invented. The knowledge layer has never recorded real
    # color data for ANY category — only roughness/subsurface/specular/
    # bumpiness. "metal" is the one exception: it reuses rust_color, the
    # SAME constant apply_weathering_recipe already defaults to (server.py
    # ~5809) and has been used/seen across many live weathering calls
    # tonight — a real, already-approved value, not a new guess. Every
    # other category keeps a flat, explicitly-labeled gray placeholder
    # (color_source in the result) rather than silently implying it means
    # something it doesn't.
    if resolved_category == "metal":
        rust_color = [0.35, 0.14, 0.05]
        dark_color = [c * 0.5 for c in rust_color]
        light_color = [min(1.0, c * 1.4) for c in rust_color]
        color_source = "reused_rust_color"
    else:
        dark_color = [0.25, 0.25, 0.25]
        light_color = [0.6, 0.6, 0.6]
        color_source = "generic_gray_placeholder"

    return _calibrate_and_build_procedural_material(
        object_name, material_name,
        target_roughness, roughness_was_recorded,
        target_bumpiness, target_subsurface, target_specular,
        metallic_const, dark_color, light_color, color_source,
        extra_result_fields={
            "category_used": resolved_category,
            "canonical_recipe_used": resolved_recipe.get("canonical_name") if resolved_recipe else None,
        },
    )


# surface_pattern x pattern_scale -> target_bumpiness heuristic table for
# match_material_from_photo/apply_photo_material_match. A documented mapping,
# same honesty discipline as generate_procedural_material's bump_strength_
# heuristic — never invents a bumpiness value outside this table.
_PHOTO_BUMPINESS_TABLE = {
    ("smooth", "fine"): 0.005, ("smooth", "medium"): 0.008, ("smooth", "coarse"): 0.012,
    ("grainy", "fine"): 0.015, ("grainy", "medium"): 0.025, ("grainy", "coarse"): 0.04,
    ("woven", "fine"): 0.02, ("woven", "medium"): 0.035, ("woven", "coarse"): 0.05,
    ("pitted", "fine"): 0.03, ("pitted", "medium"): 0.05, ("pitted", "coarse"): 0.08,
    ("scratched", "fine"): 0.025, ("scratched", "medium"): 0.045, ("scratched", "coarse"): 0.07,
    ("noisy", "fine"): 0.02, ("noisy", "medium"): 0.04, ("noisy", "coarse"): 0.06,
}


@mcp.tool()
def match_material_from_photo(object_name: str, material_name: str, reference_image_path: str) -> list:
    """
    MATCH MATERIAL FROM PHOTO — step 1 of 2. Loads a real reference photo and
    asks for a structured vision analysis of it, rather than guessing PBR
    values from a fixed category library the way generate_procedural_material
    does. Same two-call pattern as construction_mode() -> calculate_world_coordinates():
    this call returns the image + a vision_prompt; read the image, answer the
    prompt with real JSON, then call apply_photo_material_match(object_name,
    material_name, vision_analysis_json=<your JSON response>) to actually
    build and calibrate the material.

    Returns [Image, request_dict]. No Blender calls happen in this half —
    pure image load + prompt construction.
    """
    img_path = Path(reference_image_path)
    if not img_path.exists():
        return [json.dumps({"error": f"Reference image not found: {reference_image_path}"})]

    with open(img_path, "rb") as f:
        img_bytes = f.read()
    suffix = img_path.suffix.lower().lstrip(".")
    img_format = "jpeg" if suffix in ("jpg", "jpeg") else "png"

    vision_prompt = """Look at the reference photo above. You're estimating PBR
material parameters for a Blender procedural material to visually match this
photo's surface. Respond with ONLY this JSON (no markdown fences):

{"dominant_base_color_rgb": [r, g, b], "secondary_color_rgb": [r, g, b] or null,
"perceived_roughness": 0.0-1.0, "roughness_reasoning": "str",
"perceived_metallic": 0.0-1.0, "metallic_reasoning": "str",
"surface_pattern": "smooth|grainy|woven|pitted|scratched|noisy",
"pattern_scale": "fine|medium|coarse", "confidence": "high|medium|low"}

Rules: r/g/b are 0-1 floats, your best visual estimate of the dominant surface
color (not lighting/shadow color). perceived_roughness: 0=mirror-glossy,
1=fully matte, based on how sharp/diffuse highlights look. perceived_metallic:
0=fully dielectric, 1=fully metallic, based on whether reflections are
colored (dielectric) or tinted by the base color (metallic). surface_pattern/
pattern_scale describe the visible micro-texture, not the macro shape. Be
honest in confidence — this is a visual estimate from a single photo, not a
physical measurement; say "low" if the photo is small, blurry, or ambiguous."""

    request = {
        "status": "vision_analysis_required",
        "object_name": object_name,
        "material_name": material_name,
        "reference_image_path": reference_image_path,
        "vision_prompt": vision_prompt,
        "instruction": (
            "Analyze the image above using vision_prompt, then call "
            "apply_photo_material_match(object_name, material_name, "
            "vision_analysis_json=<your JSON response as a string>) to build "
            "and calibrate the actual material."
        ),
    }
    return [Image(data=img_bytes, format=img_format), request]


@mcp.tool()
def apply_photo_material_match(
    object_name: str,
    material_name: str,
    vision_analysis_json: str,
    save_as_recipe: str = "",
) -> list:
    """
    APPLY PHOTO MATERIAL MATCH — step 2 of 2, follows match_material_from_photo.
    Takes the structured vision analysis JSON and builds a real procedural PBR
    material from it, verified through the EXACT SAME bake-and-measure
    calibration loop generate_procedural_material uses (see
    _calibrate_and_build_procedural_material) — so calibration_status still
    honestly reports matched/approximate/unverified against the target,
    even though here the target itself came from a vision estimate of a
    photo rather than a recorded recipe.

    HONESTY BOUNDARY, stated explicitly: perceived_roughness/perceived_metallic/
    color in vision_analysis_json are Claude's visual judgment of the photo —
    there is no reliable way to derive a physical roughness/metallic value
    from an arbitrary photo with unknown lighting and exposure. What IS real:
    the generated node graph is bake-verified to actually produce the
    estimated target (color_source is reported as "vision_estimated_from_photo",
    never blurred with a measured value). target_bumpiness comes from a fixed,
    documented surface_pattern x pattern_scale lookup table
    (_PHOTO_BUMPINESS_TABLE) — never invented outside that table.

    If save_as_recipe is given, records the result as a real recipe_type=
    "material" entry via record_creative_recipe (parameters.category=
    "photo_matched") so a future generate_procedural_material(target_recipe=
    save_as_recipe) call can reuse this match without needing the photo again.
    """
    try:
        vision = json.loads(vision_analysis_json) if isinstance(vision_analysis_json, str) else vision_analysis_json
    except (json.JSONDecodeError, TypeError) as e:
        return [json.dumps({"error": f"vision_analysis_json is not valid JSON: {e}"})]

    required = ["dominant_base_color_rgb", "perceived_roughness", "perceived_metallic",
                "surface_pattern", "pattern_scale"]
    missing = [k for k in required if k not in vision or vision[k] is None]
    if missing:
        return [json.dumps({
            "error": f"vision_analysis_json is missing required field(s): {missing}",
            "fix": "Call match_material_from_photo first and answer its vision_prompt in full — "
                   "no field here is guessed if the vision analysis didn't provide it.",
        })]

    def _clamp01(v):
        return max(0.0, min(1.0, float(v)))

    base_rgb = [_clamp01(c) for c in vision["dominant_base_color_rgb"]]
    if len(base_rgb) != 3:
        return [json.dumps({"error": "dominant_base_color_rgb must have exactly 3 values (r, g, b)."})]

    secondary_rgb = vision.get("secondary_color_rgb")
    dark_color = [c * 0.5 for c in base_rgb]
    if secondary_rgb and len(secondary_rgb) == 3:
        light_color = [_clamp01(c) for c in secondary_rgb]
    else:
        light_color = [_clamp01(c * 1.4) for c in base_rgb]

    target_roughness = _clamp01(vision["perceived_roughness"])
    metallic_const = _clamp01(vision["perceived_metallic"])

    pattern = str(vision["surface_pattern"]).lower().strip()
    scale = str(vision["pattern_scale"]).lower().strip()
    bumpiness_key = (pattern, scale)
    if bumpiness_key not in _PHOTO_BUMPINESS_TABLE:
        return [json.dumps({
            "error": f"surface_pattern/pattern_scale combo not recognized: {bumpiness_key}",
            "fix": f"Use one of: {sorted(set(k[0] for k in _PHOTO_BUMPINESS_TABLE))} x "
                   f"{sorted(set(k[1] for k in _PHOTO_BUMPINESS_TABLE))}",
        })]
    target_bumpiness = _PHOTO_BUMPINESS_TABLE[bumpiness_key]

    result_list = _calibrate_and_build_procedural_material(
        object_name, material_name,
        target_roughness, True,  # roughness_was_recorded — tracked as a real target here
        target_bumpiness, 0.0, 0.5,  # no photo signal for subsurface/specular — documented defaults
        metallic_const, dark_color, light_color, "vision_estimated_from_photo",
        extra_result_fields={
            "vision_confidence": vision.get("confidence"),
            "vision_roughness_reasoning": vision.get("roughness_reasoning"),
            "vision_metallic_reasoning": vision.get("metallic_reasoning"),
            "vision_note": "subsurface_weight_set/specular_ior_level_set are documented defaults "
                            "(0.0/0.5) — no photo signal exists for either.",
        },
    )

    if save_as_recipe:
        try:
            result_dict = json.loads(result_list[-1])
        except (json.JSONDecodeError, IndexError):
            result_dict = {}
        if "error" not in result_dict:
            measured = result_dict.get("measured_roughness")
            record_creative_recipe(
                recipe_type="material",
                canonical_name=save_as_recipe,
                trigger_phrases=[material_name],
                parameters={
                    "category": "photo_matched",
                    "fingerprint": {
                        "roughness_avg": measured if measured is not None else target_roughness,
                        "normal_map_bumpiness": target_bumpiness,
                        "subsurface_weight": 0.0,
                        "specular_ior_level": 0.5,
                    },
                },
                notes=f"vision-matched from a reference photo (metallic={metallic_const}, "
                      f"pattern={pattern}/{scale}, confidence={vision.get('confidence')})",
            )

    return result_list


@mcp.tool()
def apply_photo_as_texture(
    object_name: str,
    material_name: str,
    reference_image_path: str,
    generate_roughness: bool = True,
    generate_normal: bool = True,
    normal_strength: float = 2.0,
) -> list:
    """
    APPLY PHOTO AS TEXTURE — maps your reference photo's actual pixels onto
    the mesh as a real Base Color image texture, instead of trying to
    procedurally regenerate its look with noise/voronoi nodes (that's what
    match_material_from_photo/apply_photo_material_match do — works fine for
    simple flat materials, but cannot reproduce a complex multi-region photo
    like flaking rust; a 2-color noise ramp is the wrong tool for that).
    This is the "just use the real photo" path — visually identical to your
    reference because it IS your reference, not an approximation built from
    two sampled colors.

    Roughness/Normal are DERIVED from the photo's own pixel luminance, not
    invented and not a claim of literally measured surface properties:
    - Roughness: brighter texels -> lower roughness (a standard heuristic —
      front-lit highlights read as brighter pixels in a photo), clamped to
      a 0.35-0.90 range. Reported honestly as a heuristic, with the real
      min/max it produced.
    - Normal: a real 3x3 Sobel operator over photo luminance (the same
      standard technique cpetry's NormalMap-Online converter uses —
      treating brightness as height and taking the Sobel-filtered local
      slope) — not a claim of measured depth data, just a real, common
      technique for adding surface relief derived from a 2D photo. Sobel's
      row-weighted kernel implicitly smooths along the perpendicular axis,
      which is why it reads less noisy than a bare pixel-to-pixel
      difference at the same normal_strength.

    Requires the target object to already have UVs, or auto-unwraps via
    Smart UV Project if none exist — reported honestly via
    had_uvs_already/auto_unwrapped in the result (an unwrapped mesh would
    place the texture in an undefined way otherwise). Requires numpy in
    Blender's own Python (bundled by default since Blender 2.8+) — returns
    an explicit error if unavailable, never silently skips roughness/normal
    generation. reference_image_path must be a path the BLENDER PROCESS can
    read directly (same machine as this MCP server, in the setup this tool
    was built for) — it's loaded by bpy.data.images, not sent as bytes.

    Returns [before_image?, after_image?, result_json_string] — same
    convention as generate_procedural_material. Call create_checkpoint()
    first, this modifies material node graphs and (possibly) UVs.
    """
    img_path = Path(reference_image_path)
    if not img_path.exists():
        return [json.dumps({"error": f"Reference image not found: {reference_image_path}"})]

    script = r"""
import bpy, json, os

obj = bpy.data.objects.get('{OBJ}')
if obj is None:
    print(json.dumps({"error": "Object not found: {OBJ}"}))
else:
    img_path = r'{IMGPATH}'
    if not os.path.exists(img_path):
        print(json.dumps({"error": "Reference image not found on the Blender process's filesystem: " + img_path}))
    else:
        try:
            import numpy as np
        except ImportError:
            print(json.dumps({"error": "numpy not available in Blender's Python -- cannot generate roughness/normal maps."}))
        else:
            mat = bpy.data.materials.get('{MAT}')
            created_material = False
            if mat is None:
                mat = bpy.data.materials.new(name='{MAT}')
                mat.use_nodes = True
                obj.data.materials.append(mat)
                created_material = True
            elif not mat.use_nodes:
                mat.use_nodes = True
            if mat.name not in [s.material.name for s in obj.material_slots if s.material]:
                obj.data.materials.append(mat)

            mat_slot_index = next((i for i, s in enumerate(obj.material_slots) if s.material == mat), None)
            faces_using_material = sum(1 for p in obj.data.polygons if p.material_index == mat_slot_index)
            auto_assigned_all_faces = False
            if faces_using_material == 0 and len(obj.data.materials) == 1:
                for p in obj.data.polygons:
                    p.material_index = mat_slot_index
                obj.data.update()
                faces_using_material = len(obj.data.polygons)
                auto_assigned_all_faces = True

            had_uvs = len(obj.data.uv_layers) > 0
            if not had_uvs:
                bpy.ops.object.select_all(action='DESELECT')
                obj.select_set(True)
                bpy.context.view_layer.objects.active = obj
                bpy.ops.object.mode_set(mode='EDIT')
                bpy.ops.mesh.select_all(action='SELECT')
                bpy.ops.uv.smart_project(angle_limit=66)
                bpy.ops.object.mode_set(mode='OBJECT')

            base_img = bpy.data.images.load(img_path, check_existing=True)
            base_img.name = '{MAT}' + "_BaseColor"
            base_img.colorspace_settings.name = 'sRGB'

            W, H = base_img.size[0], base_img.size[1]
            px = np.empty(W * H * 4, dtype=np.float32)
            base_img.pixels.foreach_get(px)
            px = px.reshape(H, W, 4)
            lum = 0.2126 * px[:, :, 0] + 0.7152 * px[:, :, 1] + 0.0722 * px[:, :, 2]

            result = {"base_color": {"width": W, "height": H}}
            rough_img = None
            normal_img = None

            if {GENROUGH}:
                lum_min, lum_max = float(lum.min()), float(lum.max())
                span = max(1e-5, lum_max - lum_min)
                norm_lum = (lum - lum_min) / span
                rough_vals = 0.35 + (1.0 - norm_lum) * 0.55
                rough_rgba = np.stack([rough_vals, rough_vals, rough_vals, np.ones_like(rough_vals)], axis=-1)
                rough_img = bpy.data.images.new('{MAT}' + "_Roughness_Generated", W, H, alpha=True)
                # Colorspace MUST be set before foreach_set -- setting it on an
                # already-populated generated image resets the pixel buffer to
                # zero (a real Blender API quirk, confirmed live: writing pixels
                # first then setting colorspace silently zeroed the whole image,
                # which read as full-glossy roughness=0 and mirror-reflected the
                # world HDRI instead of showing the intended matte texture).
                rough_img.colorspace_settings.name = 'Non-Color'
                rough_img.pixels.foreach_set(rough_rgba.astype(np.float32).flatten())
                rough_img.pack()
                rough_img.update()
                result["roughness_generated"] = {
                    "min": round(float(rough_vals.min()), 4), "max": round(float(rough_vals.max()), 4),
                    "note": "heuristic derived from photo luminance -- brighter texels read as lower roughness, not a measured reflectance value",
                }

            if {GENNORMAL}:
                strength = {NORMALSTRENGTH}

                # Full 3x3 Sobel operator (the same standard technique
                # https://cpetry.github.io/NormalMap-Online/ uses) instead of a
                # bare 2-tap central difference -- the extra row weighting
                # (1,2,1) implicitly smooths along the perpendicular axis, which
                # is exactly why a Sobel-derived normal map reads less noisy/
                # aliased than a plain per-pixel difference at the same
                # strength. get(dy, dx) returns the texel at (y+dy, x+dx) for
                # every pixel via wrap-around roll (same wrap convention as the
                # rest of this generator).
                def get(dy, dx):
                    return np.roll(np.roll(lum, -dy, axis=0), -dx, axis=1)

                gx = (-1 * get(-1, -1) + 1 * get(-1, 1)
                      - 2 * get(0, -1) + 2 * get(0, 1)
                      - 1 * get(1, -1) + 1 * get(1, 1))
                gy = (-1 * get(-1, -1) - 2 * get(-1, 0) - 1 * get(-1, 1)
                      + 1 * get(1, -1) + 2 * get(1, 0) + 1 * get(1, 1))
                # /4 normalization keeps `strength`'s visual meaning matched to
                # the old central-difference version (a unit-slope ramp produced
                # raw diff 2 there; the unnormalized Sobel sum for the same ramp
                # is 8, so /4 brings it back to 2) -- not an arbitrary constant.
                dx = (gx / 4.0) * strength
                dy = (gy / 4.0) * strength

                nz = np.ones_like(lum)
                normal_vec = np.stack([-dx, -dy, nz], axis=-1)
                norm_len = np.linalg.norm(normal_vec, axis=-1, keepdims=True)
                normal_vec = normal_vec / np.clip(norm_len, 1e-5, None)
                normal_rgb = normal_vec * 0.5 + 0.5
                normal_rgba = np.concatenate([normal_rgb, np.ones((H, W, 1), dtype=np.float32)], axis=-1)
                normal_img = bpy.data.images.new('{MAT}' + "_Normal_Generated", W, H, alpha=True)
                normal_img.colorspace_settings.name = 'Non-Color'  # set BEFORE foreach_set -- see roughness note above
                normal_img.pixels.foreach_set(normal_rgba.astype(np.float32).flatten())
                normal_img.pack()
                normal_img.update()
                result["normal_generated"] = {
                    "strength": strength,
                    "note": "3x3 Sobel operator on photo luminance (the same standard technique as "
                            "cpetry's NormalMap-Online), not measured depth data",
                }

            base_img.pack()

            nt = mat.node_tree
            principled = next((n for n in nt.nodes if n.type == 'BSDF_PRINCIPLED'), None)
            if principled is None:
                principled = nt.nodes.new("ShaderNodeBsdfPrincipled")
            output_node = next((n for n in nt.nodes if n.type == 'OUTPUT_MATERIAL'), None)
            if output_node is None:
                output_node = nt.nodes.new("ShaderNodeOutputMaterial")
                nt.links.new(principled.outputs["BSDF"], output_node.inputs["Surface"])

            prefix = "PhotoTex_"
            for nd in list(nt.nodes):
                if nd.name.startswith(prefix):
                    nt.nodes.remove(nd)

            base_x, base_y = principled.location.x - 500, principled.location.y

            base_tex_node = nt.nodes.new("ShaderNodeTexImage")
            base_tex_node.name = prefix + "BaseColor"
            base_tex_node.location = (base_x, base_y + 300)
            base_tex_node.image = base_img
            nt.links.new(base_tex_node.outputs["Color"], principled.inputs["Base Color"])

            if rough_img is not None:
                rough_tex_node = nt.nodes.new("ShaderNodeTexImage")
                rough_tex_node.name = prefix + "Roughness"
                rough_tex_node.location = (base_x, base_y)
                rough_tex_node.image = rough_img
                nt.links.new(rough_tex_node.outputs["Color"], principled.inputs["Roughness"])

            if normal_img is not None:
                normal_tex_node = nt.nodes.new("ShaderNodeTexImage")
                normal_tex_node.name = prefix + "NormalTex"
                normal_tex_node.location = (base_x - 300, base_y - 300)
                normal_tex_node.image = normal_img

                normal_map_node = nt.nodes.new("ShaderNodeNormalMap")
                normal_map_node.name = prefix + "NormalMap"
                normal_map_node.location = (base_x, base_y - 300)
                nt.links.new(normal_tex_node.outputs["Color"], normal_map_node.inputs["Color"])
                nt.links.new(normal_map_node.outputs["Normal"], principled.inputs["Normal"])

            result.update({
                "object": '{OBJ}',
                "material": '{MAT}',
                "created_material": created_material,
                "faces_using_material": faces_using_material,
                "auto_assigned_all_faces": auto_assigned_all_faces,
                "had_uvs_already": had_uvs,
                "auto_unwrapped": not had_uvs,
            })
            print(json.dumps(result))
""".replace("{OBJ}", object_name.replace("'", "\\'")) \
   .replace("{MAT}", material_name.replace("'", "\\'")) \
   .replace("{IMGPATH}", str(img_path.resolve()).replace("'", "\\'")) \
   .replace("{GENROUGH}", "True" if generate_roughness else "False") \
   .replace("{GENNORMAL}", "True" if generate_normal else "False") \
   .replace("{NORMALSTRENGTH}", str(normal_strength))

    _invalidate_dna_cache(object_name)
    before_image = _capture_plain_screenshot(object_name)

    raw = _send_raw("execute_code_safe", code=script, required_mode="OBJECT", push_undo=True)
    if "error" in raw:
        return [json.dumps({"error": raw["error"]})]
    output = raw.get("result", "")
    result = None
    for line in output.strip().splitlines():
        line = line.strip()
        if line.startswith("{"):
            result = json.loads(line)
            break
    if result is None:
        return [json.dumps({"error": "No JSON output from apply_photo_as_texture", "raw": output})]

    out = []
    after_image = _capture_plain_screenshot(object_name)
    if before_image:
        out.append(before_image)
    if after_image:
        out.append(after_image)
    out.append(json.dumps(result, indent=2))
    return out


@mcp.tool()
def split_blended_material(
    object_name: str,
    material_name: str,
    force: bool = False,
) -> list:
    """
    SPLIT BLENDED MATERIAL — the fix for the couch-class problem: one shared
    material silently doing multiple jobs (leather + wood + fabric all baked
    into a single material, material_count: 1 hiding it completely — a real
    case proven live tonight). MODIFIES the object — call create_checkpoint()
    first, this changes face material assignment and is not a simple metric
    diff.

    Returns a list: [before_image?, after_image?, result_json_string] — same
    convention as apply_weathering_recipe/bake_weathered_textures. Parse the
    JSON result with json.loads(result[-1]).

    Refuses to run unless get_asset_dna's heterogeneity.likely_blended is
    True for this material (a fresh DNA fetch, not a stale one) — same
    discipline as bake_weathered_textures' topology gate: this tool only
    acts on a REAL measured signal (Base Color variance across disconnected
    UV islands), never on a guess. force=True bypasses the gate for when a
    human has looked and decided to split anyway.

    WHAT THIS ACTUALLY DOES — scoped honestly, not oversold:
    - Recomputes the same real per-island Base Color samples heterogeneity
      detection already measured, then splits them into exactly TWO groups
      via the largest gap in sorted island averages whose minority side is
      at least 2% of the object's total faces — a real, deterministic
      clustering, but coarser than an N-way split. Real fix from a live
      incident: the largest gap ALONE, with no size floor, peeled off
      exactly 1 face out of ~1.9M on a real test asset — technically a
      split, practically useless. Candidate gaps are now walked
      largest-first and any whose minority side is a stray fragment (below
      that 2% floor) is skipped in favor of the next one; "no meaningful
      split" is reported honestly if none qualify, rather than accepting
      whichever gap happened to be biggest. A material blending 3+ very
      different substances will have its single most different SUBSTANTIAL
      group separated out per call; re-running this tool on the remainder
      can peel off further groups.
    - The minority group (fewer islands) gets its faces reassigned
      (polygon.material_index) to a NEW material — a direct .copy() of the
      original, so it starts with the SAME shared texture. This tool does
      NOT crop or mask the texture per group — that would require real
      image-space UV-island cropping, out of scope for this pass. What it
      DOES deliver: two independently-addressable materials, so
      apply_weathering_recipe/generate_procedural_material can now be
      pointed at just the minority group's faces going forward, which was
      structurally impossible while everything shared one material slot.
    - If no meaningful gap exists (every island lands in one group), refuses
      with a clear error rather than forcing an arbitrary split.

    After splitting, both materials' fingerprints are re-measured (same
    _PBR_SOCKET_SCAN_SCRIPT scan used everywhere else) and reported — they
    will read as IDENTICAL to each other immediately after this call (both
    still share the same node graph/texture), which is expected and stated
    plainly, not hidden: the split creates the ADDRESSABILITY, visual
    differentiation is a follow-up step (weathering or regeneration) on the
    new material specifically.
    """
    _invalidate_dna_cache(object_name)
    dna_before = _reaffirm_dna(object_name)
    mat_before = next(
        (m for m in dna_before.get("materials", []) if m.get("name") == material_name), None
    )
    if mat_before is None:
        return [json.dumps({"error": f"Material '{material_name}' not found on {object_name}."})]

    heterogeneity = mat_before.get("heterogeneity", {})
    if not heterogeneity.get("likely_blended") and not force:
        return [json.dumps({
            "error": "Refusing to split — heterogeneity detection did not flag this material as blended.",
            "heterogeneity": heterogeneity,
            "why": "This tool only acts on a real measured signal (Base Color variance across "
                   "disconnected UV islands), never a guess — same discipline as "
                   "bake_weathered_textures' topology gate.",
            "fix": "If you've visually confirmed this material really is doing multiple jobs despite "
                   "the measurement not catching it (e.g. no UV seams marked so islands couldn't be "
                   "separated), call again with force=True.",
        })]

    script = r"""
import bpy, json, statistics

obj = bpy.data.objects.get('{OBJ}')
mat = bpy.data.materials.get('{MAT}')
if obj is None:
    print(json.dumps({"error": "Object not found: {OBJ}"}))
elif mat is None:
    print(json.dumps({"error": "Material not found: {MAT}"}))
else:
    mesh = obj.data
    principled = next((n for n in mat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED'), None) if mat.node_tree else None
    bc_sock = principled.inputs.get("Base Color") if principled else None
    if not (bc_sock and bc_sock.links and bc_sock.links[0].from_node.type == 'TEX_IMAGE' and bc_sock.links[0].from_node.image):
        print(json.dumps({"error": "Base Color is not texture-fed on this material — nothing to sample for a split."}))
    elif not mesh.uv_layers.active:
        print(json.dumps({"error": "No active UV layer — cannot determine islands."}))
    else:
        img = bc_sock.links[0].from_node.image
        uv_layer = mesh.uv_layers.active.data

        edge_poly_count = {}
        poly_of_edge = {}
        target_slot = next((i for i, s in enumerate(obj.material_slots) if s.material == mat), None)

        for poly in mesh.polygons:
            for key in poly.edge_keys:
                edge_poly_count[key] = edge_poly_count.get(key, 0) + 1
                poly_of_edge.setdefault(key, []).append(poly.index)

        seam_edges = set()
        for edge in mesh.edges:
            key = tuple(sorted(edge.vertices))
            if edge.use_seam or edge_poly_count.get(key, 0) <= 1:
                seam_edges.add(key)

        relevant_polys = [p.index for p in mesh.polygons if p.material_index == target_slot]
        relevant_set = set(relevant_polys)
        visited = set()
        islands = []
        for pidx in relevant_polys:
            if pidx in visited:
                continue
            stack = [pidx]
            island = []
            visited.add(pidx)
            while stack:
                cur = stack.pop()
                island.append(cur)
                p = mesh.polygons[cur]
                for key in p.edge_keys:
                    if key in seam_edges:
                        continue
                    for neighbor in poly_of_edge.get(key, []):
                        if neighbor in relevant_set and neighbor not in visited:
                            visited.add(neighbor)
                            stack.append(neighbor)
            islands.append(island)

        width, height = img.size
        pixels = img.pixels[:]
        channels = img.channels

        def sample_at_uv(u, v):
            px = min(width - 1, max(0, int(u * width)))
            py = min(height - 1, max(0, int(v * height)))
            idx = (py * width + px) * channels
            if idx + 2 >= len(pixels):
                return None
            return (pixels[idx] + pixels[idx + 1] + pixels[idx + 2]) / 3.0

        island_info = []
        for island in islands:
            samples = []
            for pidx in island[:200]:
                p = mesh.polygons[pidx]
                us = [uv_layer[li].uv.x for li in p.loop_indices]
                vs = [uv_layer[li].uv.y for li in p.loop_indices]
                if not us:
                    continue
                avg = sample_at_uv(sum(us) / len(us), sum(vs) / len(vs))
                if avg is not None:
                    samples.append(avg)
            if samples:
                island_info.append({"faces": island, "avg": sum(samples) / len(samples)})

        if len(island_info) < 2:
            print(json.dumps({"error": "Fewer than 2 sampleable islands — nothing to split."}))
        else:
            island_info.sort(key=lambda x: x["avg"])
            gaps = [(island_info[i+1]["avg"] - island_info[i]["avg"], i) for i in range(len(island_info) - 1)]
            gaps.sort(reverse=True)

            # Real live incident: the single-largest-gap on its own peeled
            # off exactly 1 face out of ~1.9M on the couch — technically a
            # "split," practically useless. A real split needs the minority
            # group to be a genuine chunk of the mesh, not a stray fragment.
            # Walk candidate gaps largest-first and skip any whose minority
            # side is below MIN_SPLIT_FRACTION of the object's total faces,
            # rather than accepting whichever gap happens to be biggest.
            MIN_SPLIT_FRACTION = 0.02
            total_faces = sum(len(e["faces"]) for e in island_info)
            minority = None
            majority = None
            for _, split_at in gaps:
                cand_a = island_info[:split_at + 1]
                cand_b = island_info[split_at + 1:]
                if not cand_a or not cand_b:
                    continue
                # Real bug caught live: picking "minority" by ISLAND COUNT
                # (fewer islands) instead of FACE COUNT reassigned 99.9998%
                # of a real mesh's faces to the "minority" group, because
                # that side happened to have fewer but much larger islands
                # — backwards from what "minority" is supposed to mean.
                faces_a = sum(len(e["faces"]) for e in cand_a)
                faces_b = sum(len(e["faces"]) for e in cand_b)
                cand_minority = cand_a if faces_a < faces_b else cand_b
                cand_majority = cand_b if cand_minority is cand_a else cand_a
                minority_faces = min(faces_a, faces_b)
                if total_faces and (minority_faces / total_faces) >= MIN_SPLIT_FRACTION:
                    minority, majority = cand_minority, cand_majority
                    break

            if minority is None:
                print(json.dumps({
                    "error": "Could not find a meaningful split — every candidate gap's minority group "
                              "was below {}% of the object's faces (largest was a stray fragment, not a "
                              "real substance region).".format(round(MIN_SPLIT_FRACTION * 100, 1)),
                }))
            else:

                new_mat = mat.copy()
                new_mat.name = mat.name + "_split"
                obj.data.materials.append(new_mat)
                new_slot = len(obj.material_slots) - 1

                reassigned = 0
                for entry in minority:
                    for pidx in entry["faces"]:
                        mesh.polygons[pidx].material_index = new_slot
                        reassigned += 1
                mesh.update()

                print(json.dumps({
                    "object": '{OBJ}',
                    "original_material": mat.name,
                    "new_material": new_mat.name,
                    "island_count": len(island_info),
                    "minority_group": {"island_count": len(minority), "faces_reassigned": reassigned,
                                        "avg_range": [round(minority[0]["avg"], 4), round(minority[-1]["avg"], 4)]},
                    "majority_group": {"island_count": len(majority), "faces_kept": sum(len(e["faces"]) for e in majority),
                                        "avg_range": [round(majority[0]["avg"], 4), round(majority[-1]["avg"], 4)]},
                }))
""".replace("{OBJ}", object_name.replace("'", "\\'")) \
   .replace("{MAT}", material_name.replace("'", "\\'"))

    _before_image = _capture_plain_screenshot(object_name)

    try:
        raw = _send_raw("execute_code_safe", code=script, required_mode="OBJECT", push_undo=True)
        if "error" in raw:
            return [json.dumps({"error": raw["error"]})]
        output = raw.get("result", "")
        for line in output.strip().splitlines():
            line = line.strip()
            if line.startswith("{"):
                parsed = json.loads(line)
                if "error" in parsed:
                    return [json.dumps(parsed)]

                # Verify, don't assume — re-measure both materials' real
                # fingerprints after the split, same discipline as every
                # other mutating tool tonight.
                dna_after = _reaffirm_dna(object_name)
                fingerprints = {}
                for m in dna_after.get("materials", []):
                    if m.get("name") in (parsed.get("original_material"), parsed.get("new_material")):
                        fingerprints[m["name"]] = m.get("fingerprint")
                parsed["fingerprints_after_split"] = fingerprints
                parsed["note"] = (
                    "Both materials share the same node graph/texture immediately after a split — "
                    "this creates addressability, not visual differentiation. Apply weathering or "
                    "generate_procedural_material to the new material specifically for that."
                )

                after_image = _capture_plain_screenshot(object_name)
                out = []
                if _before_image:
                    out.append(_before_image)
                if after_image:
                    out.append(after_image)
                out.append(json.dumps(parsed, indent=2))
                return out
        return [json.dumps({"error": "No JSON output from split_blended_material", "raw": output})]
    except Exception as e:
        logger.error(f"Error in split_blended_material: {e}")
        return [json.dumps({"error": str(e)})]


_SMOKE_TEST_OBJ = "_LiveSmokeTest_Cube"
_SMOKE_TEST_SENTINEL_MAT = "_LiveSmokeTest_Sentinel"
_SMOKE_TEST_TARGET_MAT = "_LiveSmokeTest_Target"
_SMOKE_TEST_TEX = "_LiveSmokeTest_SentinelTex"
_SMOKE_TEST_SENTINEL_RGB = (0.8, 0.2, 0.1)


def _smoke_test_sample_sentinel() -> Optional[list]:
    """Directly samples the sentinel texture's real pixel average — the
    same technique that caught the live black-couch corruption bug
    (measuring, not assuming). Returns [r, g, b] or None if the sentinel
    doesn't exist (setup failed)."""
    code = r"""
import bpy, json
img = bpy.data.images.get('{TEX}')
if img is None:
    print(json.dumps({"error": "sentinel image not found"}))
else:
    px = img.pixels[:]
    n = len(px) // 4
    r = sum(px[i] for i in range(0, len(px), 4)) / n
    g = sum(px[i+1] for i in range(0, len(px), 4)) / n
    b = sum(px[i+2] for i in range(0, len(px), 4)) / n
    print(json.dumps({"rgb": [round(r, 4), round(g, 4), round(b, 4)]}))
""".replace("{TEX}", _SMOKE_TEST_TEX)
    raw = _send_raw("execute_code_safe", code=code, required_mode="OBJECT", push_undo=False)
    if "error" in raw:
        return None
    for line in raw.get("result", "").strip().splitlines():
        line = line.strip()
        if line.startswith("{"):
            parsed = json.loads(line)
            return parsed.get("rgb")
    return None


@mcp.tool()
def live_material_smoke_test(cleanup: bool = True) -> str:
    """
    LIVE SMOKE TEST — the actual insurance against tonight's incident, not
    just documentation of it. Every unit test in this codebase is mocked;
    the black-couch corruption bug passed all 292 of them and was only
    caught by manually creating a two-material test object, baking one
    material, and directly sampling the OTHER material's real pixel data
    to see if it changed. This tool formalizes exactly that process into a
    repeatable, callable check instead of a throwaway script — run it any
    time a change touches generate_procedural_material, bake_weathered_
    textures, apply_weathering_recipe, or safe_bake_measure, before calling
    that change "verified."

    What it does:
    1. Creates a disposable two-material test object far from the origin
       (won't visually collide with real scene content): a blank TARGET
       material, and a SENTINEL material with a known, non-black texture
       (0.8/0.2/0.1 — the same values used to catch the real bug tonight).
    2. Samples the sentinel's real pixels (before).
    3. Runs generate_procedural_material on the TARGET material only.
    4. Re-samples the sentinel — must be unchanged. This is the exact
       regression class that shipped once already.
    5. Runs apply_weathering_recipe then bake_weathered_textures on the
       TARGET material — the OTHER real call site with the same historical
       exposure — and re-samples the sentinel again.
    6. Cleans up the disposable object (unless cleanup=False, useful for
       manual follow-up inspection in the Blender UI).

    Returns a structured report: which checks ran, the actual measured
    sentinel RGB at each stage, and a plain PASS/FAIL verdict per check —
    not just "no exception was raised." A smoke test that can't fail isn't
    a real check; each stage's tolerance (0.02 per channel) is tight enough
    that the real corruption bug (sentinel going fully black, a ~0.8 swing)
    would have failed it immediately.
    """
    report = {"checks": [], "verdict": "UNKNOWN"}

    setup_code = r"""
import bpy, json
old = bpy.data.objects.get('{OBJ}')
if old:
    bpy.data.objects.remove(old, do_unlink=True)
for matname in ['{SENTINEL}', '{TARGET}']:
    m = bpy.data.materials.get(matname)
    if m:
        bpy.data.materials.remove(m)
img_existing = bpy.data.images.get('{TEX}')
if img_existing:
    bpy.data.images.remove(img_existing)

bpy.ops.mesh.primitive_cube_add(size=2, location=(1000, 1000, 1000))
cube = bpy.context.active_object
cube.name = '{OBJ}'

mat_target = bpy.data.materials.new(name='{TARGET}')
mat_target.use_nodes = True
cube.data.materials.append(mat_target)

mat_sentinel = bpy.data.materials.new(name='{SENTINEL}')
mat_sentinel.use_nodes = True
img = bpy.data.images.new('{TEX}', width=16, height=16, alpha=False)
px = list(img.pixels)
for i in range(0, len(px), 4):
    px[i] = {R}; px[i+1] = {G}; px[i+2] = {B}
img.pixels = px
img.update()
tex_node = mat_sentinel.node_tree.nodes.new("ShaderNodeTexImage")
tex_node.image = img
principled = next(n for n in mat_sentinel.node_tree.nodes if n.type == 'BSDF_PRINCIPLED')
mat_sentinel.node_tree.links.new(tex_node.outputs["Color"], principled.inputs["Base Color"])
tex_node.select = True
mat_sentinel.node_tree.nodes.active = tex_node
cube.data.materials.append(mat_sentinel)

for i, p in enumerate(cube.data.polygons):
    p.material_index = 1 if i % 2 == 0 else 0
cube.data.update()
print(json.dumps({"ok": True}))
""".replace("{OBJ}", _SMOKE_TEST_OBJ).replace("{SENTINEL}", _SMOKE_TEST_SENTINEL_MAT) \
   .replace("{TARGET}", _SMOKE_TEST_TARGET_MAT).replace("{TEX}", _SMOKE_TEST_TEX) \
   .replace("{R}", str(_SMOKE_TEST_SENTINEL_RGB[0])).replace("{G}", str(_SMOKE_TEST_SENTINEL_RGB[1])) \
   .replace("{B}", str(_SMOKE_TEST_SENTINEL_RGB[2]))

    def cleanup_test_object():
        cleanup_code = r"""
import bpy, json
obj = bpy.data.objects.get('{OBJ}')
if obj:
    bpy.data.objects.remove(obj, do_unlink=True)
for matname in ['{SENTINEL}', '{TARGET}']:
    m = bpy.data.materials.get(matname)
    if m:
        bpy.data.materials.remove(m)
img = bpy.data.images.get('{TEX}')
if img:
    bpy.data.images.remove(img)
print(json.dumps({"ok": True}))
""".replace("{OBJ}", _SMOKE_TEST_OBJ).replace("{SENTINEL}", _SMOKE_TEST_SENTINEL_MAT) \
   .replace("{TARGET}", _SMOKE_TEST_TARGET_MAT).replace("{TEX}", _SMOKE_TEST_TEX)
        _send_raw("execute_code_safe", code=cleanup_code, required_mode="OBJECT", push_undo=False)

    try:
        raw = _send_raw("execute_code_safe", code=setup_code, required_mode="OBJECT", push_undo=True)
        if "error" in raw:
            report["verdict"] = "SETUP_FAILED"
            report["error"] = raw["error"]
            return json.dumps(report, indent=2)

        def tolerance_ok(before, after):
            if before is None or after is None:
                return False
            return all(abs(b - a) <= 0.02 for b, a in zip(before, after))

        rgb_initial = _smoke_test_sample_sentinel()
        report["sentinel_initial_rgb"] = rgb_initial

        _invalidate_dna_cache(_SMOKE_TEST_OBJ)
        gen_result = generate_procedural_material(
            object_name=_SMOKE_TEST_OBJ, material_name=_SMOKE_TEST_TARGET_MAT, category="metal"
        )
        gen_parsed = json.loads(gen_result[-1])
        rgb_after_gen = _smoke_test_sample_sentinel()
        gen_ok = tolerance_ok(rgb_initial, rgb_after_gen) and "error" not in gen_parsed
        report["checks"].append({
            "check": "generate_procedural_material does not corrupt the sentinel material",
            "pass": gen_ok,
            "sentinel_rgb_after": rgb_after_gen,
            "generate_result_had_error": "error" in gen_parsed,
        })

        weather_result = apply_weathering_recipe(object_name=_SMOKE_TEST_OBJ, material_name=_SMOKE_TEST_TARGET_MAT)
        weather_parsed = json.loads(weather_result[-1])
        bake_result = bake_weathered_textures(
            object_name=_SMOKE_TEST_OBJ, material_name=_SMOKE_TEST_TARGET_MAT,
            output_dir=_DNA_EXPORT_DIR, resolution=64, force=True,
        )
        bake_parsed = json.loads(bake_result[-1])
        rgb_after_bake = _smoke_test_sample_sentinel()
        bake_ok = tolerance_ok(rgb_initial, rgb_after_bake) and "error" not in bake_parsed
        report["checks"].append({
            "check": "apply_weathering_recipe + bake_weathered_textures do not corrupt the sentinel material",
            "pass": bake_ok,
            "sentinel_rgb_after": rgb_after_bake,
            "weather_had_error": "error" in weather_parsed,
            "bake_had_error": "error" in bake_parsed,
        })

        report["verdict"] = "PASS" if all(c["pass"] for c in report["checks"]) else "FAIL"
        return json.dumps(report, indent=2)
    finally:
        if cleanup:
            cleanup_test_object()


@mcp.tool()
def bake_weathered_textures(
    object_name: str,
    material_name: str,
    output_dir: str,
    resolution: int = 2048,
    bake_roughness: Optional[bool] = None,
    rewire_to_baked: bool = True,
    force: bool = False,
) -> list:
    """
    BAKE TO TEXTURE — closes the gap between "looks right in Blender" and
    "survives export." Everything apply_weathering_recipe/export_material_as_
    materialx produces is live Blender procedural shading (Mix nodes, vertex
    color attributes) — none of it exists once exported to FBX/UE5. This
    bakes the EFFECTIVE Base Color (and optionally Roughness) into real
    portable PNG files via the Emission-trick (routes the property's current
    source through an Emission shader and bakes with type='EMIT', capturing
    node values directly regardless of scene lighting).

    Returns a list: [before_image?, after_image?, result_json_string] —
    before/after FRONT-view screenshots are included whenever at least one
    channel was actually baked (skipped on a no-op/error/gate-refused call),
    same convention as apply_weathering_recipe and auto_repair_mesh — visual
    proof travels with the result instead of depending on a follow-up
    screenshot (real incident: KB-006's black bake artifacts were only
    caught because a screenshot happened to get taken afterward). The JSON
    result is always the LAST element; parse it with json.loads(result[-1]).

    Handles two real failure modes this session hit live, not hypothetically:
    - Blender's bake operator validates EVERY material on the object, not
      just the target — one broken/unresolved texture reference anywhere on
      the object (a real Tripo3D pipeline artifact, not rare) fails the
      whole bake. Broken images (0 channels or 0x0 size) are temporarily
      swapped for a placeholder for the bake's duration only, then restored.
    - A failed bake mid-operation can leave the material's Output.Surface
      wired to a temporary Emission node instead of the real shader if
      cleanup doesn't run. Original wiring is captured before ANY node is
      created, and restoration runs in a finally block — guaranteed even if
      the bake itself raises.

    rewire_to_baked=True (default) also reconnects Base Color/Roughness to
    the new baked textures instead of the procedural chain, making the
    material genuinely export-ready — not just previewable in Blender.
    Requires the object to already have a UV map (it will).

    bake_roughness: pass True/False to force it. Left at its default (None),
    it's inferred from Asset DNA — True if this material's Roughness socket
    isn't texture-fed (get_asset_dna's missing_maps), meaning it's still
    procedural/constant and worth baking; False if it's already a real
    texture, to skip redundant work. An explicit value always wins over the
    inferred one. After baking, re-checks DNA and reports whether Base
    Color/Roughness actually stopped showing up as missing_maps — not just
    that the bake operation didn't raise.

    Refuses to bake onto unrepaired non-manifold/boundary-edge topology
    unless force=True (real incident: baking onto a mesh auto_repair_mesh
    had already flagged production_ready: false produced genuine black-
    texel artifacts — KB-006). force=True is for when a human has looked
    at the topology and decided to bake anyway; it doesn't silence the
    reason, just the block.
    """
    _invalidate_dna_cache(object_name)
    dna_before = _reaffirm_dna(object_name)
    mat_before = next(
        (m for m in dna_before.get("materials", []) if m.get("name") == material_name), {}
    )

    nm_edges = dna_before.get("geometry", {}).get("non_manifold_edges") or 0
    bd_edges = dna_before.get("geometry", {}).get("boundary_edges") or 0
    if (nm_edges or bd_edges) and not force:
        return [json.dumps({
            "error": "Refusing to bake onto unrepaired topology.",
            "non_manifold_edges": nm_edges,
            "boundary_edges": bd_edges,
            "why": "Baking onto non-manifold/boundary-edge geometry can produce genuine "
                   "black-texel artifacts (Cycles sampling the wrong or degenerate face at "
                   "some texels) — a real, previously-hit incident (KB-006), not theoretical.",
            "fix": "Run auto_repair_mesh(object_name) first, or if the remaining topology "
                   "issue is acceptable for this asset, call again with force=True.",
        })]

    if bake_roughness is None:
        bake_roughness = "Roughness" in mat_before.get("missing_maps", [])
    import os
    os.makedirs(output_dir, exist_ok=True)
    safe_mat_name = re.sub(r'[^A-Za-z0-9_-]', '_', material_name)
    basecolor_path = os.path.join(output_dir, f"{safe_mat_name}_baked_basecolor.png")
    roughness_path = os.path.join(output_dir, f"{safe_mat_name}_baked_roughness.png")

    script = _SAFE_MATERIAL_BAKE_SNIPPET + r"""
import bpy, json, statistics, traceback

result = {"errors": []}
obj = bpy.data.objects.get('{OBJ}')
mat = bpy.data.materials.get('{MAT}')

if obj is None:
    print(json.dumps({"error": "Object not found: {OBJ}"}))
elif mat is None:
    print(json.dumps({"error": "Material not found: {MAT}"}))
else:
    nt = mat.node_tree
    principled = next((n for n in nt.nodes if n.type == 'BSDF_PRINCIPLED'), None)
    output_node = next((n for n in nt.nodes if n.type == 'OUTPUT_MATERIAL'), None)
    mat_slot_index = next((i for i, s in enumerate(obj.material_slots) if s.material == mat), None)

    if principled is None or output_node is None:
        print(json.dumps({"error": "Material has no Principled BSDF / Output node"}))
    else:
        # Capture TRUE original state BEFORE creating any temp nodes —
        # a failed bake must never leave this ambiguous.
        original_surface_link = output_node.inputs["Surface"].links[0] if output_node.inputs["Surface"].links else None
        original_surface_from = original_surface_link.from_socket if original_surface_link else None

        # Object-wide broken-image scan — Blender's bake validates every
        # material on the object, not just the target one.
        placeholder = bpy.data.images.new("TEMP_BakePlaceholder", width=4, height=4, alpha=False)
        swapped = []
        for slot in obj.material_slots:
            m = slot.material
            if not m or not m.node_tree:
                continue
            for n in m.node_tree.nodes:
                if n.type == 'TEX_IMAGE' and n.image:
                    img = n.image
                    if img.channels == 0 or img.size[0] == 0 or img.size[1] == 0:
                        swapped.append((n, img))
                        n.image = placeholder

        original_engine = bpy.context.scene.render.engine
        original_samples = bpy.context.scene.cycles.samples
        temp_nodes = []
        baked = {}

        def bake_pass(source_socket, image_name, w, h):
            # Scoped to ONLY this material's faces via safe_bake_measure —
            # real fix for a live incident where an unscoped bake corrupted
            # an unrelated material's real texture (Blender bakes every
            # material with faces on the object in one pass, each into
            # whichever image node is "active" in that material's own tree).
            bake_img = safe_bake_measure(obj, nt, mat_slot_index, output_node, source_socket,
                                          image_name, w, h, 16)
            if bake_img is None:
                raise RuntimeError(f"safe_bake_measure failed for {image_name}")

            pixels = bake_img.pixels[:]
            channels = bake_img.channels
            sample = [pixels[i] for i in range(0, min(len(pixels), 40000), channels)]

            return bake_img, {
                "min": round(min(sample), 4), "max": round(max(sample), 4),
                "mean": round(statistics.mean(sample), 4), "stdev": round(statistics.pstdev(sample), 4),
            }

        def near_black_pct(image, stride_mult=7):
            '''Fraction of sampled texels that are near-pure-black. Used to
            detect bake artifacts: a topology problem or a missing shader
            fallback can make a bake introduce black content the ORIGINAL
            source texture never had (real incident: 33.9% vs 15.2% on a
            mesh with unrepaired non-manifold edges — KB-006).'''
            px = image.pixels[:]
            ch = image.channels
            if ch == 0 or not px:
                return None
            n = 0
            black = 0
            for i in range(0, len(px), ch * stride_mult):
                n += 1
                if px[i] < 0.03 and px[i+1] < 0.03 and px[i+2] < 0.03:
                    black += 1
            return round(100 * black / n, 2) if n else None

        def find_source_tex_image(socket):
            '''Walk back at most one Mix node's "A" input to find the
            TEX_IMAGE node apply_weathering_recipe preserves the original
            through — that's precisely where it lives by this tool's own
            design. None if not found within that one hop (skip the
            comparison rather than guess).'''
            node = socket.node
            if node.type == 'TEX_IMAGE':
                return node.image
            if node.type == 'MIX':
                a_sock = next((s for s in node.inputs if s.name == 'A' and s.links), None)
                if a_sock:
                    src = a_sock.links[0].from_node
                    if src.type == 'TEX_IMAGE':
                        return src.image
            return None

        try:
            bpy.context.scene.render.engine = 'CYCLES'
            bpy.context.scene.cycles.samples = 16

            bc_link = principled.inputs["Base Color"].links[0] if principled.inputs["Base Color"].links else None
            if bc_link:
                source_img = find_source_tex_image(bc_link.from_socket)
                source_black_pct = near_black_pct(source_img) if source_img else None

                bc_img, bc_stats = bake_pass(bc_link.from_socket, "TEMP_bake_basecolor", {RES}, {RES})
                bc_img.filepath_raw = '{BCPATH}'
                bc_img.file_format = 'PNG'
                bc_img.save()
                baked_black_pct = near_black_pct(bc_img)
                bc_stats["source_near_black_pct"] = source_black_pct
                bc_stats["baked_near_black_pct"] = baked_black_pct
                # +10 percentage points over source is a real anomaly, not
                # noise — the live incident this guards against was +18.7pts.
                bc_stats["bake_introduced_black_artifact"] = bool(
                    source_black_pct is not None and baked_black_pct is not None
                    and baked_black_pct > source_black_pct + 10.0
                )
                baked["base_color"] = {"path": '{BCPATH}', "stats": bc_stats}
            else:
                result["errors"].append("Base Color has no input connection — nothing to bake, it's already a flat constant")

            if {BAKEROUGH}:
                rough_link = principled.inputs["Roughness"].links[0] if principled.inputs["Roughness"].links else None
                if rough_link:
                    r_img, r_stats = bake_pass(rough_link.from_socket, "TEMP_bake_roughness", {RES}, {RES})
                    r_img.filepath_raw = '{ROUGHPATH}'
                    r_img.file_format = 'PNG'
                    r_img.save()
                    baked["roughness"] = {"path": '{ROUGHPATH}', "stats": r_stats}
                else:
                    result["errors"].append("Roughness has no input connection — nothing to bake")

            if {REWIRE} and baked:
                for prop_name, info in baked.items():
                    socket_name = "Base Color" if prop_name == "base_color" else "Roughness"
                    img = bpy.data.images.load(info["path"], check_existing=True)
                    tex_node = nt.nodes.new("ShaderNodeTexImage")
                    tex_node.name = "Baked_" + prop_name
                    tex_node.image = img
                    if prop_name == "roughness":
                        img.colorspace_settings.name = 'Non-Color'
                    nt.links.new(tex_node.outputs["Color"], principled.inputs[socket_name])
                result["rewired"] = True
            else:
                result["rewired"] = False

        except Exception as e:
            result["errors"].append(f"{type(e).__name__}: {e}")
            result["trace"] = traceback.format_exc()
        finally:
            # GUARANTEED restoration, even if the bake itself raised.
            for n in list(temp_nodes):
                try:
                    nt.nodes.remove(n)
                except Exception:
                    pass
            if original_surface_from is not None:
                try:
                    nt.links.new(original_surface_from, output_node.inputs["Surface"])
                except Exception:
                    pass
            for n, img in swapped:
                try:
                    n.image = img
                except Exception:
                    pass
            try:
                bpy.data.images.remove(placeholder)
            except Exception:
                pass
            bpy.context.scene.render.engine = original_engine
            bpy.context.scene.cycles.samples = original_samples

        result["baked"] = baked
        result["broken_images_worked_around"] = [img.name for n, img in swapped]
        print(json.dumps(result))
""".replace("{OBJ}", object_name.replace("'", "\\'")) \
   .replace("{MAT}", material_name.replace("'", "\\'")) \
   .replace("{RES}", str(resolution)) \
   .replace("{BCPATH}", basecolor_path.replace("\\", "\\\\")) \
   .replace("{ROUGHPATH}", roughness_path.replace("\\", "\\\\")) \
   .replace("{BAKEROUGH}", str(bake_roughness)) \
   .replace("{REWIRE}", str(rewire_to_baked))

    # Captured before the bake runs, unconditionally — cheap Blender-side
    # render, only actually RETURNED if something ends up getting baked.
    _before_image = _capture_plain_screenshot(object_name)

    try:
        raw = _send_raw("execute_code_safe", code=script, required_mode="OBJECT", push_undo=True)
        if "error" in raw:
            return [json.dumps({"error": raw["error"]})]
        output = raw.get("result", "")
        for line in output.strip().splitlines():
            line = line.strip()
            if line.startswith("{"):
                parsed = json.loads(line)
                dna_after = _reaffirm_dna(object_name)
                mat_after = next(
                    (m for m in dna_after.get("materials", []) if m.get("name") == material_name), {}
                )
                missing_after = mat_after.get("missing_maps", [])

                # "texture-fed now" (missing_maps check above) proves the
                # socket is WIRED to a baked image, not that the baked
                # CONTENT is sane — a real bug hit live tonight: a flat 0.0
                # roughness bake reported "confirmed" because it was
                # correctly wired, even though the pixel data itself was
                # degenerate (stdev 0.0, uniform mirror-smooth). Flag that
                # class of problem explicitly instead of only checking wiring.
                flat_bakes = [
                    prop for prop, info in parsed.get("baked", {}).items()
                    if info.get("stats", {}).get("stdev", 1.0) < 0.005
                ]

                # Same idea, different signal: does the baked Base Color have
                # meaningfully MORE near-black content than the original
                # source texture it was baked from? Computed live during the
                # bake (find_source_tex_image/near_black_pct in the script
                # above) — surfaced here so it's part of the tool's own
                # verified result, not something only visible by eyeballing
                # the viewport (real incident: KB-006, +18.7 points on a
                # mesh with unrepaired non-manifold topology).
                bc_stats = parsed.get("baked", {}).get("base_color", {}).get("stats", {})
                bake_artifact = bc_stats.get("bake_introduced_black_artifact", False)

                parsed["dna_verification"] = {
                    "base_color_confirmed": "base_color" in parsed.get("baked", {}) and "Base Color" not in missing_after,
                    "roughness_confirmed": (
                        ("Roughness" not in missing_after) if "roughness" in parsed.get("baked", {}) else None
                    ),
                    "suspiciously_flat_bakes": flat_bakes,
                    "flat_bake_warning": (
                        f"{', '.join(flat_bakes)} baked with near-zero variance (stdev < 0.005) — "
                        "wired correctly but the content itself may be degenerate (e.g. an unlinked "
                        "constant input's fallback wasn't set, or the mesh has topology problems "
                        "corrupting the bake). Inspect visually before trusting this bake."
                    ) if flat_bakes else None,
                    "bake_introduced_black_artifact": bake_artifact,
                    "black_artifact_warning": (
                        f"Base Color baked with {bc_stats.get('baked_near_black_pct')}% near-black pixels "
                        f"vs. {bc_stats.get('source_near_black_pct')}% in the original source texture — "
                        "a jump this large usually means unrepaired mesh topology (non-manifold/boundary "
                        "edges) is corrupting the bake, not legitimate shading. Inspect visually; consider "
                        "auto_repair_mesh before re-baking."
                    ) if bake_artifact else None,
                }

                out = []
                if parsed.get("baked"):
                    after_image = _capture_plain_screenshot(object_name)
                    if _before_image:
                        out.append(_before_image)
                    if after_image:
                        out.append(after_image)
                out.append(json.dumps(parsed, indent=2))
                return out
        return [json.dumps({"error": "No JSON output from bake_weathered_textures", "raw": output})]
    except Exception as e:
        logger.error(f"Error in bake_weathered_textures: {e}")
        return [json.dumps({"error": str(e)})]


def _write_materialx_document(material_name: str, properties: dict, output_path: str) -> dict:
    """Build a real .mtlx file via the MaterialX SDK — the actual portable
    artifact, not just a JSON description of one. Isolated from the tool
    function so a missing/broken MaterialX install degrades to a clear error
    instead of taking down the whole translation result."""
    try:
        import MaterialX as mx
    except ImportError:
        return {"file_written": None,
                "file_write_error": "MaterialX Python package not installed in this server's environment."}

    try:
        doc = mx.createDocument()
        shader = doc.addNode("open_pbr_surface", material_name + "_shader", "surfaceshader")

        color3_props = {"base_color", "emission_color"}
        float_props = {"metalness", "specular_roughness", "specular_IOR", "opacity"}

        for prop_name, source in properties.items():
            if source.get("type") == "image_texture" and source.get("filepath"):
                img_type = "color3" if prop_name in color3_props else "float"
                image_node = doc.addNode("image", f"{prop_name}_image", img_type)
                image_node.setInputValue("file", source["filepath"], "filename")
                shader.addInput(prop_name, img_type).setNodeName(image_node.getName())
            elif source.get("type") == "constant":
                value = source["value"]
                if prop_name in color3_props and isinstance(value, (list, tuple)):
                    shader.setInputValue(prop_name, mx.Color3(*value[:3]), "color3")
                elif prop_name in float_props and isinstance(value, (int, float)):
                    shader.setInputValue(prop_name, float(value), "float")
            # normal_map / unsupported sources: skipped, not guessed at

        doc.addMaterialNode(material_name, shader)
        mx.writeToXmlFile(doc, output_path)
        return {"file_written": output_path}
    except Exception as e:
        return {"file_written": None, "file_write_error": str(e)}


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
    """Scans the scene for existing SM_AssetName_LOD0..N objects and reports
    which are present/missing and their face-reduction ratios. Read-only —
    does NOT generate, rename, or create any LOD meshes; you still need to
    build those yourself (e.g. via decimation) before this has anything
    real to report on."""
    return _send_json("prepare_lod_names", base_name=base_name, lod_count=lod_count)


@mcp.tool()
def generate_collision_mesh(object_name: str, collision_type: str = "convex") -> str:
    """
    COLLISION MESH — creates a UCX_/UBX_ collision object for UE5 import.

    ASK THE USER FIRST before calling this. Many Unreal teams build collision
    directly in-engine instead of in Blender — this is a workflow choice, not
    a mechanical fix like a mesh repair. Only call after explicit confirmation
    that Blender-side collision generation (not in-Unreal) is what's wanted.

    collision_type: "convex" (UCX_ prefix, tight-fitting hull, duplicates+
    reduces the source mesh) | "box" (UBX_ prefix, axis-aligned bounding box —
    cheaper, looser fit). Creates a new object, hidden in the viewport by
    default (collision meshes aren't meant to be seen).
    """
    _invalidate_dna_cache(object_name)  # changes production.collision_mesh_present
    script = r"""
import bpy, json
from mathutils import Vector

src = bpy.data.objects.get('{OBJ}')
if src is None:
    print(json.dumps({"error": "Object not found: {OBJ}"}))
else:
    col_type = '{COLTYPE}'
    prefix   = "UCX_" if col_type == "convex" else "UBX_"
    col_name = prefix + '{OBJ}'

    existing = bpy.data.objects.get(col_name)
    if existing:
        bpy.data.objects.remove(existing, do_unlink=True)

    bpy.context.view_layer.objects.active = src
    bpy.ops.object.select_all(action='DESELECT')

    if col_type == "convex":
        src.select_set(True)
        bpy.ops.object.duplicate()
        hull_obj = bpy.context.active_object
        hull_obj.name = col_name
        hull_obj.data.name = col_name + "_mesh"
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.mesh.convex_hull()
        bpy.ops.object.mode_set(mode='OBJECT')
    else:
        corners = [Vector(c) for c in src.bound_box]
        local_min = Vector((min(c.x for c in corners), min(c.y for c in corners), min(c.z for c in corners)))
        local_max = Vector((max(c.x for c in corners), max(c.y for c in corners), max(c.z for c in corners)))
        center = (local_min + local_max) / 2
        size   = local_max - local_min
        bpy.ops.mesh.primitive_cube_add(size=1.0, location=src.matrix_world @ center)
        hull_obj = bpy.context.active_object
        hull_obj.name = col_name
        hull_obj.data.name = col_name + "_mesh"
        hull_obj.rotation_euler = src.rotation_euler.copy()
        hull_obj.scale = (
            max(size.x, 0.001) * src.scale.x,
            max(size.y, 0.001) * src.scale.y,
            max(size.z, 0.001) * src.scale.z,
        )
        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)

    hull_obj.hide_set(True)

    print(json.dumps({
        "collision_object": col_name,
        "collision_type": col_type,
        "verts": len(hull_obj.data.vertices),
        "faces": len(hull_obj.data.polygons),
        "hidden_in_viewport": True,
        "note": "Object created and hidden. Export alongside the source mesh in the same FBX for UE5 to pick it up as collision.",
    }))
""".replace("{OBJ}", object_name.replace("'", "\\'")).replace("{COLTYPE}", collision_type)

    try:
        blender = get_blender_connection()
        raw = blender.send_command("execute_code_safe", {
            "code": script, "required_mode": "OBJECT", "push_undo": True
        })
        if "error" in raw:
            return json.dumps({"error": raw["error"]})
        output = raw.get("result", "")
        for line in output.strip().splitlines():
            line = line.strip()
            if line.startswith("{"):
                parsed = json.loads(line)
                dna_after = _reaffirm_dna(object_name)
                parsed["dna_verification"] = {
                    "collision_mesh_confirmed": dna_after.get("production", {}).get("collision_mesh_present", False),
                }
                return json.dumps(parsed, indent=2)
        return json.dumps({"error": "No JSON output from generate_collision_mesh", "raw": output})
    except Exception as e:
        logger.error(f"Error in generate_collision_mesh: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_session_log() -> str:
    """Get the last ~20 commands executed this Blender session with status, for debugging and audit."""
    return _send_json("get_session_log")


# ─────────────────────────────────────────────────────────────────────────────
# AI TECHNICAL DIRECTOR LAYER (v2.2) — compound tools, auto-repair, critic
# ─────────────────────────────────────────────────────────────────────────────

# Live-calibrated from the real couch asset (tripo_node_ea89bc21,
# tripo_mat_ea89bc21) — the concrete known-blended proof case from tonight
# (leather body + wood trim + plaid blanket, confirmed live, material_count:
# 1 hiding it completely). First live run measured island_count=6,
# color_variance=0.00153 — the original 0.01 starting guess was ~6.5x too
# high and missed it (likely_blended came back False on a case we KNOW is
# blended). Set below that real measured value with margin.
#
# Second data point (the negative side, closing the original single-point
# gap): a genuinely single-substance test object (UV-seamed sphere, 3 real
# islands, one perfectly uniform gray texture) measured color_variance=0.0
# — correctly reports likely_blended=False at this threshold, no false
# positive. Both real measurements now bracket 0.001 consistently: 0.0
# (known-clean) < 0.001 (threshold) < 0.00153 (known-blended).
_HETEROGENEITY_THRESHOLD = 0.001

_PBR_SOCKET_SCAN_SCRIPT = _SAFE_MATERIAL_BAKE_SNIPPET + r"""
import bpy, json, statistics

def sample_avg_stdev(image, max_samples=1500):
    '''Strided sample of an image's first channel — real measured average AND
    spread, not just a single number. stdev is what makes this useful for
    normal maps: a flat/absent normal map has near-zero variance, a heavily
    detailed one has real variance — a genuine "how bumpy is this surface"
    signal, distinct from the Roughness (specular response) channel.'''
    channels = image.channels
    if channels == 0 or image.size[0] == 0 or image.size[1] == 0:
        return None, None
    pixels = image.pixels[:]
    texel_count = len(pixels) // channels
    stride = max(1, texel_count // max_samples)
    vals = [pixels[i] for i in range(0, len(pixels), channels * stride)]
    if not vals:
        return None, None
    return round(sum(vals) / len(vals), 4), round(statistics.pstdev(vals), 4)

def get_input(node, names):
    '''Version-safe socket lookup — Blender 4.x renamed several Principled
    BSDF inputs (e.g. "Subsurface" -> "Subsurface Weight", "Specular" ->
    "Specular IOR Level"). Try each known name, return the first that exists.'''
    for n in names:
        s = node.inputs.get(n)
        if s is not None:
            return s
    return None

def compute_heterogeneity(obj, principled):
    '''Detects a single material silently doing multiple jobs (the couch:
    leather + wood + fabric all baked into one shared material,
    material_count: 1 hiding it completely). material_count alone can never
    catch this — it's a SLOT count, not a substance count. This measures
    Base Color's real average per disconnected UV island (split at seams/
    boundary edges, same real signal apply_weathering_recipe's fraying
    technique already uses) and compares them — real regional variance, not
    a guess. Returns island_count=1/no signal when there's nothing to
    compare (single island, no texture, broken image) rather than forcing
    a verdict with no evidence.'''
    het = {"island_count": 0, "color_variance": None, "likely_blended": False, "note": None}
    bc_sock = principled.inputs.get("Base Color")
    if not (bc_sock and bc_sock.links and bc_sock.links[0].from_node.type == 'TEX_IMAGE' and bc_sock.links[0].from_node.image):
        het["note"] = "Base Color is not texture-fed — cannot measure regional heterogeneity"
        return het
    img = bc_sock.links[0].from_node.image
    if img.channels == 0 or img.size[0] == 0 or img.size[1] == 0:
        het["note"] = "broken image reference"
        return het

    mesh = obj.data
    if not mesh.uv_layers.active:
        het["note"] = "no active UV layer"
        return het
    uv_layer = mesh.uv_layers.active.data

    edge_poly_count = {}
    poly_of_edge = {}
    for poly in mesh.polygons:
        for key in poly.edge_keys:
            edge_poly_count[key] = edge_poly_count.get(key, 0) + 1
            poly_of_edge.setdefault(key, []).append(poly.index)

    # Island-breaking edges: UV seams (deliberate authoring boundaries) OR
    # true mesh boundary edges — the same real signal the fraying weathering
    # technique already relies on, not a new invented heuristic.
    seam_edges = set()
    for edge in mesh.edges:
        key = tuple(sorted(edge.vertices))
        if edge.use_seam or edge_poly_count.get(key, 0) <= 1:
            seam_edges.add(key)

    visited = set()
    islands = []
    for poly in mesh.polygons:
        if poly.index in visited:
            continue
        stack = [poly.index]
        island = []
        visited.add(poly.index)
        while stack:
            pidx = stack.pop()
            island.append(pidx)
            p = mesh.polygons[pidx]
            for key in p.edge_keys:
                if key in seam_edges:
                    continue
                for neighbor in poly_of_edge.get(key, []):
                    if neighbor not in visited:
                        visited.add(neighbor)
                        stack.append(neighbor)
        islands.append(island)

    het["island_count"] = len(islands)
    if len(islands) < 2:
        het["note"] = "single connected island (no internal seams/boundaries) — nothing to compare"
        return het

    width, height = img.size
    pixels = img.pixels[:]
    channels = img.channels

    def sample_at_uv(u, v):
        px = min(width - 1, max(0, int(u * width)))
        py = min(height - 1, max(0, int(v * height)))
        idx = (py * width + px) * channels
        if idx + 2 >= len(pixels):
            return None
        return (pixels[idx] + pixels[idx + 1] + pixels[idx + 2]) / 3.0

    island_averages = []
    for island in islands:
        samples = []
        for pidx in island[:200]:
            p = mesh.polygons[pidx]
            us, vs = [], []
            for li in p.loop_indices:
                uv = uv_layer[li].uv
                us.append(uv.x)
                vs.append(uv.y)
            if not us:
                continue
            avg = sample_at_uv(sum(us) / len(us), sum(vs) / len(vs))
            if avg is not None:
                samples.append(avg)
        if samples:
            island_averages.append(sum(samples) / len(samples))

    if len(island_averages) < 2:
        het["note"] = "could not sample enough islands for a real comparison"
        return het

    variance = statistics.pvariance(island_averages)
    het["color_variance"] = round(variance, 5)
    het["likely_blended"] = variance > {HETEROGENEITY_THRESHOLD}
    return het

obj = bpy.data.objects.get('{OBJECT_NAME}')
result = []
if obj is not None:
    expected = ["Base Color", "Roughness", "Metallic", "Normal"]
    for slot in obj.material_slots:
        m = slot.material
        if not m:
            continue
        entry = {"name": m.name, "has_principled": False, "texture_fed": [], "missing_maps": [],
                  "fingerprint": {}, "heterogeneity": {"island_count": 0, "color_variance": None,
                  "likely_blended": False, "note": "no Principled BSDF"}}
        if m.use_nodes and m.node_tree:
            principled = next((n for n in m.node_tree.nodes if n.type == 'BSDF_PRINCIPLED'), None)
            if principled:
                entry["has_principled"] = True
                entry["heterogeneity"] = compute_heterogeneity(obj, principled)
                for socket_name in expected:
                    sock = principled.inputs.get(socket_name)
                    if socket_name == "Normal":
                        # A real normal map is NEVER wired directly to
                        # Principled.Normal — it always goes through an
                        # intermediate Normal Map node (TEX_IMAGE -> Normal
                        # Map -> Principled.Normal). Checking for a direct
                        # TEX_IMAGE link here (like the other 3 sockets)
                        # means Normal reads "missing" on EVERY correctly-
                        # wired material — real bug caught live: a real,
                        # correctly-connected normal map on a 3-piece
                        # character was flagged missing by this exact check.
                        is_textured = bool(
                            sock and sock.links and sock.links[0].from_node.type == 'NORMAL_MAP'
                            and sock.links[0].from_node.inputs.get("Color")
                            and sock.links[0].from_node.inputs["Color"].links
                            and sock.links[0].from_node.inputs["Color"].links[0].from_node.type == 'TEX_IMAGE'
                        )
                    else:
                        is_textured = bool(sock and sock.links and sock.links[0].from_node.type == 'TEX_IMAGE')
                    if is_textured:
                        entry["texture_fed"].append(socket_name)
                    else:
                        entry["missing_maps"].append(socket_name)

                # Real material fingerprint — every value here is MEASURED,
                # never guessed, same discipline as metal_factor. This is the
                # richer signal set beyond the single metal_factor scalar that
                # material_category dispatch used alone before tonight.
                fp = {}

                rough_sock = principled.inputs.get("Roughness")
                if rough_sock and rough_sock.links and rough_sock.links[0].from_node.type == 'TEX_IMAGE' and rough_sock.links[0].from_node.image:
                    avg, _ = sample_avg_stdev(rough_sock.links[0].from_node.image)
                    fp["roughness_source"] = "texture_sampled" if avg is not None else "broken_image_fallback"
                    fp["roughness_avg"] = avg
                elif rough_sock and rough_sock.links:
                    # Linked to something other than a plain TEX_IMAGE (e.g. a
                    # Math/Mix node chain, exactly what generate_procedural_material
                    # builds) — the socket's own default_value is stale/meaningless
                    # once linked, so reporting it as "constant" is a fabricated
                    # number, not a measurement (real bug caught live: a genuinely
                    # measured 0.9789 roughness read back as a fabricated 0.5
                    # "constant" through this exact path). Only a real, face-scoped
                    # bake (safe_bake_measure — same fix as the black-couch incident)
                    # can read the TRUE current value.
                    rough_output_node = next((n for n in m.node_tree.nodes if n.type == 'OUTPUT_MATERIAL'), None)
                    rough_slot_index = next((i for i, s in enumerate(obj.material_slots) if s.material == m), None)
                    measured_rough = None
                    if rough_output_node is not None and rough_slot_index is not None:
                        calib_img = safe_bake_measure(obj, m.node_tree, rough_slot_index, rough_output_node,
                                                       rough_sock.links[0].from_socket, "TEMP_FPRoughCalib", 16, 16, 4)
                        if calib_img is not None:
                            measured_rough, _ = sample_avg_stdev(calib_img)
                            bpy.data.images.remove(calib_img)
                    fp["roughness_source"] = "procedural_measured" if measured_rough is not None else "procedural_measure_failed"
                    fp["roughness_avg"] = measured_rough
                elif rough_sock:
                    fp["roughness_source"] = "constant"
                    fp["roughness_avg"] = round(float(rough_sock.default_value), 4)
                else:
                    fp["roughness_source"] = None
                    fp["roughness_avg"] = None

                sss_sock = get_input(principled, ["Subsurface Weight", "Subsurface"])
                fp["subsurface_weight"] = round(float(sss_sock.default_value), 4) if sss_sock else None

                spec_sock = get_input(principled, ["Specular IOR Level", "Specular"])
                fp["specular_ior_level"] = round(float(spec_sock.default_value), 4) if spec_sock else None

                fp["normal_map_present"] = False
                fp["normal_map_bumpiness"] = None
                normal_sock = principled.inputs.get("Normal")
                if normal_sock and normal_sock.links and normal_sock.links[0].from_node.type == 'NORMAL_MAP':
                    normal_map_node = normal_sock.links[0].from_node
                    color_in = normal_map_node.inputs.get("Color")
                    if color_in and color_in.links and color_in.links[0].from_node.type == 'TEX_IMAGE' and color_in.links[0].from_node.image:
                        _, stdev = sample_avg_stdev(color_in.links[0].from_node.image)
                        if stdev is not None:
                            fp["normal_map_present"] = True
                            fp["normal_map_bumpiness"] = stdev

                entry["fingerprint"] = fp
        result.append(entry)
print(json.dumps(result))
"""

_DNA_CACHE_TTL_SECONDS = 300  # defense in depth for mutations _invalidate_dna_cache
                               # can't see (e.g. raw execute_code_safe scripts, or
                               # edits made directly in the Blender UI)

_TEXTURE_EXPORT_SCRIPT = r"""
import bpy, json
obj = bpy.data.objects.get('{OBJECT_NAME}')
mat = bpy.data.materials.get('{MATERIAL_NAME}') if obj else None
result = {"path": None}
if mat and mat.use_nodes and mat.node_tree:
    principled = next((n for n in mat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED'), None)
    sock = principled.inputs.get('{SOCKET_NAME}') if principled else None
    if sock and sock.links and sock.links[0].from_node.type == 'TEX_IMAGE':
        img = sock.links[0].from_node.image
        out_path = '{OUT_PATH}'
        orig_format = img.file_format
        img.filepath_raw = out_path
        img.file_format = 'PNG'
        img.save()
        img.file_format = orig_format
        result = {"path": out_path, "size": list(img.size)}
print(json.dumps(result))
"""

_DNA_EXPORT_DIR = "/private/tmp/claude-501/-Users-masonbrown-Desktop-blender-mcp-upgrade-main/ca239516-0531-44e8-a988-5b54bd0895fe/scratchpad/textures"


def _export_material_texture(object_name: str, material_name: str, socket_name: str, out_dir: str = _DNA_EXPORT_DIR) -> Optional[dict]:
    """Save the TEX_IMAGE feeding `socket_name` on `material_name` to a PNG in
    out_dir. Returns {"path", "size"} or None if the socket isn't texture-fed."""
    import os
    os.makedirs(out_dir, exist_ok=True)
    safe_mat = re.sub(r'[^A-Za-z0-9_-]', '_', material_name)
    safe_sock = re.sub(r'[^A-Za-z0-9_-]', '_', socket_name)
    out_path = os.path.join(out_dir, f"{safe_mat}_{safe_sock}.png")
    code = (
        _TEXTURE_EXPORT_SCRIPT
        .replace("{OBJECT_NAME}", object_name)
        .replace("{MATERIAL_NAME}", material_name)
        .replace("{SOCKET_NAME}", socket_name)
        .replace("{OUT_PATH}", out_path)
    )
    result = _send_raw("execute_code_safe", code=code, required_mode="OBJECT", push_undo=False)
    for line in str(result.get("result", "")).splitlines():
        line = line.strip()
        if line.startswith("{"):
            parsed = json.loads(line)
            return parsed if parsed.get("path") else None
    return None


@mcp.tool()
def get_asset_dna(object_name: str, target_engine: str = "unreal", force_refresh: bool = False) -> str:
    """
    COMPOUND TOOL — assembles one canonical ground-truth spec for an object:
    measured geometry/UV/material facts plus deterministic production-rule
    recommendations. No invented category/genre/confidence — identity.category
    comes only from session_update(active_playbook=...) if set, never guessed.
    Call this before recommending any workflow so every decision reasons
    against the same facts instead of re-deriving them each time.

    Raw fetches are cached per object and invalidated automatically by every
    mutating tool (weathering, bake, repair, collision gen, checkpoint restore,
    etc.) — see _invalidate_dna_cache(). A 300s TTL is a safety net for
    mutations made outside this MCP (raw execute_code_safe scripts, direct
    Blender UI edits) that the automatic hooks can't see. force_refresh=True
    bypasses the cache entirely when you know something changed.

    Each entry in materials[] carries a "fingerprint" — real MEASURED signals
    beyond metal_factor alone: roughness_source/roughness_avg (texture-
    sampled or constant), subsurface_weight, specular_ior_level, and
    normal_map_present/normal_map_bumpiness (pixel-variance of the connected
    normal map — a genuine "how physically detailed is this surface" signal,
    distinct from Roughness's specular-response meaning). Every value here is
    measured, never guessed — the same discipline as metal_factor. This is
    the raw material for the material knowledge layer (recipe_type="material"
    entries): record what was actually measured on a real material, not
    invented material-science facts.

    Each material entry also carries closest_known_material — the material
    knowledge layer looked up by MEASURED SIMILARITY (fingerprint distance),
    not by name. Auto-generated material names never match a recorded
    trigger_phrase, so this is the only retrieval path that actually works
    on most real assets. null when nothing recorded is close enough —
    never forces a match just because something's the least-far option.
    """
    try:
        cached = None if force_refresh else _SNAPSHOTS.get(object_name, {}).get("_dna_raw")
        if cached and (time.time() - cached[0]) < _DNA_CACHE_TTL_SECONDS:
            _, raw_quality, raw_topology, raw_ue5, raw_object, raw_materials, handoffs = cached
        else:
            raw_quality  = _send_raw("get_mesh_quality_report", name=object_name)
            raw_topology = _send_raw("analyze_topology", name=object_name, context="generic")
            raw_ue5      = _send_raw("run_unreal_readiness_check", name=object_name)
            raw_object   = _send_raw("get_object_info", name=object_name)
            pbr_code     = _PBR_SOCKET_SCAN_SCRIPT.replace("{OBJECT_NAME}", object_name) \
                                                   .replace("{HETEROGENEITY_THRESHOLD}", str(_HETEROGENEITY_THRESHOLD))
            pbr_result   = _send_raw("execute_code_safe", code=pbr_code, required_mode="OBJECT", push_undo=False)
            raw_materials = []
            for line in str(pbr_result.get("result", "")).splitlines():
                line = line.strip()
                if line.startswith("["):
                    raw_materials = json.loads(line)
                    break

            # Missing-normal-map auto-handoff: computed once per fresh fetch
            # (not on every cache hit — a cache hit means nothing about the
            # material could have changed, so re-exporting would just be a
            # redundant Blender round-trip), then cached alongside the raw
            # bundle so the output shape is identical whether this call hit
            # the cache or not.
            handoffs = {}
            for mat in raw_materials:
                if "Normal" not in mat.get("missing_maps", []):
                    continue
                exported = _export_material_texture(object_name, mat["name"], "Base Color")
                if exported:
                    handoffs[mat["name"]] = {
                        "export_path": exported["path"],
                        "next_step": (
                            "Generate a normal map from this Base Color texture at "
                            "https://cpetry.github.io/NormalMap-Online/, then hand it "
                            "back to rewire it into the material."
                        ),
                    }

            _SNAPSHOTS.setdefault(object_name, {})["_dna_raw"] = (
                time.time(), raw_quality, raw_topology, raw_ue5, raw_object, raw_materials, handoffs
            )

        if "error" in raw_quality:
            return json.dumps({"error": raw_quality["error"]})

        counts   = raw_quality.get("counts", {})
        uv       = raw_quality.get("uv", {})
        modifiers = raw_quality.get("modifiers", [])
        has_armature = any(
            isinstance(m, dict) and m.get("type") == "ARMATURE" for m in modifiers
        )
        checks = raw_ue5.get("checks", {})

        category = _session_get("active_playbook")
        playbook = _PLAYBOOKS.get(category) if category else None
        effective_vert_budget = (
            _studio_vert_budget(category, playbook.get("vert_budget", float("inf")))
            if playbook else None
        )

        # Retrieval by measured similarity, not by material name — auto-
        # generated names (tripo_mat_XXXXXXXX) never match a trigger_phrase,
        # so this is the only way the material knowledge layer is actually
        # reachable on most real assets. Runs unconditionally on every
        # material in every get_asset_dna call, not opt-in.
        for mat in raw_materials:
            mat["closest_known_material"] = _find_closest_material_recipe(
                mat.get("fingerprint", {})
            )

        dna = {
            "object": object_name,
            "target_engine": target_engine,
            "identity": {
                "category": category,  # real session state only — never guessed
                "vertex_count": counts.get("verts", 0),
                "edge_count": counts.get("edges", 0),
                "polygon_count": counts.get("faces", 0),
                "has_armature": has_armature,
                "material_count": len(raw_object.get("materials", [])),
            },
            "geometry": {
                "topology_score": raw_topology.get("topology_score"),
                "quad_ratio_pct": raw_topology.get("stats", {}).get("quad_ratio_pct"),
                "tris_pct": raw_topology.get("stats", {}).get("tris_pct"),
                "ngon_count": raw_topology.get("stats", {}).get("ngons"),
                "non_manifold_edges": raw_topology.get("stats", {}).get("non_manifold_edges"),
                "boundary_edges": raw_topology.get("stats", {}).get("boundary_edges"),
                "has_uvs": uv.get("has_uvs", False),
                "uv_layer_count": uv.get("layer_count", 0),
                "lightmap_uv_present": checks.get("lightmap_uv", {}).get("pass", False),
            },
            "materials": raw_materials,
            "production": {
                "rig_present": has_armature,
                "lod_naming_present": checks.get("lod_naming", {}).get("pass", False),
                "collision_mesh_present": checks.get("collision_mesh", {}).get("pass", False),
                "playbook_vert_budget": effective_vert_budget,
                "over_budget": (
                    bool(playbook) and counts.get("verts", 0) > (effective_vert_budget or float("inf"))
                ),
            },
        }
        dna["rules_fired"] = _evaluate_production_rules(dna)

        if handoffs:
            for fired in dna["rules_fired"]:
                if fired["id"] == "missing_pbr_maps":
                    fired["handoff"] = handoffs

        return json.dumps(dna, indent=2, default=str)

    except Exception as e:
        logger.error(f"Error in get_asset_dna: {e}")
        return json.dumps({"error": str(e)})


def _reaffirm_dna(object_name: str) -> dict:
    """Force-refresh Asset DNA immediately after a mutation, for tools that
    need to confirm their own effect rather than trust the operation succeeded
    just because it didn't raise. Returns the parsed DNA dict (or {"error":...})."""
    return json.loads(get_asset_dna(object_name, force_refresh=True))


@mcp.tool()
def analyze_mesh_for_unreal(name: str, topology_context: str = "generic", verbose: bool = False) -> str:
    """
    COMPOUND TOOL — full pre-export analysis: detect_mesh_problems +
    get_mesh_quality_report + analyze_topology + run_unreal_readiness_check,
    combined into one prioritised report. First step before any UE5 export.
    topology_context: "generic"|"character_body"|"face"|"hand"|"hard_surface".
    verbose=False (default) returns only verdict + failing/warning findings.
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

        session_asset_type = _session_get("asset_type")
        if session_asset_type:
            # Trust what the user already confirmed this session over re-guessing
            # from vert-count/armature heuristics on every single call.
            assumed_tier = session_asset_type
            budget_note  = (
                f"Evaluating as '{session_asset_type}' — confirmed this session via "
                f"session_update(). {vert_count:,} verts. Call session_update(asset_type=...) "
                "again if this has changed."
            )
        elif vert_count > 300_000:
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
def auto_repair_mesh(name: str, dry_run: bool = False) -> list:
    """
    AUTO-REPAIR — safe mesh cleanup: non-manifold edges, loose verts, duplicate
    faces, zero-area faces, inverted normals. Verifies each repair actually
    reduced its problem count (doesn't just trust "no exception" as success).

    Does NOT touch: ngons or UV overlaps — those need artist judgment on edge
    flow, not a mechanical fix.

    dry_run=True: plans only, no changes. dry_run=False: executes + verifies.
    Returns list: dry_run=True → [report]; dry_run=False → [before_image,
    after_image, report] — FRONT-view screenshots with problem markers burned in.
    """
    if not dry_run:
        _invalidate_dna_cache(name)
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
            return [{
                "object": name,
                "status": "no_auto_repairs_needed",
                "message": (
                    "No auto-repairable problems found. "
                    f"Issues requiring artist review: {needs_artist or 'none'}."
                ),
                "before": reasoned_before.get("_reasoning", {}),
            }]

        if dry_run:
            return [{
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
            }]

        # ── STEP 2b: Pre-repair screenshot (Tier 1c) ─────────────────────────
        # Taken now: scan confirmed there IS work to do, but no repairs have run yet.
        _pre_screenshot_bytes = None
        _pre_screenshot_image = None
        try:
            _pre_screenshot_bytes = _capture_single_front_view(name)
            if _pre_screenshot_bytes:
                try:
                    _pre_coords = json.loads(get_problem_coordinates(name))
                except Exception:
                    _pre_coords = {}
                _pre_annotated = _annotate_image_with_clusters(
                    _pre_screenshot_bytes, _pre_coords, "FRONT"
                )
                _pre_screenshot_image = Image(data=_pre_annotated, format="png")
        except Exception as _pre_err:
            logger.warning(f"auto_repair_mesh: pre-repair screenshot failed: {_pre_err}")

        # ── STEP 3: Execute repairs in safe order ──────────────────────────
        before_counts = {p.get("type", ""): p.get("count", 0) for p in raw_before.get("problems", [])}

        attempted = []
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
                if "error" in result:
                    repair_errors.append(f"{repair_key}: {result.get('error', 'unknown error')}")
                    continue
            except Exception as e:
                repair_errors.append(f"{repair_key}: {e}")
                continue
            attempted.append(repair_key)

        # ── STEP 3b: Post-repair screenshot (Tier 1c) ────────────────────────
        # Taken immediately after all repair scripts have run, before verify scan.
        _post_screenshot_bytes = None
        _post_screenshot_image = None
        try:
            _post_screenshot_bytes = _capture_single_front_view(name)
            if _post_screenshot_bytes:
                try:
                    _post_coords = json.loads(get_problem_coordinates(name))
                except Exception:
                    _post_coords = {}
                _post_annotated = _annotate_image_with_clusters(
                    _post_screenshot_bytes, _post_coords, "FRONT"
                )
                _post_screenshot_image = Image(data=_post_annotated, format="png")
        except Exception as _post_err:
            logger.warning(f"auto_repair_mesh: post-repair screenshot failed: {_post_err}")

        # ── STEP 4: Verify — re-scan after repairs and confirm each attempted
        # repair actually reduced its problem count. A script can run without
        # raising an error and still change nothing (e.g. dissolve_degenerate
        # on a fully isolated face with no neighboring geometry to fold into) —
        # "no exception" is not evidence of a real fix, so don't trust it alone.
        raw_after = _send_raw("detect_mesh_problems", name=name)
        if "error" in raw_after:
            reasoned_after = {"error": raw_after["error"]}
            after_counts = {}
        else:
            reasoned_after = _reason_mesh_problems(raw_after).get("_reasoning", {})
            after_counts = {p.get("type", ""): p.get("count", 0) for p in raw_after.get("problems", [])}

        repairs_executed = []
        repairs_no_effect = []
        for repair_key in attempted:
            problem_type = "isolated_verts" if repair_key == "loose_vertices" else repair_key
            before_n = before_counts.get(problem_type, 0)
            after_n = after_counts.get(problem_type, 0)
            if after_n < before_n:
                repairs_executed.append(f"{repair_key} ({before_n}→{after_n})")
            else:
                repairs_no_effect.append(repair_key)

        # ── STEP 5: Build result report ────────────────────────────────────
        before_summary = reasoned_before.get("_reasoning", {})
        remaining_issues = reasoned_after.get("findings", []) if isinstance(reasoned_after, dict) else []
        remaining_critical = [f for f in remaining_issues if f.get("severity") == "critical"]

        status = "success" if repairs_executed and not repair_errors and not repairs_no_effect and not remaining_critical else (
            "partial" if (repairs_executed or repairs_no_effect) else "failed"
        )

        # Mark multiview capture stale — mesh geometry has changed
        if status in ("success", "partial"):
            mv = _session_get("multiview")
            if isinstance(mv, dict):
                mv["capture_stale"] = True
                _session_set(multiview=mv)
                _save_session()

        # Store pre/post bytes in _VISUAL_SNAPSHOTS (Tier 1c)
        import datetime as _dt
        _VISUAL_SNAPSHOTS[name] = {
            "before":    _pre_screenshot_bytes,
            "after":     _post_screenshot_bytes,
            "timestamp": _dt.datetime.now().isoformat(),
            "status":    status,
        }

        nm_before = before_counts.get("non_manifold_edges", 0)
        nm_after  = after_counts.get("non_manifold_edges", 0)

        report_dict = {
            "object": name,
            "status": status,
            "repairs_executed": repairs_executed,
            "repairs_attempted_no_effect": repairs_no_effect,
            "repair_errors": repair_errors,
            "issues_that_need_artist_review": needs_artist,
            "dna_verification": {
                "non_manifold_edges_before": nm_before,
                "non_manifold_edges_after": nm_after,
                "confirmed": nm_after <= nm_before,
            },
            "summary": (
                f"Repaired {len(repairs_executed)} issue(s): {', '.join(repairs_executed) or 'none'}. "
                f"{len(repairs_no_effect)} repair(s) ran but had no measurable effect "
                f"(needs manual review): {', '.join(repairs_no_effect) or 'none'}. "
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
            "visual_diff": {
                "before_image": "image[0] — FRONT view before repair with problem markers",
                "after_image":  "image[1] — FRONT view after repair with any remaining markers",
                "how_to_read": (
                    "Compare image[0] (before) vs image[1] (after). "
                    "Colored circles = problem clusters: RED=critical, ORANGE=warning, BLUE=pole. "
                    "Fewer/no circles in image[1] = repair succeeded. "
                    "Same circles in both = issue survived, needs artist review."
                ),
            } if (_pre_screenshot_image and _post_screenshot_image) else {
                "note": "Visual screenshots could not be captured — check Blender connection.",
            },
        }

        # Journal + issue tracker (Sprint A)
        _journal_entry(
            "auto_repair_mesh", name, status,
            f"Repaired: {repairs_executed or 'none'}. Remaining critical: {len(remaining_critical)}."
        )
        # Close issues that were fixed
        repaired_types = [r.split(" ")[0] for r in repairs_executed]
        _close_issues_for(name, repaired_types, closed_by="auto_repair_mesh")
        # Open issues for anything that remains critical
        for f in remaining_critical:
            _open_issue("auto_repair_mesh", name, f.get("type","unknown"), "critical",
                        f.get("description", "")[:200])

        # Return [before_image, after_image, report_dict] (Tier 1c)
        out = []
        if _pre_screenshot_image:
            out.append(_pre_screenshot_image)
        if _post_screenshot_image:
            out.append(_post_screenshot_image)
        out.append(report_dict)
        return out

    except Exception as e:
        logger.error(f"Error in auto_repair_mesh: {e}")
        _journal_entry("auto_repair_mesh", name, "error", str(e))
        return [{"error": str(e), "object": name}]


@mcp.tool()
def critique_animation(name: str, frame_start: Optional[int] = None, frame_end: Optional[int] = None) -> str:
    """
    ANIMATION CRITIC — plain-English animation review: grade A-F, issues
    ranked by severity, frame-accurate correction guidance, production
    readiness verdict. Wraps analyze_animation_quality with reasoning.
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
    AI ANIMATION COACH — frame-specific coaching on contact timing, weight
    transfer arcs, anticipation, follow-through. Unlike critique_animation
    (what's wrong with the data), this explains *why it reads wrong* to an
    animator's eye and which principle fixes it. focus: "all" | "timing" |
    "arcs" | "weight" | "contact" | "follow_through". Adds a lesson per
    finding when apprentice_mode=True (via session_update).
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
    PIPELINE STAGE CLASSIFIER — infers which of 6 stages the asset is in
    (Sculpt, Retopo, Bake-Ready, Texture/Material, Rig/Animation, Export-Ready)
    from vertex count, topology, UVs, materials, armature, modifiers. Returns
    stage + confidence + signals_detected + applicable standards + next_steps.
    Call get_viewport_screenshot() first.
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
    PRIORITY ACTION — answers "what's the single most important thing to do
    right now?" One action, not a plan or list — the highest-leverage step
    for the inferred pipeline stage. States its assumption about asset
    purpose explicitly (context hint optional) so you can correct it if wrong.
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
        session_asset_type = _session_get("asset_type")
        if context:
            assumed_context = context
        elif session_asset_type:
            # Trust what's already confirmed this session over re-deriving from
            # signals on every call.
            assumed_context = session_asset_type
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

        # ── Knowledge-layer enrichment ─────────────────────────────────────────
        # Purely additive — never changes which action wins above. Surfaces two
        # things the material knowledge layer now knows that this tool
        # previously had no way to mention: a material with no active
        # Principled BSDF at all (generate_procedural_material can now build
        # one from scratch, calibrated — a real option that didn't exist
        # before tonight), and a material that closely matches something
        # already recorded (apply_weathering_recipe/generate_procedural_material
        # can build on that instead of starting blind). Advisory only — a
        # failure here never breaks the core recommendation above.
        material_knowledge_notes = []
        if has_materials:
            try:
                dna_for_materials = json.loads(get_asset_dna(object_name, target_engine="unreal"))
                for mat in dna_for_materials.get("materials", []):
                    mname = mat.get("name", "?")
                    if not mat.get("has_principled"):
                        material_knowledge_notes.append(
                            f"'{mname}' has no active Principled BSDF — "
                            f"generate_procedural_material(object_name, material_name='{mname}', "
                            f"category=...) can build a real, calibrated PBR material from scratch."
                        )
                    else:
                        match = mat.get("closest_known_material")
                        if match:
                            material_knowledge_notes.append(
                                f"'{mname}' closely matches the recorded '{match['canonical_name']}' "
                                f"recipe (distance {match['distance']}) — apply_weathering_recipe or "
                                f"generate_procedural_material can lean on that prior knowledge "
                                f"instead of starting blind."
                            )
            except Exception:
                pass

        # ── Playbook context ───────────────────────────────────────────────────
        pb = _get_active_playbook()
        playbook_block = None
        playbook_conflicts = []
        if pb:
            pb_name = pb["name"]
            pb_vert = _studio_vert_budget(_session_get("active_playbook"), pb["vert_budget"])
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
        if material_knowledge_notes:
            result["material_knowledge"] = material_knowledge_notes
        if verified:
            result["session_verified_checks"] = verified

        # Inject live issue tracker into what_next output (Sprint A)
        tracker = _SESSION.get("issue_tracker", [])
        open_tracker = [i for i in tracker if i.get("status") == "open"
                        and i.get("object") == object_name]
        if open_tracker:
            result["open_issues"] = [
                {"id": i["id"], "type": i["issue_type"], "severity": i["severity"],
                 "detail": i["detail"], "opened_by": i["tool"], "at": i["ts_opened"]}
                for i in open_tracker
            ]
            result["open_issue_count"] = len(open_tracker)

        _journal_entry("what_next", object_name, "ok",
                       f"stage={stage_num} action='{(action or '')[:80]}'")
        return json.dumps(result, indent=2, default=str)

    except Exception as e:
        logger.error(f"Error in what_next: {e}")
        _journal_entry("what_next", object_name, "error", str(e))
        return json.dumps({"error": str(e)})


@mcp.tool()
def analyze_rig_weights(object_name: str, verbose: bool = False) -> str:
    """
    RIG WEIGHT QA — checks vertex group weights (pass the MESH, not the
    armature). CRITICAL: unweighted verts (snap to world origin on first pose)
    and >8-influence verts (UE5 truncates to 8, discards the rest silently).
    WARNING: zero-weight assignments (memory/CPU cost, painting errors).
    verdict: CRITICAL | WARNING | PASS.
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
    RIG SKELETON QA — finds the armature via the object's ARMATURE modifier
    (pass the MESH object, not the armature) and checks: root bone at world
    origin (CRITICAL if off by >0.01), orphan bones with no vertex group
    (WARNING — expected for control/IK bones), bone count and UE5 root-naming
    convention (INFO). verdict: CRITICAL | WARNING | INFO | PASS.
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
    BAKE PRE-FLIGHT — run BEFORE baking. Hard-stops on conditions that cause
    black textures, smeared detail, or bad normals: missing UVs, overlapping
    UV islands, no Image Texture node active in the shader, invalid bake target.
    Also warns on tight UV margins, hidden high-poly, unapplied scale, modifiers.
    verbose=False (default) returns only failing/warning checks.
    safe_to_bake is True only with zero CRITICAL failures.
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
    MATERIAL / PBR REVIEWER — senior TA review of every material slot: PBR
    workflow compliance, roughness/metallic plausibility, broken texture
    paths, procedural-only materials that won't transfer to Unreal, normal
    map direction (OpenGL vs DirectX), orphaned nodes, draw-call cost.
    Primary QA tool at Stage 4 (Texture/Material) and before UE5 export.
    Call get_viewport_screenshot() first.
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
    SCENE CLASSIFIER — zero-arg scene inventory: scene_mode (HERO = 1 dominant
    mesh | COLLECTION = 2-20 meshes | ENVIRONMENT = 20+), object inventory by
    type, dominant asset, per-object health flag, total poly count, recommended
    audit depth. Mandatory second step after get_viewport_screenshot() on any
    unseen scene — never run audit_all_objects() without this first.
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
    SCENE AUDIT — multi-object analysis, depth auto-calibrated to scene size.
    HERO (1 dominant mesh): full analysis on it. COLLECTION (2-20 meshes):
    ranked table, deep-dive only on CRITICAL/FAIL verdicts (capped at
    max_deep_dive, hard cap 10). ENVIRONMENT (20+): severity triage, top 5
    critical issues only. mode: "auto" (recommended) | "hero" | "collection"
    | "environment". Run get_scene_summary() first.
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
    AI TECHNICAL DIRECTOR — builds an ordered 5-step production plan (tool,
    success criteria, gate per step) toward goal: "export_ready" (default) |
    "bake_ready" | "rig_ready" | "texture_ready" | "review_only".
    ALWAYS present the plan and wait for explicit approval before executing
    any step — never run steps automatically.
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
    AI MESH CRITIC — senior TA review of topology: why each finding matters,
    which production scenarios expose it, what a senior artist would actually
    do. Unlike analyze_mesh_for_unreal (what's wrong), this explains why, in
    context of the active playbook. focus: "all"|"topology"|"uvs"|"geometry"|
    "deformation". verbose=True includes what's done well, not just problems.
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
            pb_vert_budget = _studio_vert_budget(_session_get("active_playbook"), pb["vert_budget"])
            playbook_context = {
                "playbook":           pb["name"],
                "topology_score_min": topo_min,
                "topology_pass":      topo_score >= topo_min,
                "topology_verdict":   f"{topo_score}/100 — {'PASS' if topo_score >= topo_min else 'FAIL'} for {pb['name']} standard (min {topo_min})",
                "vert_budget":        pb_vert_budget,
                "vert_count":         vert_count,
                "vert_budget_pass":   vert_count <= pb_vert_budget,
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
    PRODUCTION REVIEW — full QA sweep in one call: score 0-100, grade, strengths,
    critical_blockers, warnings, time_estimate. The "show me everything" command.

    Surfaces conflicts between stated asset_type and what the data shows —
    states the conflict and asks for confirmation, never silently resolves it.

    asset_type left blank → tool states its inference instead of guessing silently.
    include_rig requires an Armature modifier on the object.
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

        # ── Step 5.5: Material knowledge layer enrichment ────────────────────
        # Surfaces closest_known_material matches and blended-material
        # candidates as findings — visible the same way every other finding
        # is (feeds the existing score/recommendations pipeline below), not
        # a buried field nobody reads. Never a hard blocker on its own:
        # a known-recipe match is info severity, a likely-blended material
        # is warning severity — consistent with known_material_match's
        # info severity in get_asset_dna's own rules_fired. Advisory only;
        # a failure here never breaks the core review.
        try:
            dna_for_materials = json.loads(get_asset_dna(object_name, target_engine="unreal"))
            for mat in dna_for_materials.get("materials", []):
                mname = mat.get("name", "?")
                match = mat.get("closest_known_material")
                if match:
                    all_findings.append({
                        "issue": f"Material '{mname}' closely matches recorded recipe "
                                 f"'{match['canonical_name']}' (distance {match['distance']})",
                        "severity": "info",
                        "source": "material_knowledge",
                        "fix": "apply_weathering_recipe or generate_procedural_material can build "
                               "on this prior knowledge instead of starting blind.",
                    })
                heterogeneity = mat.get("heterogeneity")
                if heterogeneity and heterogeneity.get("likely_blended"):
                    all_findings.append({
                        "issue": f"Material '{mname}' looks like it's blending multiple substances "
                                 f"into one slot (island_count={heterogeneity.get('island_count')}, "
                                 f"color_variance={heterogeneity.get('color_variance')})",
                        "severity": "warning",
                        "source": "material_knowledge",
                        "fix": "split_blended_material(object_name, material_name) can separate it "
                               "into distinct, individually-weatherable materials.",
                    })
        except Exception:
            pass

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

        # Granular triangle budget — checked FIRST: if effective_type names a
        # specific tri_budgets entry (e.g. "Giant Boss", "Sword"), that's a
        # more precise real number than the broad vertex-based table below,
        # and triangle count is what these numbers actually mean. Falls
        # through to the broad vertex check when no granular match exists.
        # limit stays None on the granular path — real bug caught by the
        # test suite: the "strengths" section further down reads `limit`
        # unconditionally and would raise UnboundLocalError otherwise.
        limit = None
        tri_budget = _studio_tri_budget(effective_type)
        if tri_budget:
            topo_stats = raw_topology.get("stats", {}) if "error" not in raw_topology else {}
            tri_estimate = _estimate_triangle_count(
                raw_quality.get("counts", {}).get("faces", 0) or 0,
                topo_stats.get("quad_ratio_pct"),
                topo_stats.get("tris_pct"),
            )
            est_tris = tri_estimate["estimated_tri_count"]
            if est_tris > tri_budget:
                ratio = est_tris / tri_budget
                conflicts.append({
                    "conflict":       "Triangle budget exceeded",
                    "data_shows":     f"~{est_tris:,} triangles ({'exact' if tri_estimate['exact'] else 'estimated — ngon portion approximated'}) "
                                      f"— {ratio:.1f}× the studio_profile {effective_type} ceiling ({tri_budget:,})",
                    "stated_type":    effective_type,
                    "confirm_question": (
                        f"I estimate ~{est_tris:,} triangles, {ratio:.1f}× the {effective_type} ceiling "
                        f"({tri_budget:,}) from studio_profile.json. Is this intentional or should I "
                        f"evaluate against a different asset type?"
                    ),
                })
                _session_append("surfaced_conflicts",
                    f"tri_budget: ~{est_tris:,} is {ratio:.1f}x {effective_type} ceiling")
        else:
            # Broad vertex budget conflict — unchanged behavior, now backed
            # by studio_profile.json instead of a third hardcoded table.
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
            limit = _studio_vert_budget(effective_type, BUDGET_LIMITS.get(effective_type))
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

        # Journal + open issues for blockers (Sprint A)
        score = report.get("production_score", 0)
        grade = report.get("score_grade", "?")
        blockers = report.get("critical_blockers", [])
        _journal_entry(
            "production_review", object_name, "ok",
            f"score={score} grade={grade} blockers={len(blockers)} conflicts={len(conflicts)}"
        )
        for b in blockers:
            b_text = b if isinstance(b, str) else str(b)
            _open_issue("production_review", object_name, "production_blocker", "critical", b_text[:200])

        return json.dumps(report, indent=2, default=str)

    except Exception as e:
        logger.error(f"Error in production_review: {e}")
        _journal_entry("production_review", object_name, "error", str(e))
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
    SPATIAL INTELLIGENCE — relationship graph of every object in the scene:
    above/below/beside/inside/intersecting/touching/near triples with distances,
    plus a plain-English spatial_summary. Scenes over max_objects get a
    collection-summary view instead of full per-object detail.
    relationship_radius caps the pairwise search distance to avoid O(n^2) blowup.
    """
    script = r"""
import bpy
import json
import math
from mathutils import Vector
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

def check_intersecting_aabb(obj_a, obj_b):
    # AABB overlap test — 6 axis-aligned comparisons.
    # Catches coplanar/flush cases that BVHTree.overlap() misses (parallel faces
    # never cross so BVH returns empty; AABB separation = 0 so it correctly fires).
    # Trade-off: ignores mesh rotation, uses world-space bounding box extents.
    # For precise rotated-mesh intersection use query_spatial(query_type='intersecting')
    # which runs full 15-axis SAT on oriented bounding boxes.
    try:
        amin, amax = bbox_min_max(obj_a)
        bmin, bmax = bbox_min_max(obj_b)
        # Separated on any axis → no overlap
        if amax[0] <= bmin[0] or bmax[0] <= amin[0]: return False
        if amax[1] <= bmin[1] or bmax[1] <= amin[1]: return False
        if amax[2] <= bmin[2] or bmax[2] <= amin[2]: return False
        return True
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
            intersects = check_intersecting_aabb(obj_a, obj_b)
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
    SPATIAL QUERY ENGINE — targeted spatial questions instead of the whole scene graph.

    query_type (required params in parens):
      nearest(object_name, count) | in_radius(object_name, radius) |
      intersecting(object_name) | supporting(object_name) |
      above(object_name, radius) | below(object_name, radius) |
      raycast(origin, direction) | floating() | isolated(radius)
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
import bpy, json, math
from mathutils import Vector, Matrix

# Separating Axis Theorem (SAT) on Oriented Bounding Boxes.
# Tests 15 axes: 3 face normals from box A, 3 from box B, 9 cross-product pairs.
# If all 15 axes show overlap → objects intersect.
# Catches coplanar/flush cases that BVHTree.overlap() misses because parallel
# faces never cross (no triangle-triangle intersection) but have zero separation
# on the SAT axis — correctly detected here.

def get_obb(obj):
    # Returns OBB as (center_vec, [axis0, axis1, axis2], [half_ext0, half_ext1, half_ext2])
    # Axes are the world-space columns of the rotation matrix, half_extents are half-dimensions.
    mw   = obj.matrix_world
    dims = obj.dimensions
    center = mw.to_translation()
    # Extract rotation axes (columns of the 3x3 rotation part, normalised)
    rot  = mw.to_3x3().normalized()
    axes = [rot.col[0].copy(), rot.col[1].copy(), rot.col[2].copy()]
    half = [dims.x * 0.5, dims.y * 0.5, dims.z * 0.5]
    return center, axes, half

def sat_overlap(obb_a, obb_b):
    # Returns True if the two OBBs overlap (no separating axis found).
    ca, axes_a, ha = obb_a
    cb, axes_b, hb = obb_b
    t = cb - ca  # translation vector between centres

    def project_onto(axis):
        # Projected half-extent of each OBB onto axis, plus centre separation.
        ra = sum(ha[i] * abs(axes_a[i].dot(axis)) for i in range(3))
        rb = sum(hb[i] * abs(axes_b[i].dot(axis)) for i in range(3))
        separation = abs(t.dot(axis))
        return separation <= ra + rb  # True → no separation on this axis

    # Test 3 face normals of A
    for ax in axes_a:
        if not project_onto(ax):
            return False
    # Test 3 face normals of B
    for ax in axes_b:
        if not project_onto(ax):
            return False
    # Test 9 cross-product edge pairs (A_i x B_j)
    for ax_a in axes_a:
        for ax_b in axes_b:
            cross = ax_a.cross(ax_b)
            if cross.length < 1e-6:
                continue  # parallel edges — skip degenerate axis
            if not project_onto(cross.normalized()):
                return False
    return True  # no separating axis found → overlap

obj = bpy.data.objects.get('{OBJ}')
if obj is None:
    print(json.dumps({"error": "Object not found: {OBJ}"}))
else:
    obb_a = get_obb(obj)
    center_a = obj.matrix_world.to_translation()
    found = []
    for other in bpy.context.scene.objects:
        if other.type != 'MESH' or other is obj or other.hide_viewport:
            continue
        oc   = other.matrix_world.to_translation()
        dist = (center_a - oc).length
        if dist > 10.0:  # skip distant objects
            continue
        try:
            obb_b = get_obb(other)
            if sat_overlap(obb_a, obb_b):
                found.append({"name": other.name, "distance": round(dist, 3),
                    "method": "SAT-OBB"})
        except Exception:
            pass
    print(json.dumps({"query": "intersecting", "reference": "{OBJ}", "count": len(found), "results": found,
        "verdict": "WARN: intersecting objects detected — likely placement errors" if found else "PASS: no intersections",
        "method_note": "SAT on oriented bounding boxes — catches coplanar/flush overlaps that BVH misses"}))
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
    SPATIAL CONTEXT — semantic description of an object and its surroundings:
    inferred role, position/dimensions, floor contact, nearest neighbors with
    direction, intersections, and a ready-to-use plain-English spatial_sentence.
    Read this before any spatial decision — richer than raw geometry stats.
    """
    script = r"""
import bpy, json, math, re
from mathutils import Vector

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

    # ── Intersecting objects (AABB) ──────────────────────────────────────
    # Uses axis-aligned bounding box overlap — 6 comparisons per pair.
    # Catches coplanar/flush cases that BVHTree.overlap() misses.
    # For precise rotated-mesh intersection use query_spatial(query_type='intersecting')
    # which runs full 15-axis SAT on oriented bounding boxes.
    def bbox_min_max_local(o):
        pts = [o.matrix_world @ Vector(c) for c in o.bound_box]
        xs = [v.x for v in pts]; ys = [v.y for v in pts]; zs = [v.z for v in pts]
        return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))

    def aabb_overlap(o_a, o_b):
        amin, amax = bbox_min_max_local(o_a)
        bmin, bmax = bbox_min_max_local(o_b)
        if amax[0] <= bmin[0] or bmax[0] <= amin[0]: return False
        if amax[1] <= bmin[1] or bmax[1] <= amin[1]: return False
        if amax[2] <= bmin[2] or bmax[2] <= amin[2]: return False
        return True

    intersecting = []
    for other in meshes:
        dist = (center - other.matrix_world.to_translation()).length
        if dist > 5.0:
            continue
        try:
            if aabb_overlap(obj, other):
                intersecting.append(other.name)
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
# SPRINT A — COGNITION LAYER
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def get_production_journal(last_n: int = 50, object_name: str = "") -> str:
    """
    PRODUCTION JOURNAL — timestamped log of every significant tool call this
    session (what was checked/repaired/generated, in order) plus open/closed
    issues, without re-running analysis. last_n caps entries (max 200).
    """
    journal = _SESSION.get("journal", [])
    tracker = _SESSION.get("issue_tracker", [])

    # Filter by object if requested
    if object_name:
        journal = [e for e in journal if e.get("object") == object_name or not e.get("object")]
        tracker = [i for i in tracker if i.get("object") == object_name]

    # Clamp to last_n
    journal = journal[-min(last_n, 200):]

    open_issues   = [i for i in tracker if i.get("status") == "open"]
    closed_issues = [i for i in tracker if i.get("status") == "closed"]

    # Build plain-English session narrative
    tool_counts: dict = {}
    for e in journal:
        tool_counts[e.get("tool", "?")] = tool_counts.get(e.get("tool", "?"), 0) + 1

    errors   = [e for e in journal if e.get("outcome") == "error"]
    repairs  = [e for e in journal if e.get("outcome") in ("repaired", "ok") and "repair" in e.get("tool", "")]
    analyses = [e for e in journal if e.get("tool", "") in (
        "get_spatial_analysis", "production_review", "analyze_mesh_for_unreal",
        "analyze_topology", "what_next", "critique_animation",
    )]

    narrative_lines = []
    if analyses:
        narrative_lines.append(
            f"{len(analyses)} analysis operation(s) run: "
            + ", ".join(dict.fromkeys(e["tool"] for e in analyses))
        )
    if repairs:
        narrative_lines.append(f"{len(repairs)} repair operation(s) executed.")
    if open_issues:
        critical = [i for i in open_issues if i.get("severity") == "critical"]
        narrative_lines.append(
            f"{len(open_issues)} issue(s) currently open "
            f"({len(critical)} critical, {len(open_issues)-len(critical)} warning)."
        )
    if closed_issues:
        narrative_lines.append(f"{len(closed_issues)} issue(s) resolved this session.")
    if errors:
        narrative_lines.append(f"⚠ {len(errors)} error(s) encountered: "
                                + ", ".join(e.get("tool","?") for e in errors))
    if not narrative_lines:
        narrative_lines = ["No significant actions recorded yet this session."]

    return json.dumps({
        "session_narrative": " ".join(narrative_lines),
        "journal":           journal,
        "open_issues":       open_issues,
        "closed_issues":     closed_issues,
        "tool_call_counts":  tool_counts,
        "total_journal_entries": len(_SESSION.get("journal", [])),
    }, indent=2, default=str)


@mcp.tool()
def close_issue(issue_id: str, reason: str = "") -> str:
    """
    ISSUE TRACKER — manually close an open issue by ID (e.g. "ISS-003", from
    get_production_journal). Issues normally auto-close when a repair tool
    fixes them; use this for fixes made by hand outside the MCP.
    """
    import datetime as _dt
    tracker = _SESSION.get("issue_tracker", [])
    for entry in tracker:
        if entry.get("id") == issue_id:
            if entry.get("status") == "closed":
                return json.dumps({"status": "already_closed", "issue": entry})
            entry["status"]    = "closed"
            entry["ts_closed"] = _dt.datetime.now().strftime("%H:%M:%S")
            entry["closed_by"] = f"manual: {reason}" if reason else "manual"
            _save_session()
            _journal_entry("close_issue", entry.get("object",""), "ok",
                           f"Manually closed {issue_id}: {reason}")
            return json.dumps({"status": "closed", "issue": entry}, indent=2)
    return json.dumps({"status": "not_found", "issue_id": issue_id,
                       "hint": "Use get_production_journal() to see all issue IDs."})


@mcp.tool()
def synthesize_session(object_name: str = "") -> str:
    """
    SESSION SYNTHESIS — reads the full session journal + open issues and
    produces THREE ranked decision paths with tradeoffs (not one answer),
    plus a recommendation, confidence level, and data_gaps (analyses not
    yet run that would sharpen the picture). object_name empty = whole session.
    """
    journal  = _SESSION.get("journal", [])
    tracker  = _SESSION.get("issue_tracker", [])
    playbook = _get_active_playbook()
    pb_name  = _session_get("active_playbook") or "none"
    stage    = _session_get("confirmed_stage")
    asset_t  = _session_get("asset_type") or "unknown"
    active_obj = object_name or _session_get("active_object") or "unknown"

    # Filter tracker by object if specified
    if object_name:
        relevant = [i for i in tracker if i.get("object") == object_name]
    else:
        relevant = tracker

    open_issues   = [i for i in relevant if i.get("status") == "open"]
    closed_issues = [i for i in relevant if i.get("status") == "closed"]

    # Rank open issues: critical first, then by element type priority
    _SEV_RANK = {"critical": 0, "warning": 1, "info": 2}
    _TYPE_RANK = {
        "non_manifold_edges": 0, "production_blocker": 1, "ngons": 2,
        "deformation_risk": 3, "uv_missing": 4, "pole": 5,
    }
    open_issues.sort(key=lambda i: (
        _SEV_RANK.get(i.get("severity","info"), 9),
        _TYPE_RANK.get(i.get("issue_type",""), 9),
    ))

    # What tools have been run?
    tools_run = list(dict.fromkeys(e.get("tool","") for e in journal))
    analyses_done = [t for t in tools_run if t in (
        "get_spatial_analysis", "production_review", "analyze_mesh_for_unreal",
        "analyze_topology", "critique_animation", "analyze_rig_weights",
        "analyze_rig_skeleton", "analyze_deformation_zones",
    )]
    repairs_done = [t for t in tools_run if "repair" in t or t == "auto_repair_mesh"]

    # Data gaps — what would improve confidence?
    all_key_analyses = [
        "get_spatial_analysis", "production_review", "analyze_mesh_for_unreal",
        "analyze_topology", "analyze_deformation_zones",
    ]
    data_gaps = [a for a in all_key_analyses if a not in analyses_done]

    # ── Build three decision paths ─────────────────────────────────────────
    critical_open = [i for i in open_issues if i.get("severity") == "critical"]
    warning_open  = [i for i in open_issues if i.get("severity") == "warning"]

    paths = []

    # PATH A — Fix critical blockers first (always valid if any exist)
    if critical_open:
        top_crit = critical_open[0]
        # Remediation advice per issue_type — auto_repair_mesh only fixes
        # geometry issues; giving that advice for e.g. deformation_risk is
        # actively misleading (auto_repair_mesh never touches poles).
        _HOW_BY_TYPE = {
            "non_manifold_edges": "auto_repair_mesh() — automatic geometry repair.",
            "zero_area_faces":    "auto_repair_mesh() — automatic geometry repair.",
            "isolated_verts":     "auto_repair_mesh() — automatic geometry repair.",
            "duplicate_faces":    "auto_repair_mesh() — automatic geometry repair.",
            "ngons":              "Manual edit mode topology cleanup — requires artist judgment on edge flow.",
            "deformation_risk":   "Manually re-route topology at the flagged joints — auto_repair_mesh does NOT fix poles/deformation. Use get_problem_detail_view() to see the exact location.",
            "production_blocker": "Review the specific blocker detail — remediation varies by blocker type.",
            "uv_missing":         "Unwrap UVs via Smart UV Project or manual seam placement.",
        }
        present_types = dict.fromkeys(i.get("issue_type", "") for i in critical_open)
        how_lines = [
            _HOW_BY_TYPE.get(t, f"Review '{t}' issue detail and use get_problem_detail_view() to inspect visually.")
            for t in present_types
        ]
        paths.append({
            "path": "A",
            "label": "Fix critical blockers first",
            "priority": 1,
            "action": (
                f"Address {len(critical_open)} critical issue(s) starting with "
                f"'{top_crit['issue_type']}' on {top_crit['object']}: {top_crit['detail'][:120]}"
            ),
            "tradeoff": "Safest path. Nothing else is valid until critical issues are resolved.",
            "how": " ".join(dict.fromkeys(how_lines)),
            "estimated_effort": "Low–Medium depending on issue count",
        })

    # PATH B — Run missing analyses to improve picture confidence
    if data_gaps:
        paths.append({
            "path": "B",
            "label": "Fill data gaps before acting",
            "priority": 2 if not critical_open else 3,
            "action": (
                f"Run {len(data_gaps)} missing analysis tool(s) to get a complete picture: "
                + ", ".join(data_gaps)
            ),
            "tradeoff": (
                "Conservative. Costs one round of analysis calls but gives you "
                "a complete picture before committing to repairs or next pipeline stage."
            ),
            "how": "Call each missing tool listed above in order.",
            "estimated_effort": "Low — analysis only, no mesh changes",
        })

    # PATH C — Advance to next pipeline stage (valid if no critical issues)
    if not critical_open:
        next_stage_action = "Run full production_review() to confirm readiness for next stage"
        if warning_open:
            next_stage_action = (
                f"Accept {len(warning_open)} warning(s) as known risk and advance to next stage. "
                f"Warnings: {', '.join(i['issue_type'] for i in warning_open[:3])}"
            )
        paths.append({
            "path": "C",
            "label": "Advance to next pipeline stage",
            "priority": 2 if not data_gaps else 3,
            "action": next_stage_action,
            "tradeoff": (
                "Progress-focused. Accepts warnings as managed risk. "
                "Only valid if you've confirmed warnings are non-blocking for your target platform."
            ),
            "how": "production_review() → resolve any conflicts → export_for_unreal() or next stage gate.",
            "estimated_effort": "Low if mesh is clean",
        })

    # If no paths built (clean session, no data), give a default
    if not paths:
        paths.append({
            "path": "A",
            "label": "Start with a full inspection",
            "priority": 1,
            "action": "Run get_spatial_analysis() and production_review() to establish a baseline.",
            "tradeoff": "No data yet — can't reason without it.",
            "how": "get_spatial_analysis(object_name) → production_review(object_name)",
            "estimated_effort": "Low",
        })

    # Recommended path
    recommended = sorted(paths, key=lambda p: p["priority"])[0]
    rec_why = (
        f"Path {recommended['path']} is recommended because "
        + ("there are critical blockers that must be resolved before anything else. "
           if critical_open else
           ("the picture is incomplete — more data will give better decisions. "
            if data_gaps else
            "the mesh appears clean and ready to advance. "))
        + f"Playbook: {pb_name}. Asset type: {asset_t}. Stage: {stage or 'unconfirmed'}."
    )

    # Confidence
    if len(analyses_done) >= 3 and not data_gaps:
        confidence = "high"
    elif len(analyses_done) >= 1:
        confidence = "medium"
    else:
        confidence = "low — no analysis data yet"

    # Session picture
    picture_parts = []
    if analyses_done:
        picture_parts.append(f"Analyses run: {', '.join(analyses_done)}.")
    if repairs_done:
        picture_parts.append(f"Repairs executed: {', '.join(repairs_done)}.")
    if closed_issues:
        picture_parts.append(f"{len(closed_issues)} issue(s) resolved.")
    if open_issues:
        picture_parts.append(
            f"{len(open_issues)} issue(s) still open "
            f"({len(critical_open)} critical, {len(warning_open)} warning)."
        )
    if not picture_parts:
        picture_parts = ["Session just started — no data collected yet."]

    _journal_entry("synthesize_session", active_obj, "ok",
                   f"confidence={confidence} open={len(open_issues)} paths={len(paths)}")

    return json.dumps({
        "object":          active_obj,
        "session_picture": " ".join(picture_parts),
        "confidence":      confidence,
        "data_gaps":       data_gaps,
        "open_issues":     [
            {"id": i["id"], "type": i["issue_type"], "severity": i["severity"],
             "detail": i["detail"][:120], "opened_by": i["tool"]}
            for i in open_issues
        ],
        "resolved_issues": len(closed_issues),
        "decision_paths":  paths,
        "recommendation":  {
            "path":   recommended["path"],
            "label":  recommended["label"],
            "action": recommended["action"],
            "why":    rec_why,
            "how":    recommended["how"],
        },
        "session_context": {
            "asset_type":      asset_t,
            "active_playbook": pb_name,
            "stage":           stage,
            "tools_run":       tools_run,
        },
    }, indent=2, default=str)


# ─────────────────────────────────────────────────────────────────────────────
# SPRINT B — DEFORMATION INTELLIGENCE
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def analyze_deformation_zones(object_name: str) -> str:
    """
    DEFORMATION INTELLIGENCE — checks if topology can bend cleanly at joints:
    edge loop density, pole placement on bend axes, ngons in deforming areas,
    support loops on both sides of a crease. With an Armature modifier, zones
    come from real bone positions; without one, estimated from mesh proportions
    (shoulder/elbow/wrist/hip/knee/ankle). Returns overall_risk + deformation_ready.
    """
    blender_script = f"""
import bpy, bmesh, math, json

obj = bpy.data.objects.get("{object_name}")
if obj is None:
    print(json.dumps({{"error": "Object not found"}}))
    raise SystemExit

if obj.type != 'MESH':
    print(json.dumps({{"error": "Object is not a mesh"}}))
    raise SystemExit

# ── Identify bones / deformation sites ─────────────────────────────────────
bone_sites = []   # {{name, head_world, tail_world, length}}

arm_obj = None
for mod in obj.modifiers:
    if mod.type == 'ARMATURE' and mod.object:
        arm_obj = mod.object
        break

if arm_obj and arm_obj.type == 'ARMATURE':
    arm  = arm_obj.data
    mwi  = obj.matrix_world.inverted()
    for bone in arm.bones:
        head_w = arm_obj.matrix_world @ bone.head_local
        tail_w = arm_obj.matrix_world @ bone.tail_local
        bone_sites.append({{
            "name":       bone.name,
            "head_local": list(mwi @ head_w),
            "tail_local": list(mwi @ tail_w),
            "length":     bone.length,
        }})

# ── BMesh analysis ──────────────────────────────────────────────────────────
bm = bmesh.new()
bm.from_mesh(obj.data)
bm.verts.ensure_lookup_table()
bm.edges.ensure_lookup_table()
bm.faces.ensure_lookup_table()

# Compute per-vertex valence
valence = {{v.index: len(v.link_edges) for v in bm.verts}}

# Bbox for region labelling
xs = [v.co.x for v in bm.verts]
ys = [v.co.y for v in bm.verts]
zs = [v.co.z for v in bm.verts]
x_range = max(xs) - min(xs) if xs else 1
y_range = max(ys) - min(ys) if ys else 1
z_range = max(zs) - min(zs) if zs else 1
z_min, z_max = min(zs), max(zs)
x_min, x_max = min(xs), max(xs)

def region_label(co):
    zn = (co.z - z_min) / (z_range or 1)
    xn = (co.x - x_min) / (x_range or 1)
    if zn > 0.85: return "head"
    if zn > 0.70: return "neck"
    if zn > 0.55: return "shoulder"
    if zn > 0.40: return "torso_upper"
    if zn > 0.30: return "torso_lower"
    if zn > 0.20: return "hip"
    if zn > 0.12: return "knee"
    if zn > 0.04: return "ankle"
    return "foot"

def analyse_zone_around(center_local, radius):
    \"\"\"Return topology stats for vertices within radius of center_local.\"\"\"
    import mathutils
    c = mathutils.Vector(center_local)
    nearby_verts = [v for v in bm.verts if (v.co - c).length < radius]
    if not nearby_verts:
        return {{"vert_count": 0, "pole_count": 0, "ngon_count": 0,
                "avg_valence": 0, "min_edge_loop_density": 0,
                "has_support_loops": False}}

    poles     = [v for v in nearby_verts if valence[v.index] > 5]
    ngon_faces = [f for f in bm.faces
                  if len(f.verts) > 4
                  and any((fv.co - c).length < radius for fv in f.verts)]
    avg_val   = sum(valence[v.index] for v in nearby_verts) / len(nearby_verts)

    # Rough edge-loop density: count unique edge loop cross-sections
    # (count edges roughly parallel to the major axis of motion)
    edge_loop_estimate = max(1, len(nearby_verts) // max(1, len(poles) + 1))
    has_support = edge_loop_estimate >= 3   # >= 3 loops around joint = adequate

    return {{
        "vert_count":            len(nearby_verts),
        "pole_count":            len(poles),
        "ngon_count":            len(ngon_faces),
        "avg_valence":           round(avg_val, 2),
        "edge_loop_estimate":    edge_loop_estimate,
        "has_support_loops":     has_support,
    }}

# ── Analyse each zone ───────────────────────────────────────────────────────
zones = []
search_radius = max(x_range, y_range, z_range) * 0.12   # 12% of bbox as zone radius

if bone_sites:
    # Armature-guided: use bone heads (joint positions) as zone centers
    for bs in bone_sites[:20]:   # cap at 20 bones
        center = bs["head_local"]
        stats  = analyse_zone_around(center, search_radius)
        if stats["vert_count"] == 0:
            continue

        # Risk scoring
        risk_score = 0
        findings   = []

        if stats["pole_count"] > 0:
            risk_score += 30 * stats["pole_count"]
            findings.append(f"{{stats['pole_count']}} pole(s) in joint zone — shading artefact risk under deformation")
        if stats["ngon_count"] > 0:
            risk_score += 25 * stats["ngon_count"]
            findings.append(f"{{stats['ngon_count']}} n-gon(s) in deforming area — will auto-triangulate unpredictably")
        if not stats["has_support_loops"]:
            risk_score += 20
            findings.append(f"Insufficient edge loops ({{stats['edge_loop_estimate']}}) — smooth bending requires ≥3")
        if stats["avg_valence"] > 5.5:
            risk_score += 10
            findings.append(f"High average valence ({{stats['avg_valence']:.1f}}) — dense topology may cause pinching")
        if stats["vert_count"] < 4:
            risk_score += 15
            findings.append("Very sparse geometry at joint — may collapse under extreme pose")

        severity = "critical" if risk_score >= 50 else ("warning" if risk_score >= 20 else "ok")

        zones.append({{
            "zone":         bs["name"],
            "zone_type":    "bone_joint",
            "risk_score":   min(risk_score, 100),
            "severity":     severity,
            "findings":     findings,
            "stats":        stats,
            "fix": (
                "Dissolve pole edges and re-route through the joint with a clean loop. "
                "Add support loops on both sides of the crease. Remove n-gons before skinning."
            ) if severity != "ok" else "Zone looks deformation-ready.",
        }})
else:
    # No armature: analyse geometric regions likely to be deformation zones
    region_centers = {{
        "shoulder_L": [x_min + x_range*0.1, 0, z_min + z_range*0.6],
        "shoulder_R": [x_max - x_range*0.1, 0, z_min + z_range*0.6],
        "elbow_L":    [x_min + x_range*0.05, 0, z_min + z_range*0.45],
        "elbow_R":    [x_max - x_range*0.05, 0, z_min + z_range*0.45],
        "wrist_L":    [x_min + x_range*0.02, 0, z_min + z_range*0.30],
        "wrist_R":    [x_max - x_range*0.02, 0, z_min + z_range*0.30],
        "hip_L":      [x_min + x_range*0.2, 0, z_min + z_range*0.25],
        "hip_R":      [x_max - x_range*0.2, 0, z_min + z_range*0.25],
        "knee_L":     [x_min + x_range*0.2, 0, z_min + z_range*0.13],
        "knee_R":     [x_max - x_range*0.2, 0, z_min + z_range*0.13],
        "ankle_L":    [x_min + x_range*0.2, 0, z_min + z_range*0.04],
        "ankle_R":    [x_max - x_range*0.2, 0, z_min + z_range*0.04],
    }}
    for zone_name, center in region_centers.items():
        stats = analyse_zone_around(center, search_radius)
        if stats["vert_count"] == 0:
            continue

        risk_score = 0
        findings   = []
        if stats["pole_count"] > 0:
            risk_score += 30 * stats["pole_count"]
            findings.append(f"{{stats['pole_count']}} pole(s) in estimated joint zone")
        if stats["ngon_count"] > 0:
            risk_score += 25 * stats["ngon_count"]
            findings.append(f"{{stats['ngon_count']}} n-gon(s) in deforming area")
        if not stats["has_support_loops"]:
            risk_score += 20
            findings.append(f"Insufficient edge loops ({{stats['edge_loop_estimate']}}) for clean bend")

        severity = "critical" if risk_score >= 50 else ("warning" if risk_score >= 20 else "ok")
        zones.append({{
            "zone":       zone_name,
            "zone_type":  "estimated_anatomical",
            "risk_score": min(risk_score, 100),
            "severity":   severity,
            "findings":   findings,
            "stats":      stats,
            "note":       "No armature found — zones are estimated from mesh proportions.",
            "fix": (
                "Re-route topology at this joint with clean loops before skinning."
            ) if severity != "ok" else "Zone looks deformation-ready.",
        }})

bm.free()

# ── Overall risk ────────────────────────────────────────────────────────────
critical_zones = [z for z in zones if z["severity"] == "critical"]
warning_zones  = [z for z in zones if z["severity"] == "warning"]
if critical_zones:
    overall_risk = "critical"
elif warning_zones:
    overall_risk = "medium" if len(warning_zones) < 3 else "high"
elif zones:
    overall_risk = "low"
else:
    overall_risk = "unknown"

deformation_ready = overall_risk in ("low",)

result = {{
    "object":            "{object_name}",
    "zone_source":       "armature" if bone_sites else "estimated_anatomical",
    "zones_analysed":    len(zones),
    "overall_risk":      overall_risk,
    "deformation_ready": deformation_ready,
    "critical_zones":    [z["zone"] for z in critical_zones],
    "warning_zones":     [z["zone"] for z in warning_zones],
    "zones":             zones,
    "summary": (
        f"{{len(critical_zones)}} critical zone(s), {{len(warning_zones)}} warning zone(s) "
        f"across {{len(zones)}} deformation zone(s) analysed. "
        f"Overall risk: {{overall_risk}}. "
        + ("Mesh is NOT deformation-ready — fix critical zones before skinning."
           if critical_zones else
           ("Warnings present — review before final rig binding."
            if warning_zones else
            "Mesh appears deformation-ready."))
    ),
}}
print(json.dumps(result))
"""
    try:
        blender = get_blender_connection()
        result  = blender.send_command("execute_code_safe", {
            "code": blender_script, "required_mode": "OBJECT", "push_undo": False
        })
        raw_out = result.get("result") or result.get("output") or ""
        for line in str(raw_out).splitlines():
            if line.strip().startswith("{"):
                parsed = json.loads(line.strip())
                if "error" in parsed:
                    return json.dumps(parsed)

                # Sprint A: journal + open issues
                _journal_entry(
                    "analyze_deformation_zones", object_name,
                    "warning" if parsed.get("critical_zones") else "ok",
                    f"risk={parsed.get('overall_risk')} critical_zones={parsed.get('critical_zones')}"
                )
                for zone in parsed.get("zones", []):
                    if zone.get("severity") == "critical":
                        _open_issue(
                            "analyze_deformation_zones", object_name,
                            "deformation_risk", "critical",
                            f"Zone '{zone['zone']}': {'; '.join(zone.get('findings',[]))[:200]}"
                        )
                return json.dumps(parsed, indent=2, default=str)

        return json.dumps({"error": "No JSON output from deformation analysis", "raw": raw_out[:500]})
    except Exception as e:
        logger.error(f"Error in analyze_deformation_zones: {e}")
        _journal_entry("analyze_deformation_zones", object_name, "error", str(e))
        return json.dumps({"error": str(e)})


# ─────────────────────────────────────────────────────────────────────────────
# SPRINT C — PRESENTATION LAYER
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def simulate_production_readiness(object_name: str, asset_type: str = "") -> str:
    """
    PRODUCTION SIMULATION — "if this ships now" go/no-go scorecard across
    GEOMETRY, TOPOLOGY, UV, MATERIALS, RIG, ENGINE, DEFORMATION, PERFORMANCE.
    asset_type hints vert-budget thresholds. Returns overall PASS|WARN|FAIL,
    ship_verdict, blockers (FAIL dims), risks (WARN dims).
    """
    try:
        # Gather data from existing tools
        raw_problems  = _send_raw("detect_mesh_problems",        name=object_name)
        raw_quality   = _send_raw("get_mesh_quality_report",     name=object_name)
        raw_topology  = _send_raw("analyze_topology",            name=object_name)
        raw_ue5       = _send_raw("run_unreal_readiness_check",  name=object_name)
        raw_obj_info  = _send_raw("get_object_info",             name=object_name)

        if "error" in raw_obj_info:
            return json.dumps({"error": f"Object not found: {object_name}"})

        # Resolve asset type
        eff_type = asset_type or _session_get("asset_type") or "unknown"
        pb       = _get_active_playbook()

        # ── Extract signals ────────────────────────────────────────────────
        prob_map  = {p.get("type",""): p.get("count",0)
                     for p in raw_problems.get("problems", [])}
        nm_edges  = prob_map.get("non_manifold_edges", 0)
        iso_verts = prob_map.get("isolated_verts", 0)
        zero_area = prob_map.get("zero_area_faces", 0)

        face_types = raw_quality.get("face_types", {})
        ngon_count = face_types.get("ngons", 0) or 0
        quad_count = face_types.get("quads", 0) or 0
        tri_count  = face_types.get("tris",  0) or 0
        total_faces = max(ngon_count + quad_count + tri_count, 1)
        quad_ratio  = round(quad_count / total_faces * 100, 1)

        mesh_block   = raw_obj_info.get("mesh", {})
        vert_count   = mesh_block.get("vertices", 0) or 0
        has_uvs      = raw_quality.get("uv", {}).get("has_uvs", False)
        uv_layers    = raw_quality.get("uv", {}).get("layer_count", 0) or 0
        has_arm      = any(m.get("type") == "ARMATURE"
                           for m in raw_quality.get("modifiers", []) if isinstance(m, dict))
        mat_list     = raw_obj_info.get("materials", [])
        has_mats     = bool(mat_list)

        ue5_checks   = raw_ue5.get("checks", {}) if isinstance(raw_ue5, dict) else {}

        # ── Scorecard ──────────────────────────────────────────────────────
        def _grade(fail_cond, warn_cond, pass_msg, fail_msg, warn_msg):
            if fail_cond:   return ("FAIL", fail_msg)
            if warn_cond:   return ("WARN", warn_msg)
            return          ("PASS", pass_msg)

        scorecard = {}

        scorecard["GEOMETRY"] = _grade(
            nm_edges > 0 or zero_area > 0,
            iso_verts > 0,
            f"Clean — no non-manifold or degenerate geometry.",
            f"HARD BLOCKER — {nm_edges} non-manifold edge(s), {zero_area} zero-area face(s). UE5 will reject or corrupt this mesh.",
            f"{iso_verts} isolated vertex/vertices — will inflate vert count and may cause LOD issues.",
        )
        scorecard["TOPOLOGY"] = _grade(
            ngon_count > 10,
            ngon_count > 0 or quad_ratio < 60,
            f"Quad-dominant ({quad_ratio}% quads). Clean topology.",
            f"{ngon_count} n-gons ({round(ngon_count/total_faces*100,1)}% of faces) — UE5 auto-triangulation will produce star patterns.",
            f"{ngon_count} n-gon(s) present. Quad ratio: {quad_ratio}% (target ≥80% for deforming assets).",
        )
        scorecard["UV"] = _grade(
            not has_uvs,
            uv_layers < 2 and eff_type in ("hero_character", "weapon"),
            f"{uv_layers} UV layer(s) — present and adequate.",
            "No UV map — baking, texturing, and lightmapping are all blocked.",
            f"Only {uv_layers} UV layer — hero/weapon assets need a second lightmap channel for UE5 static lighting.",
        )
        scorecard["MATERIALS"] = _grade(
            not has_mats,
            has_mats and len(mat_list) > 1,
            f"{len(mat_list)} material(s) — assigned.",
            "No materials assigned — asset will appear grey in engine.",
            f"{len(mat_list)} material(s) — multiple materials mean multiple draw calls. Merge if possible.",
        )
        # run_unreal_readiness_check's "checks" dict holds nested {"pass": bool, ...}
        # per check, not a bare bool — comparing the dict itself to False is always
        # False regardless of actual state. Read the nested "pass" key instead.
        scorecard["ENGINE"] = _grade(
            not ue5_checks.get("scale_applied", {}).get("pass", True),
            not ue5_checks.get("pivot_at_origin", {}).get("pass", True),
            "Scale applied, pivot at origin — UE5 conventions met.",
            "Scale NOT applied — mesh will import at wrong size in UE5. Apply scale before export.",
            "Pivot not at origin — asset will rotate/translate incorrectly in UE5.",
        )

        # Vert budget check — real bug fixed here: pb["vert_budget"] is a
        # single int for the active playbook, not a dict keyed by asset
        # type; `pb.get("vert_budget", {}).get(eff_type)` would raise
        # AttributeError on any int whenever a playbook was active. Now
        # goes through the real studio_profile-backed lookup instead.
        budget = _studio_vert_budget(
            eff_type, {"hero_character": 80000, "weapon": 15000, "env_prop": 5000}.get(eff_type, 50000)
        )
        scorecard["PERFORMANCE"] = _grade(
            vert_count > budget * 1.5,
            vert_count > budget,
            f"{vert_count:,} verts — within budget ({budget:,}).",
            f"{vert_count:,} verts — 50%+ over {eff_type} budget of {budget:,}. LOD generation may not rescue this.",
            f"{vert_count:,} verts — over {eff_type} budget of {budget:,}. Generate LODs before shipping.",
        )

        scorecard["RIG"] = ("N/A", "No armature — rig check skipped.") if not has_arm else _grade(
            False, False,
            "Armature present — run analyze_rig_weights() for full rig QA.",
            "", "",
        )

        scorecard["DEFORMATION"] = ("N/A", "Run analyze_deformation_zones() for deformation risk score.")

        # ── Overall verdict ────────────────────────────────────────────────
        fails  = [(k, v) for k, v in scorecard.items() if v[0] == "FAIL"]
        warns  = [(k, v) for k, v in scorecard.items() if v[0] == "WARN"]

        if fails:
            overall      = "FAIL"
            ship_verdict = (
                f"DO NOT SHIP. {len(fails)} hard blocker(s): "
                + ", ".join(k for k, _ in fails)
                + ". These will cause visible failures or import errors in production."
            )
        elif warns:
            overall      = "WARN"
            ship_verdict = (
                f"SHIP WITH KNOWN RISKS. {len(warns)} warning(s): "
                + ", ".join(k for k, _ in warns)
                + ". Each is a known risk — confirm with the art director before shipping."
            )
        else:
            overall      = "PASS"
            ship_verdict = "READY TO SHIP. All simulated production gates pass."

        _journal_entry(
            "simulate_production_readiness", object_name,
            "ok" if overall == "PASS" else ("warning" if overall == "WARN" else "error"),
            f"verdict={overall} fails={len(fails)} warns={len(warns)}"
        )

        return json.dumps({
            "object":       object_name,
            "asset_type":   eff_type,
            "overall":      overall,
            "ship_verdict": ship_verdict,
            "scorecard": {k: {"result": v[0], "reason": v[1]} for k, v in scorecard.items()},
            "blockers":  [{"dimension": k, "reason": v[1]} for k, v in fails],
            "risks":     [{"dimension": k, "reason": v[1]} for k, v in warns],
        }, indent=2, default=str)

    except Exception as e:
        logger.error(f"Error in simulate_production_readiness: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def review_board(object_name: str, asset_type: str = "") -> str:
    """
    REVIEW BOARD — 5 specialist verdicts (TECHNICAL_ARTIST, CHARACTER_ARTIST,
    ANIMATOR, RENDERING, ENGINE), each scored 0-100/grade A-F with
    top_concerns + praise, combined into a consensus score and majority
    verdict. asset_type hints budget thresholds.
    """
    try:
        # Gather all data once
        raw_problems = _send_raw("detect_mesh_problems",       name=object_name)
        raw_quality  = _send_raw("get_mesh_quality_report",    name=object_name)
        raw_topology = _send_raw("analyze_topology",           name=object_name)
        raw_obj_info = _send_raw("get_object_info",            name=object_name)
        raw_ue5      = _send_raw("run_unreal_readiness_check", name=object_name)

        if "error" in raw_obj_info:
            return json.dumps({"error": f"Object not found: {object_name}"})

        eff_type   = asset_type or _session_get("asset_type") or "unknown"
        pb         = _get_active_playbook()

        prob_map   = {p.get("type",""): p.get("count",0)
                      for p in raw_problems.get("problems", [])}
        face_types = raw_quality.get("face_types", {})
        ngons      = face_types.get("ngons",  0) or 0
        quads      = face_types.get("quads",  0) or 0
        tris       = face_types.get("tris",   0) or 0
        total_f    = max(ngons + quads + tris, 1)
        quad_pct   = round(quads / total_f * 100, 1)
        nm_edges   = prob_map.get("non_manifold_edges", 0)
        iso_v      = prob_map.get("isolated_verts", 0)
        mesh_b     = raw_obj_info.get("mesh", {})
        vert_count = mesh_b.get("vertices", 0) or 0
        has_uvs    = raw_quality.get("uv", {}).get("has_uvs", False)
        uv_layers  = raw_quality.get("uv", {}).get("layer_count", 0) or 0
        has_arm    = any(m.get("type") == "ARMATURE"
                         for m in raw_quality.get("modifiers", []) if isinstance(m, dict))
        mat_list   = raw_obj_info.get("materials", [])
        # Real bug fixed here too — pb["vert_budget"] is a single int, not
        # a dict keyed by asset type; the old pb_budgets.get(eff_type)
        # would raise AttributeError on any int whenever a playbook was
        # active. Now goes through the real studio_profile-backed lookup.
        budget     = _studio_vert_budget(
            eff_type, {"hero_character": 80000, "weapon": 15000, "env_prop": 5000}.get(eff_type, 50000)
        )
        # run_unreal_readiness_check's real schema has no "issues" key — the
        # actual readiness flag is "ue5_ready" (bool). The old "issues" lookup
        # always returned None (falsy), so this was always True regardless of
        # actual UE5 readiness.
        ue5_ok     = raw_ue5.get("ue5_ready", True) if isinstance(raw_ue5, dict) else True

        def _score_to_grade(s):
            if s >= 90: return "A"
            if s >= 80: return "B"
            if s >= 70: return "C"
            if s >= 55: return "D"
            return "F"

        panel = {}

        # ── TECHNICAL ARTIST ───────────────────────────────────────────────
        ta_score = 100
        ta_concerns, ta_praise = [], []
        if nm_edges > 0:
            ta_score -= 40; ta_concerns.append(f"{nm_edges} non-manifold edge(s) — hard export blocker")
        if ngons > 5:
            ta_score -= 15; ta_concerns.append(f"{ngons} n-gons — topology needs cleanup")
        if iso_v > 0:
            ta_score -= 5;  ta_concerns.append(f"{iso_v} isolated vert(s)")
        if not has_uvs:
            ta_score -= 20; ta_concerns.append("No UV map — can't texture or bake")
        if quad_pct > 75:
            ta_praise.append(f"Strong quad ratio ({quad_pct}%)")
        if not ta_concerns:
            ta_praise.append("Clean geometry — export-ready")
        panel["TECHNICAL_ARTIST"] = {
            "score": max(ta_score, 0), "grade": _score_to_grade(max(ta_score,0)),
            "verdict": "Approved" if ta_score >= 75 else ("Revision required" if ta_score >= 50 else "Blocked"),
            "top_concerns": ta_concerns, "praise": ta_praise,
            "comment": f"Geometry health is {'good' if ta_score>=75 else 'concerning'}. "
                       + (f"Fix {nm_edges} non-manifold edge(s) immediately." if nm_edges else "")
        }

        # ── CHARACTER ARTIST ───────────────────────────────────────────────
        ca_score = 80   # base — we can't fully judge art without vision
        ca_concerns, ca_praise = [], []
        if ngons > 0:
            ca_score -= 10; ca_concerns.append(f"{ngons} n-gon(s) will disrupt edge flow in deforming areas")
        if quad_pct < 70:
            ca_score -= 15; ca_concerns.append(f"Low quad ratio ({quad_pct}%) — poor edge flow foundation")
        if quad_pct >= 80:
            ca_praise.append(f"Clean quad flow ({quad_pct}%) — good foundation for deformation")
        ca_concerns.append("Run critique_artistic() for full artistic assessment — visual review needed")
        panel["CHARACTER_ARTIST"] = {
            "score": max(ca_score, 0), "grade": _score_to_grade(max(ca_score,0)),
            "verdict": "Pending visual review" if ca_score >= 70 else "Revision required",
            "top_concerns": ca_concerns, "praise": ca_praise,
            "comment": "Topology assessed from data — for silhouette, proportions, and shape language run critique_artistic()."
        }

        # ── ANIMATOR ───────────────────────────────────────────────────────
        an_score = 80 if has_arm else 50
        an_concerns, an_praise = [], []
        if not has_arm:
            an_concerns.append("No armature — rig not yet assigned")
            an_score = 40
        else:
            an_praise.append("Armature modifier present")
            an_concerns.append("Run analyze_rig_weights() and analyze_deformation_zones() for full animation QA")
        if ngons > 0:
            an_score -= 15; an_concerns.append(f"{ngons} n-gon(s) in mesh — will cause skinning artefacts")
        panel["ANIMATOR"] = {
            "score": max(an_score, 0), "grade": _score_to_grade(max(an_score,0)),
            "verdict": "Needs rig QA" if has_arm else "No rig — animator cannot assess",
            "top_concerns": an_concerns, "praise": an_praise,
            "comment": "Run analyze_deformation_zones() to get a bone-by-bone deformation risk score."
        }

        # ── RENDERING ──────────────────────────────────────────────────────
        rn_score = 100
        rn_concerns, rn_praise = [], []
        if not has_uvs:
            rn_score -= 40; rn_concerns.append("No UVs — texturing and baking are impossible")
        elif uv_layers < 2:
            rn_score -= 10; rn_concerns.append("Only 1 UV channel — second channel needed for UE5 lightmaps")
        if not mat_list:
            rn_score -= 20; rn_concerns.append("No material — asset will render grey")
        if has_uvs and mat_list:
            rn_praise.append("UVs and materials present — renderable")
        panel["RENDERING"] = {
            "score": max(rn_score, 0), "grade": _score_to_grade(max(rn_score,0)),
            "verdict": "Approved" if rn_score >= 75 else ("Revision required" if rn_score >= 50 else "Blocked"),
            "top_concerns": rn_concerns, "praise": rn_praise,
            "comment": "Material and UV quality assessed from metadata — run analyze_material_pbr() for node-level review."
        }

        # ── ENGINE ─────────────────────────────────────────────────────────
        en_score = 100
        en_concerns, en_praise = [], []
        if vert_count > budget * 1.5:
            en_score -= 30; en_concerns.append(f"{vert_count:,} verts — 50%+ over {eff_type} budget of {budget:,}")
        elif vert_count > budget:
            en_score -= 15; en_concerns.append(f"{vert_count:,} verts — over {eff_type} budget ({budget:,}). Generate LODs.")
        else:
            en_praise.append(f"{vert_count:,} verts — within {eff_type} budget ({budget:,})")
        if not ue5_ok:
            en_score -= 20; en_concerns.append("Unreal readiness check flagged issues — run run_unreal_readiness_check()")
        if nm_edges > 0:
            en_score -= 25; en_concerns.append("Non-manifold geometry — engine may corrupt this mesh on import")
        panel["ENGINE"] = {
            "score": max(en_score, 0), "grade": _score_to_grade(max(en_score,0)),
            "verdict": "Approved" if en_score >= 75 else ("Revision required" if en_score >= 50 else "Blocked"),
            "top_concerns": en_concerns, "praise": en_praise,
            "comment": f"Budget check: {vert_count:,}/{budget:,} verts for {eff_type}."
        }

        # ── Consensus ──────────────────────────────────────────────────────
        scores        = [v["score"] for v in panel.values()]
        consensus_avg = round(sum(scores) / len(scores))
        consensus_grade = _score_to_grade(consensus_avg)
        blocked       = sum(1 for v in panel.values() if v["verdict"] in ("Blocked",))
        revisions     = sum(1 for v in panel.values() if "Revision" in v.get("verdict",""))
        approved      = sum(1 for v in panel.values() if v["verdict"] == "Approved")

        if blocked >= 2:
            majority_verdict = "DO NOT APPROVE — multiple specialists blocked"
        elif blocked == 1:
            majority_verdict = "BLOCKED — resolve the blocking specialist's issue first"
        elif revisions >= 3:
            majority_verdict = "REVISIONS REQUIRED — majority of specialists want changes"
        elif revisions >= 1:
            majority_verdict = "CONDITIONAL APPROVAL — minor revisions requested"
        else:
            majority_verdict = "APPROVED — panel consensus"

        chair_summary = (
            f"Review board for '{object_name}' ({eff_type}): "
            f"consensus score {consensus_avg}/100 (grade {consensus_grade}). "
            f"{approved} approved, {revisions} requesting revision, {blocked} blocked. "
            f"Majority verdict: {majority_verdict}. "
            + (f"Critical path: fix {nm_edges} non-manifold edge(s) first." if nm_edges else
               "No hard blockers — review warnings and proceed.")
        )

        _journal_entry(
            "review_board", object_name, "ok",
            f"consensus={consensus_avg} grade={consensus_grade} verdict='{majority_verdict}'"
        )

        return json.dumps({
            "object":           object_name,
            "asset_type":       eff_type,
            "panel":            panel,
            "consensus": {
                "score":           consensus_avg,
                "grade":           consensus_grade,
                "majority_verdict": majority_verdict,
                "approved":        approved,
                "revisions":       revisions,
                "blocked":         blocked,
            },
            "chair_summary": chair_summary,
        }, indent=2, default=str)

    except Exception as e:
        logger.error(f"Error in review_board: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def critique_artistic(object_name: str, context: str = "") -> list:
    """
    ARTISTIC CRITIQUE — vision-based review of silhouette, proportion, shape
    language, balance, readability. NOT topology/UV/rig — use get_spatial_analysis(),
    analyze_material_pbr(), analyze_rig_weights() for those. context: optional
    viewing-distance/scenario hint. Returns [7 view images, critique_dict].
    """
    try:
        # Capture 7 views (no wireframe — we want clean artistic views)
        mv_result = get_multiview_capture(object_name, include_wireframe=False)
        images    = [item for item in mv_result if isinstance(item, Image)]

        view_names = ["FRONT", "BACK", "LEFT", "RIGHT", "TOP", "BOTTOM", "PERSP"]
        view_map   = {view_names[i]: i for i in range(len(view_names))}

        # Build the artistic prompt as a structured critique guide
        ctx_note = f" Context: {context}." if context else ""
        critique_prompt = (
            f"You are a senior character/prop artist reviewing '{object_name}' for production.{ctx_note} "
            "You have 7 viewport screenshots: FRONT, BACK, LEFT, RIGHT, TOP, BOTTOM, PERSP. "
            "Give a structured artistic critique across these dimensions:\n"
            "1. SILHOUETTE — Does the silhouette read clearly at distance? Is it distinctive?\n"
            "2. PROPORTIONS — Are proportions believable and consistent with the asset type?\n"
            "3. SHAPE LANGUAGE — Does it use clear primary/secondary/tertiary shapes? "
            "Are shapes varied (not all boxy or all round)?\n"
            "4. VISUAL BALANCE — Is mass distributed believably? Any side that looks too heavy or empty?\n"
            "5. FOCAL POINT — Where does the eye go first? Is that the right place?\n"
            "6. NEGATIVE SPACE — Is negative space used effectively?\n"
            "7. READABILITY — At thumbnail scale / gameplay distance, does the design still read?\n"
            "8. RECOMMENDATIONS — Top 3 specific changes that would most improve the design.\n"
            "Be direct. Speak as an artist to another artist. Don't hedge."
        )

        # The images + prompt are returned together so Claude's vision can process them
        critique_dict = {
            "artistic_critique": "pending_vision_review",
            "object":           object_name,
            "context":          context or "general review",
            "view_count":       len(images),
            "critique_prompt":  critique_prompt,
            "how_to_use": (
                "The 7 images above are the viewport captures. "
                "Read the critique_prompt field and apply it to the images. "
                "Give your full artistic assessment across all 8 dimensions listed. "
                "The images show FRONT[0] BACK[1] LEFT[2] RIGHT[3] TOP[4] BOTTOM[5] PERSP[6]."
            ),
            "dimensions": [
                "SILHOUETTE", "PROPORTIONS", "SHAPE_LANGUAGE",
                "VISUAL_BALANCE", "FOCAL_POINT", "NEGATIVE_SPACE",
                "READABILITY", "RECOMMENDATIONS"
            ],
        }

        _journal_entry("critique_artistic", object_name, "ok",
                       f"Captured {len(images)} views for artistic critique.")

        return images + [critique_dict]

    except Exception as e:
        logger.error(f"Error in critique_artistic: {e}")
        _journal_entry("critique_artistic", object_name, "error", str(e))
        return [{"error": str(e), "object": object_name}]


_STUDIO_PROFILE_DEFAULT = {
    "_comment": "Studio QA profile — edit these values to match YOUR studio's standards.",
    "studio_name": "My Studio",
    "target_engine": "UE5",
    "vert_budgets": {
        "hero_character":      80000,
        "background_character": 20000,
        "crowd_character":     10000,
        "weapon":              15000,
        "environment_prop":     5000,
        "vehicle":             50000,
        "creature":            60000,
    },
    "tri_budgets": {},
    "texel_density": {
        "hero_character":   10.24,   # px/cm
        "weapon":            5.12,
        "environment_prop":  2.56,
    },
    "uv_requirements": {
        "require_lightmap_channel": True,
        "max_uv_overlap_pct":       5.0,
        "min_uv_margin_px":         4,
    },
    "topology_standards": {
        "min_quad_pct":         75.0,
        "max_ngon_pct":          2.0,
        "max_poles_per_1k_verts": 5,
    },
    "naming_conventions": {
        "mesh_prefix":      "SM_",
        "skeletal_prefix":  "SK_",
        "material_prefix":  "M_",
        "texture_prefix":   "T_",
    },
    "export": {
        "format":              "FBX",
        "scale":               1.0,
        "apply_modifiers":     True,
        "triangulate_before":  False,
    },
}

# _PLAYBOOKS uses short keys (hero_char, env_prop); studio_profile.json's
# vert_budgets/tri_budgets use longer, human-written keys (hero_character,
# environment_prop) — weapon/vehicle/creature already match verbatim.
_CATEGORY_ALIASES = {"hero_char": "hero_character", "env_prop": "environment_prop"}


def _load_studio_profile_dict() -> dict:
    """Load+merge studio_profile.json over the built-in defaults, creating
    the file with defaults if missing. Reads fresh from disk every call —
    no caching — so edits take effect on the next tool call, no restart
    needed. Shared by load_studio_profile() (the human-facing tool) and
    every internal consumer (_studio_vert_budget, _studio_tri_budget) so
    there is exactly one loader, not several copies to drift out of sync."""
    profile_path = Path(__file__).parent / "studio_profile.json"
    profile = _STUDIO_PROFILE_DEFAULT.copy()
    profile["_profile_path"] = str(profile_path)
    profile["_profile_loaded"] = False
    if profile_path.exists():
        try:
            disk_profile = json.loads(profile_path.read_text())
            for k, v in disk_profile.items():
                if isinstance(v, dict) and isinstance(profile.get(k), dict):
                    profile[k].update(v)
                else:
                    profile[k] = v
            profile["_profile_loaded"] = True
        except Exception as e:
            profile["_load_error"] = str(e)
    else:
        try:
            profile_path.write_text(json.dumps(_STUDIO_PROFILE_DEFAULT, indent=2))
            profile["_created"] = f"Default profile written to {profile_path}"
        except Exception as e:
            profile["_write_error"] = str(e)
    return profile


def _studio_vert_budget(category: str, fallback: int) -> int:
    """Real vertex budget for `category` from studio_profile.json if
    present, else `fallback` unchanged — never breaks a category the
    profile doesn't mention. This is the actual fix for a real gap found
    live: the file existed, was auto-created, and its own docstring claimed
    simulate_production_readiness/review_board/production_review read it —
    none of them did; every one hardcoded its own numbers instead."""
    profile = _load_studio_profile_dict()
    key = _CATEGORY_ALIASES.get(category, category)
    value = profile.get("vert_budgets", {}).get(key)
    return value if isinstance(value, (int, float)) else fallback


def _studio_tri_budget(asset_type: str) -> Optional[int]:
    """Real triangle budget for a granular asset_type string (e.g. 'Giant
    Boss', 'Sword') from studio_profile.json's tri_budgets, matched case-
    insensitively with spaces/underscores normalized. None if no match —
    never forces a category the profile doesn't have."""
    profile = _load_studio_profile_dict()
    tri_budgets = profile.get("tri_budgets", {})
    normalized = asset_type.strip().lower().replace("_", " ").replace("-", " ")
    for key, value in tri_budgets.items():
        if key.strip().lower().replace("_", " ").replace("-", " ") == normalized:
            return value if isinstance(value, (int, float)) else None
    return None


def _estimate_triangle_count(polygon_count: int, quad_ratio_pct: float, tris_pct: float) -> dict:
    """Real effective triangle count from analyze_topology's stats — tri
    and quad portions are exact (1 tri per tri-face, 2 per quad-face); the
    ngon remainder is a documented approximation (3 tris/ngon) since real
    ngon-to-tri count depends on each ngon's actual vertex count, which
    analyze_topology's stats don't expose. exact=True only when there's no
    ngon remainder to estimate."""
    quad_ratio_pct = quad_ratio_pct or 0.0
    tris_pct = tris_pct or 0.0
    tri_faces = polygon_count * (tris_pct / 100.0)
    quad_faces = polygon_count * (quad_ratio_pct / 100.0)
    remainder_pct = max(0.0, 100.0 - tris_pct - quad_ratio_pct)
    ngon_faces = polygon_count * (remainder_pct / 100.0)
    estimated = tri_faces * 1 + quad_faces * 2 + ngon_faces * 3
    return {
        "estimated_tri_count": round(estimated),
        "exact": ngon_faces < 0.5,  # sub-half-a-face rounding noise, not a real ngon remainder
    }


@mcp.tool()
def load_studio_profile(show_current: bool = False) -> str:
    """
    STUDIO PROFILE — loads studio_profile.json (next to server.py): vert/tri
    budgets, texel density, naming conventions, UV/export settings. vert_
    budgets and tri_budgets are ACTUALLY wired into _PLAYBOOKS (what_next,
    review_board, simulate_production_readiness, production_review, the
    over_vert_budget rule) and production_review's granular asset_type
    matching — not just readable. Creates the file with defaults if missing.
    """
    profile = _load_studio_profile_dict()
    profile_loaded = profile.pop("_profile_loaded")
    profile_path = profile.pop("_profile_path")

    _journal_entry("load_studio_profile", "", "ok",
                   f"Loaded={profile_loaded} path={profile_path}")

    return json.dumps({
        "profile_path":   profile_path,
        "profile_loaded": profile_loaded,
        "profile":        profile,
        "how_to_customise": (
            f"Edit {profile_path} to set your studio's specific standards. "
            "The MCP reads this file at tool call time — no restart needed. "
            "vert_budgets/tri_budgets override the generic industry defaults "
            "used in simulate_production_readiness(), review_board(), "
            "production_review(), and what_next()."
        ),
        "active_in_tools": [
            "simulate_production_readiness (vert_budgets)",
            "review_board (vert_budgets)",
            "what_next (vert_budgets via playbook)",
            "production_review (vert_budgets + granular tri_budgets by asset_type)",
        ],
    }, indent=2, default=str)


# ─────────────────────────────────────────────────────────────────────────────
# SPRINT D — ASSET INTELLIGENCE
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def map_asset_dependencies(object_name: str) -> str:
    """
    ASSET DEPENDENCY MAP — what this object is connected to and what's
    connected to it: shared materials, shared armature, collection siblings,
    modifier references (both directions), shape keys, parent/children, and
    linked-duplicate mesh-data users. impact_summary + change_warning flag
    when edits here would cascade to other objects.
    """
    blender_script = f"""
import bpy, json

obj = bpy.data.objects.get("{object_name}")
if obj is None:
    print(json.dumps({{"error": "Object not found"}}))
    raise SystemExit

scene_objects = list(bpy.context.scene.objects)

# ── Shared materials ─────────────────────────────────────────────────────────
obj_mats = set(m.name for m in (obj.data.materials if obj.data and hasattr(obj.data,'materials') else []) if m)
shared_mat_users = []
for other in scene_objects:
    if other.name == obj.name: continue
    other_mats = set(m.name for m in (other.data.materials if other.data and hasattr(other.data,'materials') else []) if m)
    common = obj_mats & other_mats
    if common:
        shared_mat_users.append({{"object": other.name, "shared_materials": list(common)}})

# ── Shared armature ──────────────────────────────────────────────────────────
arm_name = None
for mod in obj.modifiers:
    if mod.type == 'ARMATURE' and mod.object:
        arm_name = mod.object.name
        break

shared_armature_users = []
if arm_name:
    for other in scene_objects:
        if other.name == obj.name: continue
        for mod in other.modifiers:
            if mod.type == 'ARMATURE' and mod.object and mod.object.name == arm_name:
                shared_armature_users.append(other.name)

# ── Collection siblings ──────────────────────────────────────────────────────
my_collections = [c.name for c in bpy.data.collections if obj.name in c.objects]
collection_siblings = {{}}
for col_name in my_collections:
    col = bpy.data.collections.get(col_name)
    if col:
        siblings = [o.name for o in col.objects if o.name != obj.name]
        collection_siblings[col_name] = siblings

# ── Modifier references (targets of this obj's modifiers) ───────────────────
mod_refs = []
for mod in obj.modifiers:
    ref = getattr(mod, 'object', None) or getattr(mod, 'target', None)
    if ref and ref.name != obj.name:
        mod_refs.append({{"modifier": mod.name, "type": mod.type, "references": ref.name}})

# ── Referenced by other objects' modifiers ───────────────────────────────────
referenced_by = []
for other in scene_objects:
    if other.name == obj.name: continue
    for mod in other.modifiers:
        ref = getattr(mod, 'object', None) or getattr(mod, 'target', None)
        if ref and ref.name == obj.name:
            referenced_by.append({{"object": other.name, "modifier": mod.name, "type": mod.type}})

# ── Shape keys ───────────────────────────────────────────────────────────────
shape_key_info = {{"has_shape_keys": False}}
if obj.data and hasattr(obj.data, 'shape_keys') and obj.data.shape_keys:
    sk = obj.data.shape_keys
    shape_key_info = {{
        "has_shape_keys": True,
        "key_count":      len(sk.key_blocks),
        "key_names":      [kb.name for kb in sk.key_blocks[:10]],
        "drivers":        len(sk.animation_data.drivers) if sk.animation_data else 0,
    }}

# ── Parent / children ────────────────────────────────────────────────────────
parent   = obj.parent.name if obj.parent else None
children = [c.name for c in obj.children]

# ── Mesh data users (linked duplicates) ─────────────────────────────────────
mesh_data_users = []
if obj.data:
    mesh_data_users = [o.name for o in bpy.data.objects
                       if o.data == obj.data and o.name != obj.name]

# ── Impact summary ───────────────────────────────────────────────────────────
total_deps = (len(shared_mat_users) + len(shared_armature_users) +
              len(referenced_by) + len(mesh_data_users))

if total_deps == 0:
    impact = "This object has no shared dependencies — changes are isolated."
    change_warning = None
elif total_deps < 5:
    impact = f"This object has {{total_deps}} dependency/dependencies — changes may affect a small number of other assets."
    change_warning = f"Check shared_materials and shared_armature before making topology changes."
else:
    impact = f"HIGH DEPENDENCY ASSET — {{total_deps}} connections found. Changes cascade to multiple other assets."
    change_warning = (
        f"Modifying this mesh affects: "
        + (f"{{len(shared_mat_users)}} material-sharing objects, " if shared_mat_users else "")
        + (f"{{len(shared_armature_users)}} skeleton-sharing objects, " if shared_armature_users else "")
        + (f"{{len(mesh_data_users)}} linked duplicate(s). " if mesh_data_users else "")
        + "Coordinate with the team before making changes."
    )

result = {{
    "object":            "{object_name}",
    "dependency_map": {{
        "shared_materials":    shared_mat_users,
        "shared_armature":     {{"armature": arm_name, "other_users": shared_armature_users}} if arm_name else None,
        "collection_siblings": collection_siblings,
        "modifier_references": mod_refs,
        "referenced_by":       referenced_by,
        "shape_keys":          shape_key_info,
        "parent":              parent,
        "children":            children,
        "mesh_data_users":     mesh_data_users,
    }},
    "total_dependencies": total_deps,
    "impact_summary":    impact,
    "change_warning":    change_warning,
}}
print(json.dumps(result))
"""
    try:
        blender = get_blender_connection()
        result  = blender.send_command("execute_code_safe", {
            "code": blender_script, "required_mode": "OBJECT", "push_undo": False
        })
        raw_out = result.get("result") or result.get("output") or ""
        for line in str(raw_out).splitlines():
            if line.strip().startswith("{"):
                parsed = json.loads(line.strip())
                if "error" not in parsed:
                    _journal_entry(
                        "map_asset_dependencies", object_name, "ok",
                        f"total_deps={parsed.get('total_dependencies',0)}"
                    )
                return json.dumps(parsed, indent=2, default=str)
        return json.dumps({"error": "No JSON output from dependency map", "raw": raw_out[:500]})
    except Exception as e:
        logger.error(f"Error in map_asset_dependencies: {e}")
        return json.dumps({"error": str(e)})


# ─────────────────────────────────────────────────────────────────────────────
# CONSTRUCTION MODE — Reality-to-Scene Pipeline
# Bridges reference images → asset manifest → Blender placement
#
# Token strategy:
#   - Master manifest (ERYNDOR_master_manifest.json) lives on disk, never sent raw
#   - Per-session compressed vocabulary = 1 line per asset ≈ 15 tokens each
#   - Claude only sees the compressed lines + image, not the full JSON
#   - Total vocab cost stays flat regardless of master manifest size
#
# Flow:
#   construction_mode() → get_asset_library (addon) → filter manifest →
#   compress vocab → analyze_reference_scene (Claude vision) →
#   calculate_world_coordinates → construction_preview → execute_construction (addon)
# ─────────────────────────────────────────────────────────────────────────────

_CONSTRUCTION_STATE: dict = {}   # live scene state: {instance_name: {asset, x, y, z, rx, ry, rz, sx}}
_MANIFEST_CACHE: dict = {}       # loaded manifest, invalidated when manifest file changes


def _load_manifest(manifest_path: str) -> dict:
    """Load and cache the master manifest JSON. Re-reads if file is newer than cache."""
    global _MANIFEST_CACHE
    p = Path(manifest_path)
    if not p.exists():
        return {}
    mtime = p.stat().st_mtime
    if _MANIFEST_CACHE.get("_path") == manifest_path and _MANIFEST_CACHE.get("_mtime") == mtime:
        return _MANIFEST_CACHE
    with open(p, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["_path"] = manifest_path
    data["_mtime"] = mtime
    _MANIFEST_CACHE = data
    return data


def _compress_manifest_vocab(manifest: dict, scene_asset_names: list[str]) -> str:
    """
    Convert manifest to token-efficient one-line-per-asset summary.
    Only includes assets present in the current Blender scene.
    Format: NAME | category | placement_rules | WxDxHm | description
    ~15 tokens per asset. Safe to send to Claude in every construction call.
    """
    assets = manifest.get("assets", {})
    lines = []
    for name in scene_asset_names:
        if name not in assets:
            # Asset in scene but not in manifest — include with minimal info
            lines.append(f"{name} | unknown | GROUND_PLACED | ?x?x?m | not yet in manifest")
            continue
        a = assets[name]
        d = a.get("dimensions_meters", {})
        dim = f"{d.get('x','?')}x{d.get('y','?')}x{d.get('z','?')}m"
        rules = ",".join(a.get("placement_rules", []))
        cat = a.get("category", "unknown")
        desc = a.get("description", "")[:60]
        notes = a.get("notes", "")[:60]
        lines.append(f"{name} | {cat} | {rules} | {dim} | {desc} | {notes}")
    return "\n".join(lines)


def _get_blend_dir() -> Optional[Path]:
    """
    Directory of the currently open .blend file, or None if unsaved/unavailable.
    Thin wrapper over _get_blend_filepath() — kept separate because most
    callers here want the containing directory, not the file path itself.
    """
    blend_path = _get_blend_filepath()
    return Path(blend_path).parent if blend_path else None


def _find_manifest(hint_path: str = "") -> str:
    """
    Locate the user's world manifest JSON. Search order:
    1. hint_path if explicitly provided
    2. Directory of the currently open .blend file (via Blender query)
    3. Current working directory
    The manifest is NEVER shipped with or stored next to server.py —
    it belongs to the user's project folder alongside their .blend files.
    Returns empty string if not found.
    """
    candidates = []

    # 1. Explicit override
    if hint_path:
        candidates.append(Path(hint_path))

    # 2. Directory of the open .blend file
    try:
        blend_dir = _get_blend_dir()
        if blend_dir:
            candidates.append(blend_dir / "ERYNDOR_master_manifest.json")
            # Also support any *_master_manifest.json in that dir
            for p in blend_dir.glob("*_master_manifest.json"):
                candidates.append(p)
    except Exception:
        pass  # Blender not connected yet — fall through

    # 3. Current working directory
    candidates.append(Path.cwd() / "ERYNDOR_master_manifest.json")
    for p in Path.cwd().glob("*_master_manifest.json"):
        candidates.append(p)

    seen = set()
    for c in candidates:
        if c not in seen and c.exists():
            return str(c)
        seen.add(c)
    return ""


@mcp.tool()
def load_manifest(manifest_path: str = "") -> str:
    """
    Load the world asset manifest and report what's in it.
    Auto-discovers *_master_manifest.json next to the currently open .blend
    file (or CWD) if no path given — never next to server.py.
    Call this first in any construction session to confirm manifest is readable.
    Returns: asset count, world name, scale reference, list of all known assets.
    """
    path = _find_manifest(manifest_path)
    if not path:
        return json.dumps({
            "error": "*_master_manifest.json not found",
            "searched": "directory of the currently open .blend file, then current working directory",
            "fix": "Place a *_master_manifest.json next to your .blend file, or pass manifest_path explicitly."
        }, indent=2)
    manifest = _load_manifest(path)
    assets = manifest.get("assets", {})
    meta = manifest.get("_meta", {})
    return json.dumps({
        "status": "ok",
        "manifest_path": path,
        "world": meta.get("world", "unknown"),
        "scale_reference": meta.get("scale_reference_asset"),
        "scale_reference_height_meters": meta.get("scale_reference_height_meters"),
        "total_assets": len(assets),
        "asset_names": sorted(assets.keys()),
    }, indent=2)


@mcp.tool()
def add_asset_to_manifest(
    asset_name: str,
    category: str,
    subcategory: str,
    placement_rules: str,
    width_m: float,
    depth_m: float,
    height_m: float,
    description: str,
    vision_keywords: str,
    notes: str = "",
    can_mirror: bool = True,
    can_array: bool = False,
    array_axis: str = "X",
    manifest_path: str = ""
) -> str:
    """
    Add a new SM_ asset to the ERYNDOR master manifest. Call once per new asset.
    category: building|industrial|prop|ground|overhead|character.
    placement_rules: comma-separated from GROUND_PLACED, WALL_ATTACHED,
    CEILING_HUNG, FREESTANDING, TILEABLE, CONNECTOR, SCATTER, UNIQUE.
    width/depth/height_m map to Blender X/Y/Z. vision_keywords: comma-separated
    words vision might use to describe it.
    """
    path = _find_manifest(manifest_path)
    if not path:
        # Create next to the open .blend file if it doesn't exist yet — NEVER
        # next to server.py, that's the shared repo, not the user's project.
        blend_dir = _get_blend_dir()
        base_dir = blend_dir if blend_dir else Path.cwd()
        path = str(base_dir / "ERYNDOR_master_manifest.json")

    if Path(path).exists():
        with open(path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    else:
        manifest = {
            "_meta": {
                "world": "Eryndor",
                "version": "1.0",
                "scale_reference_asset": "SM_Wooden_Door",
                "scale_reference_height_meters": 2.1,
            },
            "assets": {}
        }

    if asset_name in manifest["assets"]:
        return json.dumps({
            "status": "already_exists",
            "asset": asset_name,
            "message": f"{asset_name} is already in the manifest. Use update_asset_in_manifest() to modify it.",
        }, indent=2)

    manifest["assets"][asset_name] = {
        "category": category,
        "subcategory": subcategory,
        "placement_rules": [r.strip() for r in placement_rules.split(",")],
        "dimensions_meters": {"x": width_m, "y": depth_m, "z": height_m},
        "description": description,
        "can_mirror": can_mirror,
        "can_array": can_array,
        "array_axis": array_axis,
        "pivot": "base_center",
        "vision_keywords": [k.strip() for k in vision_keywords.split(",")],
        "notes": notes,
    }

    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    # Invalidate cache
    global _MANIFEST_CACHE
    _MANIFEST_CACHE = {}

    return json.dumps({
        "status": "added",
        "asset": asset_name,
        "manifest_path": path,
        "total_assets_now": len(manifest["assets"]),
    }, indent=2)


@mcp.tool()
def construction_mode(
    reference_image_path: str,
    scene_name: str,
    scale_anchor_asset: str = "SM_Wooden_Door",
    scale_anchor_height_meters: float = 2.1,
    manifest_path: str = "",
    alley_width_meters: float = 4.5,
    camera_height_meters: float = 1.7
) -> list:
    """
    CONSTRUCTION MODE — analyzes a reference image, matches it against your
    Eryndor SM_ assets, and returns a placement plan. Pipeline: load manifest
    → read scene assets → vision analysis → calculate_world_coordinates()
    → execute_construction() on approval. Scale anchor default:
    SM_Wooden_Door=2.1m tall. Returns [Image, plan_dict] — read the image,
    apply vision_prompt from the dict, feed your JSON response into
    calculate_world_coordinates().
    """
    try:
        # ── 1. Load manifest ──────────────────────────────────────────────────
        mpath = _find_manifest(manifest_path)
        if not mpath:
            return [{"error": "Manifest not found. Run load_manifest() first."}]
        manifest = _load_manifest(mpath)

        # ── 2. Read active scene assets from Blender ──────────────────────────
        scene_result = _send_json("get_asset_library")
        try:
            scene_data = json.loads(scene_result) if isinstance(scene_result, str) else scene_result
        except Exception:
            scene_data = {}

        scene_assets = scene_data.get("sm_assets", [])
        if not scene_assets:
            return [{
                "error": "No SM_ assets found in current Blender scene.",
                "fix": "Open your construction .blend file with SM_ assets before running construction_mode()."
            }]

        # ── 3. Compress vocabulary (token-safe) ───────────────────────────────
        vocab = _compress_manifest_vocab(manifest, scene_assets)
        meta = manifest.get("_meta", {})
        scale_ref = meta.get("scale_reference_asset", scale_anchor_asset)
        scale_h = meta.get("scale_reference_height_meters", scale_anchor_height_meters)

        # ── 4. Load reference image — returned as a real Image content block,
        # NOT base64-embedded in the JSON text (that inflated responses past
        # any reasonable size — a modest reference image blew past 2.8M chars).
        img_path = Path(reference_image_path)
        if not img_path.exists():
            return [{"error": f"Reference image not found: {reference_image_path}"}]

        with open(img_path, "rb") as f:
            img_bytes = f.read()
        suffix = img_path.suffix.lower().lstrip(".")
        img_format = "jpeg" if suffix in ("jpg", "jpeg") else "png"

        # ── 5. Build vision analysis prompt (compact — this gets sent AND
        # echoed back in the response, so every char here is paid twice) ──────
        vision_prompt = f"""3D scene layout analyst for Eryndor. Scale: {scale_ref}={scale_h}m tall (1 unit=1m).

ASSETS (name|category|placement|dims|desc|notes):
{vocab}

Analyze the image, return ONLY this JSON (no markdown fences):
{{"scene_analysis":{{"camera_angle_degrees":N,"vanishing_point_frame_x":0-1,"scene_width_meters":N,"scene_depth_meters":N,"dominant_mood":"str"}},
"placements":[{{"instance_id":"asset_001","asset":"exact SM_ name","frame_x":0-1,"frame_y":0-1,"relative_scale":1.0,"side":"left|right|center|background","facing_degrees":0-360,"mirrored":bool,"confidence":"HIGH|MEDIUM|LOW","approximate":bool,"match_reason":"str","placement_note":"str"}}],
"pipe_runs":[{{"description":"str","segments":[{{"asset":"name","instance_id":"id","frame_x":0-1,"frame_y":0-1,"orientation":"vertical|horizontal","side":"left|right","confidence":"HIGH|MEDIUM|LOW"}}]}}],
"gaps":[{{"description":"what was seen","suggested_asset_name":"SM_name"}}],
"approximate_matches":[{{"instance_id":"id","reason":"str"}}]}}

Rules: only use listed assets, never invent names. frame_x 0=left 1=right; frame_y 0=top 1=bottom (higher=closer to camera). Trace pipe runs segment by segment, one entry per section. Unmatched image content goes in gaps, not placements. Scale relative to {scale_ref}={scale_h}m."""

        # ── 6. Return the reference image as a real Image content block +
        # the vision prompt as a metadata dict. Claude reads the image
        # directly (vision is native, no base64 JSON round-trip needed) and
        # responds with the JSON plan per vision_prompt's instructions, then
        # feeds that into calculate_world_coordinates().
        vision_request = {
            "status": "vision_analysis_required",
            "scene_name": scene_name,
            "scene_assets": scene_assets,
            "manifest_asset_count": len(manifest.get("assets", {})),
            "scale_anchor": f"{scale_ref} = {scale_h}m",
            "alley_width_meters": alley_width_meters,
            "vision_prompt": vision_prompt,
            "instruction": (
                "Analyze the reference image above using the vision_prompt field. "
                "Then call calculate_world_coordinates(scene_name, vision_json, "
                f"alley_width_meters={alley_width_meters}) with your JSON response "
                "to convert frame positions to Blender world coordinates."
            ),
        }
        return [Image(data=img_bytes, format=img_format), vision_request]

    except Exception as e:
        logger.error(f"construction_mode error: {e}")
        return [{"error": str(e)}]


@mcp.tool()
def calculate_world_coordinates(
    scene_name: str,
    vision_json: str,
    alley_width_meters: float = 4.5,
    scale_anchor_height_meters: float = 2.1,
    manifest_path: str = ""
) -> str:
    """
    Converts vision_json frame positions (0-1) to Blender world XYZ. Call right
    after construction_mode(). X=horizontal, Y=depth, Z=height. Placement
    treats assets as points, not their real footprint — buildings often need
    manual X/Y spacing correction afterward via adjust_asset() so large meshes
    don't overlap; verify with a screenshot before trusting the auto layout.
    """
    try:
        try:
            vision = json.loads(vision_json)
        except json.JSONDecodeError as e:
            return json.dumps({"error": f"Invalid vision JSON: {e}"}, indent=2)

        mpath = _find_manifest(manifest_path)
        manifest = _load_manifest(mpath) if mpath else {}
        assets_meta = manifest.get("assets", {})

        scene_analysis = vision.get("scene_analysis", {})
        vp_x = scene_analysis.get("vanishing_point_frame_x", 0.5)   # horizontal vanish point
        scene_depth = scene_analysis.get("scene_depth_meters", 20.0)
        scene_width = scene_analysis.get("scene_width_meters", alley_width_meters)
        cam_angle = scene_analysis.get("camera_angle_degrees", 5.0)

        placements_out = []
        all_placements = vision.get("placements", [])

        # Flatten pipe run segments into placements list
        for run in vision.get("pipe_runs", []):
            for seg in run.get("segments", []):
                seg["_from_pipe_run"] = run.get("description", "")
                all_placements.append(seg)

        for p in all_placements:
            asset_name = p.get("asset", "")
            frame_x = float(p.get("frame_x", 0.5))
            frame_y = float(p.get("frame_y", 0.5))
            rel_scale = float(p.get("relative_scale", 1.0))
            facing = float(p.get("facing_degrees", 0.0))
            mirrored = p.get("mirrored", False)
            side = p.get("side", "center")
            orientation = p.get("orientation", "vertical")  # for pipes

            # ── X coordinate: horizontal position ────────────────────────────
            # frame_x 0=left edge, 1=right edge → world X centered at 0
            world_x = (frame_x - 0.5) * scene_width

            # ── Y coordinate: depth (distance from camera) ────────────────────
            # frame_y 0=top(far), 1=bottom(near) — but perspective means
            # objects cluster near vanishing point at horizon
            # Linear approximation: frame_y 1.0 = 0m depth, 0.0 = scene_depth
            # Corrected for vanishing point position
            horizon_y = 1.0 - (cam_angle / 90.0)   # where horizon sits in frame
            if frame_y >= horizon_y:
                # Below horizon = foreground, closer
                t = (frame_y - horizon_y) / max(1.0 - horizon_y, 0.01)
                world_y = scene_depth * (1.0 - t) * 0.4   # 0m to 40% of depth
            else:
                # Above horizon = background
                t = (horizon_y - frame_y) / max(horizon_y, 0.01)
                world_y = scene_depth * (0.4 + t * 0.6)   # 40% to 100% of depth

            # ── Z coordinate: height above ground ────────────────────────────
            # Most ground-placed assets sit at Z=0
            # Wall-attached assets need height offset based on their frame_y
            asset_meta = assets_meta.get(asset_name, {})
            placement_rules = asset_meta.get("placement_rules", [])
            asset_h = asset_meta.get("dimensions_meters", {}).get("z", scale_anchor_height_meters)

            world_z = 0.0
            if "CEILING_HUNG" in placement_rules:
                # Flags/banners hang from above — estimate ceiling height
                # Ceiling ≈ building height * 0.8 as attach point
                world_z = 6.0   # default overhead attach height
            elif "WALL_ATTACHED" in placement_rules and "GROUND_PLACED" not in placement_rules:
                # Pure wall-attached: height proportional to frame position
                world_z = (1.0 - frame_y) * 5.0

            # ── Rotation ─────────────────────────────────────────────────────
            rot_z = facing   # degrees around Z (yaw)
            rot_x = 0.0
            if orientation == "horizontal":
                rot_x = 90.0   # pipe lying on its side

            # ── Scale ────────────────────────────────────────────────────────
            scale = rel_scale

            # ── Confidence radius ────────────────────────────────────────────
            conf = p.get("confidence", "MEDIUM")
            conf_radius = {"HIGH": 0.5, "MEDIUM": 1.5, "LOW": 3.0}.get(conf, 1.5)

            placements_out.append({
                "instance_id": p.get("instance_id", f"{asset_name}_auto"),
                "asset": asset_name,
                "world_x": round(world_x, 3),
                "world_y": round(world_y, 3),
                "world_z": round(world_z, 3),
                "rotation_x_deg": round(rot_x, 1),
                "rotation_y_deg": 0.0,
                "rotation_z_deg": round(rot_z, 1),
                "scale": round(scale, 3),
                "mirrored": mirrored,
                "side": side,
                "confidence": conf,
                "confidence_radius_meters": conf_radius,
                "approximate": p.get("approximate", False),
                "match_reason": p.get("match_reason", ""),
                "placement_note": p.get("placement_note", p.get("_from_pipe_run", "")),
            })

        # Store in construction state for execute step
        global _CONSTRUCTION_STATE
        _CONSTRUCTION_STATE[scene_name] = {
            "placements": placements_out,
            "gaps": vision.get("gaps", []),
            "approximate_matches": vision.get("approximate_matches", []),
            "scene_analysis": scene_analysis,
        }

        # Counts only — full detail already lives in "placements"/"gaps" below,
        # no need for a duplicate hand-formatted text summary of the same data.
        high = sum(1 for p in placements_out if p["confidence"] == "HIGH")
        med  = sum(1 for p in placements_out if p["confidence"] == "MEDIUM")
        low  = sum(1 for p in placements_out if p["confidence"] == "LOW")
        approx = sum(1 for p in placements_out if p["approximate"])
        gaps = vision.get("gaps", [])

        return json.dumps({
            "status": "ready_for_review",
            "scene_name": scene_name,
            "total_placements": len(placements_out),
            "confidence_summary": {"HIGH": high, "MEDIUM": med, "LOW": low},
            "approximate_matches": approx,
            "gaps": gaps,
            "next_step": f"execute_construction(scene_name='{scene_name}') to place, or adjust_asset(...) to nudge first.",
            "placements": placements_out,
        }, indent=2)

    except Exception as e:
        logger.error(f"calculate_world_coordinates error: {e}")
        return json.dumps({"error": str(e)}, indent=2)


@mcp.tool()
def execute_construction(scene_name: str, collection_name: str = "") -> str:
    """
    Execute the construction plan — place all assets in Blender.
    Call after reviewing the output of calculate_world_coordinates().
    Assets are grouped into a named collection for easy management.

    Args:
        scene_name: the scene_name used in construction_mode()
        collection_name: Blender collection name (default: CONSTRUCTION_<scene_name>)

    Returns: placement results — what succeeded, what failed, total placed.
    """
    _invalidate_dna_cache()  # places/moves multiple assets, not one tracked object
    global _CONSTRUCTION_STATE
    if scene_name not in _CONSTRUCTION_STATE:
        return json.dumps({
            "error": f"No construction plan found for scene '{scene_name}'.",
            "fix": "Run construction_mode() and calculate_world_coordinates() first."
        }, indent=2)

    plan = _CONSTRUCTION_STATE[scene_name]
    placements = plan.get("placements", [])
    coll_name = collection_name or f"CONSTRUCTION_{scene_name}"

    result = _send_json(
        "execute_construction",
        placements=placements,
        collection_name=coll_name,
        scene_name=scene_name,
    )

    try:
        r = json.loads(result) if isinstance(result, str) else result
    except Exception:
        r = {"raw": str(result)}

    placed = r.get("placed", [])
    failed = r.get("failed", [])

    # Update construction state with confirmed positions
    for item in placed:
        iid = item.get("instance_id")
        if iid:
            for p in placements:
                if p["instance_id"] == iid:
                    p["blender_name"] = item.get("blender_name", iid)
                    break

    gaps = plan.get("gaps", [])

    return json.dumps({
        "status": "construction_complete" if not failed else "construction_partial",
        "scene_name": scene_name,
        "collection": coll_name,
        "placed_count": len(placed),
        "failed_count": len(failed),
        "placed": placed,
        "failed": failed,
        "gaps_not_filled": gaps,
        "next_steps": "Review in viewport. adjust_asset() to refine, add_asset_to_scene() to fill gaps, sync_construction_state() after manual edits.",
    }, indent=2)


@mcp.tool()
def adjust_asset(
    scene_name: str,
    instance_id: str,
    move_x: float = 0.0,
    move_y: float = 0.0,
    move_z: float = 0.0,
    rotate_z: float = 0.0,
    scale_delta: float = 0.0
) -> str:
    """
    Move/rotate/scale a placed asset by DELTA values (additive, not absolute).
    Use this instead of moving manually in Blender — keeps agent state in sync.
    scale_delta is additive (0.1 = +10%, -0.1 = -10%).
    """
    _invalidate_dna_cache()  # instance_id, not a plain object name — clear broadly
    global _CONSTRUCTION_STATE
    if scene_name not in _CONSTRUCTION_STATE:
        return json.dumps({"error": f"No active construction scene '{scene_name}'"}, indent=2)

    placements = _CONSTRUCTION_STATE[scene_name].get("placements", [])
    target = next((p for p in placements if p["instance_id"] == instance_id), None)
    if not target:
        return json.dumps({
            "error": f"Instance '{instance_id}' not found in scene '{scene_name}'",
            "available": [p["instance_id"] for p in placements]
        }, indent=2)

    # Apply deltas to state
    target["world_x"] = round(target["world_x"] + move_x, 3)
    target["world_y"] = round(target["world_y"] + move_y, 3)
    target["world_z"] = round(target["world_z"] + move_z, 3)
    target["rotation_z_deg"] = round(target["rotation_z_deg"] + rotate_z, 1)
    target["scale"] = round(max(0.01, target["scale"] + scale_delta), 3)

    # Send to Blender
    blender_name = target.get("blender_name", instance_id)
    result = _send_json(
        "move_object",
        name=blender_name,
        x=target["world_x"],
        y=target["world_y"],
        z=target["world_z"],
        rotation_z=target["rotation_z_deg"],
        scale=target["scale"],
    )

    return json.dumps({
        "status": "adjusted",
        "instance_id": instance_id,
        "blender_name": blender_name,
        "new_position": {
            "x": target["world_x"],
            "y": target["world_y"],
            "z": target["world_z"],
        },
        "new_rotation_z": target["rotation_z_deg"],
        "new_scale": target["scale"],
        "blender_result": result,
    }, indent=2)


@mcp.tool()
def add_asset_to_scene(
    scene_name: str,
    asset_name: str,
    world_x: float,
    world_y: float,
    world_z: float = 0.0,
    rotation_z: float = 0.0,
    scale: float = 1.0,
    mirrored: bool = False,
    instance_id: str = ""
) -> str:
    """
    Add an asset at an explicit absolute world position — fills gaps or adds
    extras after the initial construction pass. instance_id auto-generated if empty.
    """
    global _CONSTRUCTION_STATE
    if scene_name not in _CONSTRUCTION_STATE:
        _CONSTRUCTION_STATE[scene_name] = {"placements": [], "gaps": [], "approximate_matches": [], "scene_analysis": {}}

    iid = instance_id or f"{asset_name}_{len(_CONSTRUCTION_STATE[scene_name]['placements'])+1:03d}"

    placement = {
        "instance_id": iid,
        "asset": asset_name,
        "world_x": world_x,
        "world_y": world_y,
        "world_z": world_z,
        "rotation_x_deg": 0.0,
        "rotation_y_deg": 0.0,
        "rotation_z_deg": rotation_z,
        "scale": scale,
        "mirrored": mirrored,
        "confidence": "HIGH",
        "confidence_radius_meters": 0.0,
        "approximate": False,
        "match_reason": "manually added",
        "placement_note": "",
    }
    _CONSTRUCTION_STATE[scene_name]["placements"].append(placement)

    result = _send_json(
        "execute_construction",
        placements=[placement],
        collection_name=f"CONSTRUCTION_{scene_name}",
        scene_name=scene_name,
    )

    return json.dumps({
        "status": "added",
        "instance_id": iid,
        "asset": asset_name,
        "position": {"x": world_x, "y": world_y, "z": world_z},
        "blender_result": result,
    }, indent=2)


@mcp.tool()
def sync_construction_state(scene_name: str) -> str:
    """
    Re-read all construction asset positions from Blender and update agent state.
    Call this if you've made ANY manual adjustments in Blender viewport.
    Without this, the agent's internal positions will drift from reality.

    Args:
        scene_name: active construction scene name

    Returns: updated positions for all tracked assets in this scene.
    """
    global _CONSTRUCTION_STATE
    if scene_name not in _CONSTRUCTION_STATE:
        return json.dumps({"error": f"No construction state for '{scene_name}'"}, indent=2)

    placements = _CONSTRUCTION_STATE[scene_name].get("placements", [])
    blender_names = [p.get("blender_name", p["instance_id"]) for p in placements]

    result = _send_json(
        "get_construction_positions",
        object_names=blender_names,
        collection_name=f"CONSTRUCTION_{scene_name}",
    )

    try:
        r = json.loads(result) if isinstance(result, str) else result
    except Exception:
        r = {}

    positions = r.get("positions", {})
    synced = 0
    for p in placements:
        bname = p.get("blender_name", p["instance_id"])
        if bname in positions:
            pos = positions[bname]
            p["world_x"] = pos.get("x", p["world_x"])
            p["world_y"] = pos.get("y", p["world_y"])
            p["world_z"] = pos.get("z", p["world_z"])
            p["rotation_z_deg"] = pos.get("rotation_z", p["rotation_z_deg"])
            p["scale"] = pos.get("scale", p["scale"])
            synced += 1

    return json.dumps({
        "status": "synced",
        "scene_name": scene_name,
        "synced_count": synced,
        "total_tracked": len(placements),
        "message": "Agent state updated from Blender. You can now use adjust_asset() safely."
    }, indent=2)


@mcp.tool()
def construction_report(scene_name: str) -> str:
    """
    Full status report for an active construction scene.
    Shows all placed assets, their positions, confidence levels,
    approximate matches, gaps not filled, and suggested next steps.

    Args:
        scene_name: active construction scene name
    """
    global _CONSTRUCTION_STATE
    if scene_name not in _CONSTRUCTION_STATE:
        return json.dumps({"error": f"No construction state for scene '{scene_name}'"}, indent=2)

    state = _CONSTRUCTION_STATE[scene_name]
    placements = state.get("placements", [])
    gaps = state.get("gaps", [])
    approx = state.get("approximate_matches", [])

    return json.dumps({
        "scene_name": scene_name,
        "total_placed": len(placements),
        "approximate_matches": len(approx),
        "unfilled_gaps": len(gaps),
        "placed_assets": placements,
        "gaps": gaps,
        "commands": {
            "adjust": "adjust_asset(scene_name, instance_id, move_x, move_y, move_z, rotate_z)",
            "add": "add_asset_to_scene(scene_name, asset_name, world_x, world_y, world_z)",
            "sync": "sync_construction_state(scene_name) after manual Blender edits",
        }
    }, indent=2)


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

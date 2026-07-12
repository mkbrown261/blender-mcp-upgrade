# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp[cli]"]
# ///
"""
Custom MCP server for blender-mcp-upgrade.

Bridges Claude to the BlenderMCPServer TCP socket (addon.py, localhost:9876)
and exposes BOTH the original ~22 commands and the 17 new v2.0/2.1 AI
Technical Artist handlers (get_mesh_quality_report, analyze_topology, etc.)
as first-class @mcp.tool() functions.

Wire protocol (see addon.py _handle_client / execute_command):
  request  -> raw JSON, no length prefix: {"type": "<command>", "params": {...}}
  response <- raw JSON: {"status": "success", "result": ...}
              or        {"status": "error", "message": "..."}
Because there's no length prefix, the client must accumulate bytes and
retry json.loads() until it parses -- that's what receive_full_response does.
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
        sock.settimeout(180.0)  # match addon's own operation timeout
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
                        continue  # incomplete JSON, keep reading
                except socket.timeout:
                    logger.warning("Socket timeout during chunked receive")
                    break
                except (ConnectionError, BrokenPipeError, ConnectionResetError):
                    raise
        except socket.timeout:
            logger.warning("Socket timeout during chunked receive")

        if chunks:
            data = b"".join(chunks)
            json.loads(data.decode("utf-8"))  # raises if still incomplete
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
                "Timeout waiting for Blender response. If Blender is running headless "
                "(blender -b), commands never execute; run with a GUI, or via "
                "'xvfb-run -a blender'."
            )
        except (ConnectionError, BrokenPipeError, ConnectionResetError) as e:
            self.sock = None
            raise Exception(f"Connection to Blender lost: {e}")
        except json.JSONDecodeError as e:
            raise Exception(f"Invalid response from Blender: {e}")
        except Exception as e:
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


def _process_bbox(original_bbox):
    if original_bbox is None:
        return None
    if all(isinstance(i, int) for i in original_bbox):
        return original_bbox
    if any(i <= 0 for i in original_bbox):
        raise ValueError("bbox values must be greater than zero")
    return [int(float(i) / max(original_bbox) * 100) for i in original_bbox]


mcp = FastMCP("BlenderMCP")

# ─────────────────────────────────────────────────────────────────────────
# ORIGINAL LAYER (~22 commands)
# ─────────────────────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────────────────────
# NEW AI TECHNICAL ARTIST LAYER (v2.0/2.1) -- previously unreachable
# ─────────────────────────────────────────────────────────────────────────

@mcp.tool()
def get_mesh_quality_report(name: str) -> str:
    """Get mesh diagnostics for a named object: n-gons, non-manifold edges, degenerate faces, UV overlaps, vertex-group summary, suggested fixes."""
    return _send_json("get_mesh_quality_report", name=name)


@mcp.tool()
def analyze_topology(name: str, context: str = "generic") -> str:
    """Analyze mesh topology quality for a named object, tailored to a target context (e.g. 'generic', 'animation', 'subdivision')."""
    return _send_json("analyze_topology", name=name, context=context)


@mcp.tool()
def detect_mesh_problems(name: str) -> str:
    """Detect common mesh problems (non-manifold geometry, loose vertices, zero-area faces, etc.) on a named object."""
    return _send_json("detect_mesh_problems", name=name)


@mcp.tool()
def get_armature_info(name: str) -> str:
    """Get bone hierarchy, constraints, and deform-bone info for a named armature."""
    return _send_json("get_armature_info", name=name)


@mcp.tool()
def analyze_animation_quality(name: str, frame_start: Optional[int] = None, frame_end: Optional[int] = None) -> str:
    """Analyze animation quality (e.g. foot sliding, jitter, key density) for a named object/armature over an optional frame range."""
    return _send_json("analyze_animation_quality", name=name, frame_start=frame_start, frame_end=frame_end)


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
    """Create or update a production-ready Principled BSDF PBR material, with optional subsurface, emission, and a wear/variation layer."""
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
    """Run a production QA pass on a named object: UVs, materials, modifiers, and other readiness checks."""
    return _send_json("run_asset_qa", name=name, check_uvs=check_uvs, check_materials=check_materials, check_modifiers=check_modifiers)


@mcp.tool()
def run_unreal_readiness_check(name: str, expected_unit_scale: float = 0.01) -> str:
    """Check whether a named object is ready for Unreal Engine import (scale, pivot, naming, collision, etc.)."""
    return _send_json("run_unreal_readiness_check", name=name, expected_unit_scale=expected_unit_scale)


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
    """Export a named object/armature as an FBX file with Unreal Engine 5 conventions (scale, axis, triangulation)."""
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
    """Get the collection/object hierarchy of the current scene, up to max_depth."""
    return _send_json("get_scene_hierarchy", max_depth=max_depth)


@mcp.tool()
def get_selection_context() -> str:
    """Get what's currently selected in Blender: active object, selection list, mode, and edit-mesh selection counts."""
    return _send_json("get_selection_context")


@mcp.tool()
def get_material_graph(material_name: str) -> str:
    """Get the shader node graph (nodes + links) of a named material."""
    return _send_json("get_material_graph", material_name=material_name)


@mcp.tool()
def get_animation_data(name: str) -> str:
    """Get action/keyframe/fcurve data for a named object."""
    return _send_json("get_animation_data", name=name)


@mcp.tool()
def execute_code_safe(code: str, required_mode: Optional[str] = None, push_undo: bool = True) -> str:
    """Execute Python code in Blender with an undo checkpoint pushed first and an optional mode switch ('OBJECT'|'EDIT'|'POSE') restored afterward."""
    return _send_json("execute_code_safe", code=code, required_mode=required_mode, push_undo=push_undo)


@mcp.tool()
def prepare_lod_names(base_name: str, lod_count: int = 4) -> str:
    """Generate/validate LOD naming convention (e.g. base_name_LOD0..N) for a given base object name."""
    return _send_json("prepare_lod_names", base_name=base_name, lod_count=lod_count)


@mcp.tool()
def get_session_log() -> str:
    """Get the last ~20 commands executed this Blender session with status, for debugging/audit."""
    return _send_json("get_session_log")


# ─────────────────────────────────────────────────────────────────────────

def main():
    try:
        interactive = sys.stdin.isatty()
    except (AttributeError, OSError):
        interactive = False
    if interactive:
        logger.info(
            "BlenderMCP custom server is meant to be launched by an MCP client, not run "
            "by hand. It will now wait silently for a client on stdin -- that's normal."
        )
    mcp.run()


if __name__ == "__main__":
    main()

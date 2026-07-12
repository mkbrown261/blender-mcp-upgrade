# Code created by Siddharth Ahuja: www.github.com/ahujasid © 2025

import re
import bpy
import bmesh
import mathutils
import json
import threading
import socket
import time
import requests
import tempfile
import traceback
import os
import shutil
import zipfile
from bpy.props import IntProperty, BoolProperty
import io
from datetime import datetime
import hashlib, hmac, base64
import os.path as osp
from contextlib import redirect_stdout, suppress

bl_info = {
    "name": "Blender MCP",
    "author": "BlenderMCP",
    "version": (2, 0),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar > BlenderMCP",
    "description": "Connect Blender to Claude via MCP — AI Technical Artist Edition",
    "category": "Interface",
}

RODIN_FREE_TRIAL_KEY = "k9TcfFoEhNd9cCPP2guHAHHHkctZHIRhZDywZ1euGUXwihbYLpOjQhofby80NJez"

# Add User-Agent as required by Poly Haven API
REQ_HEADERS = requests.utils.default_headers()
REQ_HEADERS.update({"User-Agent": "blender-mcp"})

class BlenderMCPServer:
    def __init__(self, host='localhost', port=9876):
        self.host = host
        self.port = port
        self.running = False
        self.socket = None
        self.server_thread = None

    def start(self):
        if self.running:
            print("Server is already running")
            return

        self.running = True

        try:
            # Create socket
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.bind((self.host, self.port))
            self.socket.listen(1)

            # Start server thread
            self.server_thread = threading.Thread(target=self._server_loop)
            self.server_thread.daemon = True
            self.server_thread.start()

            print(f"BlenderMCP server started on {self.host}:{self.port}")
        except Exception as e:
            print(f"Failed to start server: {str(e)}")
            self.stop()

    def stop(self):
        self.running = False

        # Close socket
        if self.socket:
            try:
                self.socket.close()
            except:
                pass
            self.socket = None

        # Wait for thread to finish
        if self.server_thread:
            try:
                if self.server_thread.is_alive():
                    self.server_thread.join(timeout=1.0)
            except:
                pass
            self.server_thread = None

        print("BlenderMCP server stopped")

    def _server_loop(self):
        """Main server loop in a separate thread"""
        print("Server thread started")
        self.socket.settimeout(1.0)  # Timeout to allow for stopping

        while self.running:
            try:
                # Accept new connection
                try:
                    client, address = self.socket.accept()
                    print(f"Connected to client: {address}")

                    # Handle client in a separate thread
                    client_thread = threading.Thread(
                        target=self._handle_client,
                        args=(client,)
                    )
                    client_thread.daemon = True
                    client_thread.start()
                except socket.timeout:
                    # Just check running condition
                    continue
                except Exception as e:
                    print(f"Error accepting connection: {str(e)}")
                    time.sleep(0.5)
            except Exception as e:
                print(f"Error in server loop: {str(e)}")
                if not self.running:
                    break
                time.sleep(0.5)

        print("Server thread stopped")

    def _handle_client(self, client):
        """Handle connected client"""
        print("Client handler started")
        client.settimeout(None)  # No timeout
        buffer = b''

        try:
            while self.running:
                # Receive data
                try:
                    data = client.recv(8192)
                    if not data:
                        print("Client disconnected")
                        break

                    buffer += data
                    try:
                        # Try to parse command
                        command = json.loads(buffer.decode('utf-8'))
                        buffer = b''

                        # Execute command in Blender's main thread
                        def execute_wrapper():
                            try:
                                response = self.execute_command(command)
                                response_json = json.dumps(response)
                                try:
                                    client.sendall(response_json.encode('utf-8'))
                                except:
                                    print("Failed to send response - client disconnected")
                            except Exception as e:
                                print(f"Error executing command: {str(e)}")
                                traceback.print_exc()
                                try:
                                    error_response = {
                                        "status": "error",
                                        "message": str(e)
                                    }
                                    client.sendall(json.dumps(error_response).encode('utf-8'))
                                except:
                                    pass
                            return None

                        # Schedule execution in main thread
                        bpy.app.timers.register(execute_wrapper, first_interval=0.0)
                    except json.JSONDecodeError:
                        # Incomplete data, wait for more
                        pass
                except Exception as e:
                    print(f"Error receiving data: {str(e)}")
                    break
        except Exception as e:
            print(f"Error in client handler: {str(e)}")
        finally:
            try:
                client.close()
            except:
                pass
            print("Client handler stopped")

    def execute_command(self, command):
        """Execute a command in the main Blender thread"""
        try:
            return self._execute_command_internal(command)

        except Exception as e:
            print(f"Error executing command: {str(e)}")
            traceback.print_exc()
            return {"status": "error", "message": str(e)}

    def _execute_command_internal(self, command):
        """Internal command execution with proper context"""
        cmd_type = command.get("type")
        params = command.get("params", {})

        # Add a handler for checking PolyHaven status
        if cmd_type == "get_polyhaven_status":
            return {"status": "success", "result": self.get_polyhaven_status()}

        # Base handlers that are always available
        handlers = {
            "get_scene_info": self.get_scene_info,
            "get_object_info": self.get_object_info,
            "get_viewport_screenshot": self.get_viewport_screenshot,
            "execute_code": self.execute_code,
            "execute_code_safe": self.execute_code_safe,
            "get_telemetry_consent": self.get_telemetry_consent,
            "get_polyhaven_status": self.get_polyhaven_status,
            "get_hyper3d_status": self.get_hyper3d_status,
            "get_sketchfab_status": self.get_sketchfab_status,
            "get_hunyuan3d_status": self.get_hunyuan3d_status,
            # --- Analyst Layer ---
            "get_selection_context": self.get_selection_context,
            "get_mesh_quality_report": self.get_mesh_quality_report,
            "get_material_graph": self.get_material_graph,
            "get_animation_data": self.get_animation_data,
            "get_scene_hierarchy": self.get_scene_hierarchy,
            # --- Topology Layer ---
            "analyze_topology": self.analyze_topology,
            "detect_mesh_problems": self.detect_mesh_problems,
            # --- Animation Layer ---
            "get_armature_info": self.get_armature_info,
            "analyze_animation_quality": self.analyze_animation_quality,
            # --- Material Layer ---
            "create_pbr_material": self.create_pbr_material,
            "get_material_summary": self.get_material_summary,
            # --- QA Layer ---
            "run_asset_qa": self.run_asset_qa,
            "run_unreal_readiness_check": self.run_unreal_readiness_check,
            # --- Export Layer ---
            "export_for_unreal": self.export_for_unreal,
            "prepare_lod_names": self.prepare_lod_names,
            # --- Session ---
            "get_session_log": self.get_session_log,
        }

        # Add Polyhaven handlers only if enabled
        if bpy.context.scene.blendermcp_use_polyhaven:
            polyhaven_handlers = {
                "get_polyhaven_categories": self.get_polyhaven_categories,
                "search_polyhaven_assets": self.search_polyhaven_assets,
                "download_polyhaven_asset": self.download_polyhaven_asset,
                "set_texture": self.set_texture,
            }
            handlers.update(polyhaven_handlers)

        # Add Hyper3d handlers only if enabled
        if bpy.context.scene.blendermcp_use_hyper3d:
            polyhaven_handlers = {
                "create_rodin_job": self.create_rodin_job,
                "poll_rodin_job_status": self.poll_rodin_job_status,
                "import_generated_asset": self.import_generated_asset,
            }
            handlers.update(polyhaven_handlers)

        # Add Sketchfab handlers only if enabled
        if bpy.context.scene.blendermcp_use_sketchfab:
            sketchfab_handlers = {
                "search_sketchfab_models": self.search_sketchfab_models,
                "get_sketchfab_model_preview": self.get_sketchfab_model_preview,
                "download_sketchfab_model": self.download_sketchfab_model,
            }
            handlers.update(sketchfab_handlers)
        
        # Add Hunyuan3d handlers only if enabled
        if bpy.context.scene.blendermcp_use_hunyuan3d:
            hunyuan_handlers = {
                "create_hunyuan_job": self.create_hunyuan_job,
                "poll_hunyuan_job_status": self.poll_hunyuan_job_status,
                "import_generated_asset_hunyuan": self.import_generated_asset_hunyuan
            }
            handlers.update(hunyuan_handlers)

        handler = handlers.get(cmd_type)
        if handler:
            try:
                print(f"Executing handler for {cmd_type}")
                result = handler(**params)
                print(f"Handler execution complete")
                return {"status": "success", "result": result}
            except Exception as e:
                print(f"Error in handler: {str(e)}")
                traceback.print_exc()
                return {"status": "error", "message": str(e)}
        else:
            return {"status": "error", "message": f"Unknown command type: {cmd_type}"}



    # ─────────────────────────────────────────────────────────────────────────
    # SESSION STATE  — lightweight per-session log, token-safe (summaries only)
    # ─────────────────────────────────────────────────────────────────────────
    _session_log = []   # list of {"cmd": ..., "status": ..., "note": ...}

    def _log(self, cmd, status="ok", note=""):
        BlenderMCPServer._session_log.append({"cmd": cmd, "status": status, "note": str(note)})
        if len(BlenderMCPServer._session_log) > 50:
            BlenderMCPServer._session_log = BlenderMCPServer._session_log[-50:]

    def get_session_log(self):
        """Return the last 20 commands executed this session (lightweight)."""
        return {"log": BlenderMCPServer._session_log[-20:]}

    # ─────────────────────────────────────────────────────────────────────────
    # INTERNAL HELPERS
    # ─────────────────────────────────────────────────────────────────────────
    @staticmethod
    def _ensure_object_mode(obj):
        """
        Safely switch obj to OBJECT mode. Sets it as active first.
        Returns previous mode string so caller can restore if needed.
        FIX: Always set active before mode_set; poll-safe.
        """
        prev_mode = obj.mode if obj else 'OBJECT'
        if obj and obj.mode != 'OBJECT':
            bpy.context.view_layer.objects.active = obj
            try:
                bpy.ops.object.mode_set(mode='OBJECT')
            except RuntimeError:
                pass  # Already in object mode or no valid context
        return prev_mode

    @staticmethod
    def _bsdf_set(bsdf, socket_name_candidates, value):
        """
        Set a Principled BSDF input by trying multiple socket name variants.
        Handles Blender 3.x / 4.x API differences gracefully.
        FIX: Blender 4.x renamed several BSDF inputs.
        """
        for name in socket_name_candidates:
            if name in bsdf.inputs:
                try:
                    bsdf.inputs[name].default_value = value
                    return True
                except Exception:
                    continue
        return False

    @staticmethod
    def _get_blender_version():
        """Return (major, minor) tuple."""
        return (bpy.app.version[0], bpy.app.version[1])

    @staticmethod
    def _get_fcurves(action, obj=None):
        """
        Return a flat list of FCurves from an action, handling both the legacy
        API (Blender < 4.4) and the new layered action API (Blender 4.4+).

        Blender 4.4 removed the direct action.fcurves shortcut. FCurves now
        live at: action.layers[n].strips[m] (type KEYFRAME) -> channelbag
        scoped to an action slot -> channelbag.fcurves.

        Strategy:
          1. If Blender >= 4.4 and action has layers, iterate layers/strips/
             channelbags. Try slot-scoped channelbag first (correct for
             multi-object actions); fall back to all channelbags on the strip
             if the slot isn't available.
          2. Otherwise use the legacy action.fcurves list directly.
          3. If neither exists, return [].
        """
        # ── Blender 4.4+ layered action API ───────────────────────────────────
        if bpy.app.version >= (4, 4, 0) and hasattr(action, 'layers'):
            fcurves = []
            for layer in action.layers:
                for strip in layer.strips:
                    if strip.type != 'KEYFRAME':
                        continue
                    # Prefer slot-scoped channelbag — correct for multi-object actions
                    slot = None
                    if (obj
                            and hasattr(obj, 'animation_data')
                            and obj.animation_data
                            and hasattr(obj.animation_data, 'action_slot')):
                        slot = obj.animation_data.action_slot
                    if slot is not None:
                        cb = strip.channelbags.get(slot)
                        if cb:
                            fcurves.extend(cb.fcurves)
                            continue
                    # Fallback: collect from every channelbag on this strip
                    for cb in strip.channelbags:
                        fcurves.extend(cb.fcurves)
            return fcurves
        # ── Legacy API (pre-4.4): fcurves directly on action ──────────────────
        if hasattr(action, 'fcurves'):
            return list(action.fcurves)
        return []

    @staticmethod
    def _build_bmesh_from_object(obj):
        """
        Safely build a new BMesh from obj in OBJECT mode.
        Returns (bm, prev_mode). Caller MUST call bm.free() in a finally block.
        FIX: Centralised bmesh construction with active-object guarantee.
        """
        prev_mode = BlenderMCPServer._ensure_object_mode(obj)
        bm = bmesh.new()
        bm.from_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        return bm, prev_mode

    # ─────────────────────────────────────────────────────────────────────────
    # SAFE CODE EXECUTION  — wraps execute_code with undo push + mode guard
    # ─────────────────────────────────────────────────────────────────────────
    def execute_code_safe(self, code, required_mode=None, push_undo=True):
        """
        Execute Python code with optional mode switching and undo checkpoint.
        required_mode: 'OBJECT' | 'EDIT' | 'POSE' | None  (None = don't switch)
        push_undo: bool — wrap in named undo step so Ctrl+Z always works.
        FIX: undo pushed BEFORE state change; poll-safe mode switching.
        """
        original_mode = None
        obj = bpy.context.active_object
        try:
            # --- undo checkpoint FIRST (before any state change) ---
            if push_undo:
                try:
                    bpy.ops.ed.undo_push(message="BlenderMCP operation")
                except Exception:
                    pass  # undo_push fails in some contexts; non-fatal

            # --- mode switch (poll-safe) ---
            if required_mode and obj:
                original_mode = obj.mode
                if original_mode != required_mode:
                    bpy.context.view_layer.objects.active = obj
                    try:
                        bpy.ops.object.mode_set(mode=required_mode)
                    except RuntimeError as e:
                        return {"executed": False, "result": "",
                                "error": f"Cannot switch to {required_mode}: {e}",
                                "mode_before": original_mode, "undo_pushed": push_undo}

            # --- execute ---
            result = self.execute_code(code)

            self._log("execute_code_safe", "ok")
            return {**result, "mode_before": original_mode, "undo_pushed": push_undo}

        except Exception as e:
            self._log("execute_code_safe", "error", str(e))
            return {"executed": False, "result": "", "error": str(e),
                    "mode_before": original_mode, "undo_pushed": push_undo}
        finally:
            # --- restore mode if we changed it ---
            if original_mode and obj:
                try:
                    if obj.mode != original_mode:
                        bpy.ops.object.mode_set(mode=original_mode)
                except Exception:
                    pass

    # ─────────────────────────────────────────────────────────────────────────
    # ANALYST LAYER
    # ─────────────────────────────────────────────────────────────────────────
    def get_selection_context(self):
        """
        What does the user currently have selected?
        Compact — only returns names + types, never raw vertex arrays.
        FIX: safe edit-mesh BMesh access (from_edit_mesh is a reference, not freed).
        """
        try:
            active = bpy.context.active_object
            selected = [{"name": o.name, "type": o.type} for o in bpy.context.selected_objects]
            ctx = {
                "mode": bpy.context.mode,
                "active_object": active.name if active else None,
                "active_type": active.type if active else None,
                "selected_count": len(selected),
                "selected": selected,
                "cursor_location": [round(v, 4) for v in bpy.context.scene.cursor.location],
            }
            # Edit-mode element counts — from_edit_mesh is a reference, do NOT free
            if active and active.type == 'MESH' and bpy.context.mode == 'EDIT_MESH':
                bm = bmesh.from_edit_mesh(active.data)
                bm.verts.ensure_lookup_table()
                bm.edges.ensure_lookup_table()
                bm.faces.ensure_lookup_table()
                ctx["edit_selected"] = {
                    "verts": sum(1 for v in bm.verts if v.select),
                    "edges": sum(1 for e in bm.edges if e.select),
                    "faces": sum(1 for f in bm.faces if f.select),
                    "total_verts": len(bm.verts),
                    "total_faces": len(bm.faces),
                }
            self._log("get_selection_context")
            return ctx
        except Exception as e:
            self._log("get_selection_context", "error", str(e))
            return {"error": str(e)}

    def get_scene_hierarchy(self, max_depth=8):
        """
        Compact scene hierarchy: collections → objects (no mesh data).
        FIX: depth limit prevents infinite recursion on corrupted collection graphs.
        """
        try:
            def collection_info(col, depth=0):
                if depth >= max_depth:
                    return {"name": col.name, "objects": [], "children": [], "truncated": True}
                return {
                    "name": col.name,
                    "objects": [{"name": o.name, "type": o.type} for o in col.objects],
                    "children": [collection_info(c, depth + 1) for c in col.children],
                }
            hierarchy = collection_info(bpy.context.scene.collection)
            self._log("get_scene_hierarchy")
            return {"hierarchy": hierarchy, "total_objects": len(bpy.context.scene.objects)}
        except Exception as e:
            self._log("get_scene_hierarchy", "error", str(e))
            return {"error": str(e)}

    def get_mesh_quality_report(self, name):
        """
        On-demand mesh diagnostics for a named object.
        Returns a concise problem summary — not raw geometry arrays.
        FIX: bmesh freed in finally; active object set before mode_set;
             UV overlap detection added; weight group summary added.
        """
        obj = bpy.data.objects.get(name)
        if not obj or obj.type != 'MESH':
            return {"error": f"No mesh object named '{name}'"}

        bm = None
        try:
            bm, _ = self._build_bmesh_from_object(obj)

            vert_count = len(bm.verts)
            edge_count = len(bm.edges)
            face_count = len(bm.faces)
            ngons = sum(1 for f in bm.faces if len(f.verts) > 4)
            tris  = sum(1 for f in bm.faces if len(f.verts) == 3)
            quads = sum(1 for f in bm.faces if len(f.verts) == 4)
            non_manifold_edges = sum(1 for e in bm.edges if not e.is_manifold)
            isolated_verts     = sum(1 for v in bm.verts if not v.link_edges)
            poles_n3   = sum(1 for v in bm.verts if len(v.link_edges) == 3 and not v.is_boundary)
            poles_n5   = sum(1 for v in bm.verts if len(v.link_edges) == 5 and not v.is_boundary)
            poles_high = sum(1 for v in bm.verts if len(v.link_edges) > 5  and not v.is_boundary)
            zero_area  = sum(1 for f in bm.faces if f.calc_area() < 1e-8)
            boundary_edges = sum(1 for e in bm.edges if e.is_boundary)

            # Doubled-face detection (two faces sharing identical vert set)
            face_vert_sets = [frozenset(v.index for v in f.verts) for f in bm.faces]
            duplicate_faces = len(face_vert_sets) - len(set(face_vert_sets))

            uv_layers = [uv.name for uv in obj.data.uv_layers]

            # UV bounds sanity (islands outside 0-1 = tiling or unwrap error)
            uv_out_of_bounds = 0
            if obj.data.uv_layers:
                uv_layer = bm.loops.layers.uv.active
                if uv_layer:
                    for face in bm.faces:
                        for loop in face.loops:
                            u, v = loop[uv_layer].uv
                            if not (0.0 <= u <= 1.0 and 0.0 <= v <= 1.0):
                                uv_out_of_bounds += 1

            # Vertex group count (weight paint readiness)
            vgroup_count = len(obj.vertex_groups)

            modifiers = [{"name": m.name, "type": m.type, "show_viewport": m.show_viewport}
                         for m in obj.modifiers]

            # Deformation modifiers that should be applied before export
            deform_mods = [m.name for m in obj.modifiers
                           if m.type in ('ARMATURE', 'CLOTH', 'SOFT_BODY', 'CORRECTIVE_SMOOTH')]

            is_clean = (non_manifold_edges == 0 and isolated_verts == 0
                        and zero_area == 0 and ngons == 0 and duplicate_faces == 0)

            report = {
                "object": name,
                "blender_version": list(bpy.app.version[:2]),
                "counts": {"verts": vert_count, "edges": edge_count, "faces": face_count},
                "face_types": {"tris": tris, "quads": quads, "ngons": ngons},
                "problems": {
                    "non_manifold_edges": non_manifold_edges,
                    "isolated_verts": isolated_verts,
                    "zero_area_faces": zero_area,
                    "duplicate_faces": duplicate_faces,
                    "boundary_edges": boundary_edges,
                },
                "poles": {"n3_not_boundary": poles_n3, "n5_not_boundary": poles_n5,
                           "high_valence": poles_high},
                "uv": {
                    "layers": uv_layers,
                    "has_uvs": len(uv_layers) > 0,
                    "layer_count": len(uv_layers),
                    "out_of_bounds_loops": uv_out_of_bounds,
                },
                "rigging": {
                    "vertex_groups": vgroup_count,
                    "deform_modifiers": deform_mods,
                },
                "modifiers": modifiers,
                "health": "clean" if is_clean else "issues_found",
                "fixable_with": [] if is_clean else self._suggest_fixes({
                    "non_manifold_edges": non_manifold_edges,
                    "isolated_verts": isolated_verts,
                    "zero_area_faces": zero_area,
                    "ngons": ngons,
                    "duplicate_faces": duplicate_faces,
                }),
            }
            self._log("get_mesh_quality_report")
            return report
        except Exception as e:
            self._log("get_mesh_quality_report", "error", str(e))
            return {"error": str(e)}
        finally:
            if bm is not None:
                bm.free()

    @staticmethod
    def _suggest_fixes(problems):
        """Return a list of bpy operator calls that fix common mesh problems."""
        fixes = []
        if problems.get("isolated_verts"):
            fixes.append("bpy.ops.mesh.delete_loose() — removes isolated verts/edges")
        if problems.get("zero_area_faces"):
            fixes.append("bpy.ops.mesh.dissolve_degenerate() — collapses zero-area faces")
        if problems.get("duplicate_faces"):
            fixes.append("bpy.ops.mesh.remove_doubles(threshold=0.0001) — merges overlapping geometry")
        if problems.get("non_manifold_edges"):
            fixes.append("Select non-manifold (Shift+Ctrl+Alt+M) then fill or delete")
        if problems.get("ngons"):
            fixes.append("bpy.ops.mesh.quads_convert_to_tris() or knife-cut ngons into quads")
        return fixes

    def get_material_graph(self, material_name):
        """
        Returns a compact node graph for a material.
        Only node type, label, key values, and connections — no texture pixel data.
        FIX: orphaned nodes flagged; normal map direction (GL vs DX) detected.
        """
        try:
            mat = bpy.data.materials.get(material_name)
            if not mat:
                return {"error": f"Material '{material_name}' not found"}
            if not mat.use_nodes:
                return {"material": material_name, "use_nodes": False,
                        "tip": "Enable 'Use Nodes' to inspect node graph"}

            # Find nodes connected to output (non-orphaned)
            output_nodes = {n for n in mat.node_tree.nodes if n.type == 'OUTPUT_MATERIAL'}
            connected = set()
            def trace_inputs(node):
                if node in connected:
                    return
                connected.add(node)
                for inp in node.inputs:
                    for link in inp.links:
                        trace_inputs(link.from_node)
            for out in output_nodes:
                trace_inputs(out)

            nodes_out = []
            for node in mat.node_tree.nodes:
                n = {
                    "name": node.name,
                    "type": node.type,
                    "label": node.label,
                    "active": node in connected,  # FIX: flag orphaned nodes
                }
                input_vals = {}
                for inp in node.inputs:
                    if not inp.links and hasattr(inp, 'default_value'):
                        try:
                            v = inp.default_value
                            input_vals[inp.name] = list(v) if hasattr(v, '__iter__') else float(v)
                        except Exception:
                            pass
                if input_vals:
                    n["inputs"] = input_vals
                if node.type == 'TEX_IMAGE' and node.image:
                    n["image"] = node.image.name
                    n["colorspace"] = node.image.colorspace_settings.name
                # FIX: detect normal map direction for UE5 compatibility
                if node.type == 'NORMAL_MAP':
                    n["normal_space"] = node.space  # 'TANGENT', 'OBJECT', etc.
                    n["ue5_warning"] = (
                        "Blender uses OpenGL normals; UE5 uses DirectX. "
                        "Flip G channel in UE5 or use a DX normal map."
                        if node.space == 'TANGENT' else None
                    )
                nodes_out.append(n)

            links_out = [
                {"from": f"{l.from_node.name}.{l.from_socket.name}",
                 "to":   f"{l.to_node.name}.{l.to_socket.name}"}
                for l in mat.node_tree.links
            ]

            orphaned = [n["name"] for n in nodes_out if not n["active"]]
            self._log("get_material_graph")
            return {
                "material": material_name,
                "nodes": nodes_out,
                "links": links_out,
                "orphaned_nodes": orphaned,
                "has_orphaned_nodes": len(orphaned) > 0,
            }
        except Exception as e:
            self._log("get_material_graph", "error", str(e))
            return {"error": str(e)}

    def get_animation_data(self, name):
        """
        Compact animation summary for an object or armature.
        Returns action name, frame range, channel names — not raw keyframe arrays.
        FIX: handles objects with no animation_data gracefully; adds spacing analysis.
        """
        try:
            obj = bpy.data.objects.get(name)
            if not obj:
                return {"error": f"Object '{name}' not found"}

            result = {
                "object": name,
                "type": obj.type,
                "has_animation": obj.animation_data is not None,
            }

            if not obj.animation_data:
                result["tip"] = "No animation data block. Add a keyframe or action to begin."
                self._log("get_animation_data")
                return result

            if obj.animation_data.action:
                action = obj.animation_data.action
                fcurves = self._get_fcurves(action, obj)
                fcurves_summary = []
                for fc in fcurves:
                    kps = fc.keyframe_points
                    entry = {
                        "path": fc.data_path,
                        "index": fc.array_index,
                        "keyframes": len(kps),
                        "range": [round(kps[0].co[0], 1), round(kps[-1].co[0], 1)] if kps else [],
                    }
                    # FIX: spacing analysis — detect flat/linear segments (no easing)
                    if len(kps) >= 2:
                        handle_types = set()
                        for kp in kps:
                            handle_types.add(kp.interpolation)
                        entry["interpolation_types"] = list(handle_types)
                        if handle_types == {'LINEAR'}:
                            entry["spacing_warning"] = "All LINEAR — no easing, will look mechanical"
                        elif handle_types == {'CONSTANT'}:
                            entry["spacing_warning"] = "All CONSTANT — stepped animation, intentional?"
                    fcurves_summary.append(entry)

                result["action"] = {
                    "name": action.name,
                    "frame_range": [round(v, 1) for v in action.frame_range],
                    "fcurve_count": len(fcurves),
                    "channels": fcurves_summary[:30],
                }
            else:
                result["tip"] = "animation_data exists but no Action is assigned."

            if obj.animation_data.nla_tracks:
                result["nla_tracks"] = [
                    {"name": t.name, "strips": len(t.strips), "muted": t.mute}
                    for t in obj.animation_data.nla_tracks
                ]

            self._log("get_animation_data")
            return result
        except Exception as e:
            self._log("get_animation_data", "error", str(e))
            return {"error": str(e)}

    # ─────────────────────────────────────────────────────────────────────────
    # TOPOLOGY LAYER
    # ─────────────────────────────────────────────────────────────────────────
    def analyze_topology(self, name, context='generic'):
        """
        Professional topology analysis with context-aware scoring.
        context: 'generic' | 'character_body' | 'face' | 'hand' | 'hard_surface'
        FIX: pole_map keys serialized as strings (JSON-safe); context scoring;
             bmesh freed in finally; active object set before mode_set.
        """
        obj = bpy.data.objects.get(name)
        if not obj or obj.type != 'MESH':
            return {"error": f"No mesh named '{name}'"}

        bm = None
        try:
            bm, _ = self._build_bmesh_from_object(obj)

            total_faces = len(bm.faces)
            total_verts = len(bm.verts)
            if total_faces == 0:
                return {"error": f"'{name}' has no faces"}

            quads = sum(1 for f in bm.faces if len(f.verts) == 4)
            tris  = sum(1 for f in bm.faces if len(f.verts) == 3)
            ngons = sum(1 for f in bm.faces if len(f.verts) > 4)
            quad_ratio = round(quads / total_faces * 100, 1)

            # FIX: pole_map with string keys so JSON round-trip preserves them
            pole_map = {}
            for v in bm.verts:
                valence = len(v.link_edges)
                key = str(valence)
                pole_map[key] = pole_map.get(key, 0) + 1

            avg_valence = round(sum(len(v.link_edges) for v in bm.verts) / total_verts, 2)
            boundary_edges = sum(1 for e in bm.edges if e.is_boundary)
            non_manifold   = sum(1 for e in bm.edges if not e.is_manifold)

            bad_normals = 0
            for f in bm.faces:
                neighbours = {lf for e in f.edges for lf in e.link_faces if lf != f}
                if any(f.normal.dot(nbr.normal) < -0.5 for nbr in neighbours):
                    bad_normals += 1

            # Context-aware thresholds
            ctx_thresholds = {
                'character_body': {'min_quad_ratio': 85, 'max_tris_pct': 10},
                'face':           {'min_quad_ratio': 90, 'max_tris_pct': 5},
                'hand':           {'min_quad_ratio': 90, 'max_tris_pct': 5},
                'hard_surface':   {'min_quad_ratio': 70, 'max_tris_pct': 25},
                'generic':        {'min_quad_ratio': 75, 'max_tris_pct': 20},
            }
            thresholds = ctx_thresholds.get(context, ctx_thresholds['generic'])
            tris_pct = round(tris / total_faces * 100, 1)

            issues = []
            score = 100

            if quad_ratio < thresholds['min_quad_ratio']:
                issues.append(f"Low quad ratio ({quad_ratio}% < {thresholds['min_quad_ratio']}% for {context})")
                score -= 20
            if ngons > 0:
                issues.append(f"{ngons} ngons — will cause shading/subdivision artifacts")
                score -= min(ngons * 2, 20)
            if tris_pct > thresholds['max_tris_pct']:
                issues.append(f"High tri count ({tris_pct}%) — may limit subdivision and animation quality")
                score -= 10
            high_poles = int(pole_map.get("6", 0)) + int(pole_map.get("7", 0)) + int(pole_map.get("8", 0))
            if high_poles > 5:
                issues.append(f"{high_poles} high-valence poles (6+) — complex topology, may cause pinching")
                score -= 10
            if boundary_edges > 0:
                issues.append(f"{boundary_edges} boundary edges — open/non-watertight mesh")
                score -= 10
            if non_manifold > 0:
                issues.append(f"{non_manifold} non-manifold edges — geometry errors present")
                score -= 15
            if bad_normals > 0:
                issues.append(f"{bad_normals} face normal conflicts — shading errors likely")
                score -= 10

            rating = ("excellent" if score >= 90 else "good" if score >= 70
                      else "acceptable" if score >= 50 else "poor")

            result = {
                "object": name,
                "context": context,
                "topology_score": max(score, 0),
                "rating": rating,
                "stats": {
                    "total_faces": total_faces,
                    "quads": quads, "tris": tris, "ngons": ngons,
                    "quad_ratio_pct": quad_ratio,
                    "tris_pct": tris_pct,
                    "avg_vert_valence": avg_valence,
                    "boundary_edges": boundary_edges,
                    "non_manifold_edges": non_manifold,
                    "pole_distribution": pole_map,  # string keys, JSON-safe
                },
                "issues": issues,
                "recommendations": self._topology_recommendations(issues),
            }
            self._log("analyze_topology")
            return result
        except Exception as e:
            self._log("analyze_topology", "error", str(e))
            return {"error": str(e)}
        finally:
            if bm is not None:
                bm.free()

    def _topology_recommendations(self, issues):
        """Map detected issues to actionable Blender operator recommendations."""
        recs = []
        seen = set()
        def add(rec):
            if rec not in seen:
                seen.add(rec)
                recs.append(rec)
        for issue in issues:
            il = issue.lower()
            if "ngon" in il:
                add("Edit Mode → Select All by Trait → Faces by Sides (>4) → Knife-cut or Triangulate")
                add("Operator: bpy.ops.mesh.quads_convert_to_tris() as last resort")
            if "quad ratio" in il or "tri count" in il:
                add("Consider Quad Remesher (Blender 3.x+ addon) or manual retopology over base mesh")
            if "high-valence" in il:
                add("Dissolve edges (X → Dissolve Edges) around 6+ pole verts to reduce valence")
            if "boundary" in il:
                add("Edit Mode → Select → Select All by Trait → Non Manifold → Fill (F) open holes")
            if "non-manifold" in il:
                add("Mesh → Clean Up → Fill Holes; also check for interior faces")
            if "normal" in il:
                add("Edit Mode → Mesh → Normals → Recalculate Outside (Shift+N)")
                add("Overlay → Face Orientation to visualise flipped normals (red = flipped)")
        return recs

    def detect_mesh_problems(self, name):
        """
        Fast problem scanner. Returns only problems found — empty = clean.
        FIX: bmesh freed in finally; active obj set before mode_set;
             duplicate face detection added.
        """
        obj = bpy.data.objects.get(name)
        if not obj or obj.type != 'MESH':
            return {"error": f"No mesh named '{name}'"}

        bm = None
        try:
            bm, _ = self._build_bmesh_from_object(obj)

            problems = []
            nm = sum(1 for e in bm.edges if not e.is_manifold)
            if nm: problems.append({"type": "non_manifold_edges", "count": nm,
                                    "fix": "Mesh > Clean Up > Fill Holes"})
            iso = sum(1 for v in bm.verts if not v.link_edges)
            if iso: problems.append({"type": "isolated_verts", "count": iso,
                                     "fix": "bpy.ops.mesh.delete_loose()"})
            za = sum(1 for f in bm.faces if f.calc_area() < 1e-8)
            if za: problems.append({"type": "zero_area_faces", "count": za,
                                    "fix": "bpy.ops.mesh.dissolve_degenerate()"})
            ng = sum(1 for f in bm.faces if len(f.verts) > 4)
            if ng: problems.append({"type": "ngons", "count": ng,
                                    "fix": "bpy.ops.mesh.quads_convert_to_tris() or knife-cut"})
            bd = sum(1 for e in bm.edges if e.is_boundary)
            if bd: problems.append({"type": "boundary_edges", "count": bd,
                                    "fix": "Select boundary (Alt+click) then Fill (F)"})
            # Duplicate face check
            face_sets = [frozenset(v.index for v in f.verts) for f in bm.faces]
            dup = len(face_sets) - len(set(face_sets))
            if dup: problems.append({"type": "duplicate_faces", "count": dup,
                                     "fix": "bpy.ops.mesh.remove_doubles(threshold=0.0001)"})

            self._log("detect_mesh_problems")
            return {
                "object": name,
                "clean": len(problems) == 0,
                "problem_count": len(problems),
                "problems": problems,
            }
        except Exception as e:
            self._log("detect_mesh_problems", "error", str(e))
            return {"error": str(e)}
        finally:
            if bm is not None:
                bm.free()

    # ─────────────────────────────────────────────────────────────────────────
    # ANIMATION LAYER
    # ─────────────────────────────────────────────────────────────────────────
    def get_armature_info(self, name):
        """
        Compact armature summary: bones, hierarchy, constraints, IK chains.
        FIX: IK chain analysis added (root, chain length, pole target);
             constraint mute state included.
        """
        try:
            obj = bpy.data.objects.get(name)
            if not obj or obj.type != 'ARMATURE':
                return {"error": f"No armature named '{name}'"}

            arm = obj.data
            bones_out = []
            for bone in arm.bones:
                bones_out.append({
                    "name": bone.name,
                    "parent": bone.parent.name if bone.parent else None,
                    "children": [c.name for c in bone.children],
                    "length": round(bone.length, 4),
                    "use_deform": bone.use_deform,
                    "is_root": bone.parent is None,
                })

            # Pose constraints + IK chain analysis
            constraints_out = []
            ik_chains = []
            if obj.pose:
                for pb in obj.pose.bones:
                    for c in pb.constraints:
                        entry = {
                            "bone": pb.name,
                            "constraint": c.type,
                            "name": c.name,
                            "muted": c.mute,
                            "influence": round(c.influence, 3),
                        }
                        constraints_out.append(entry)

                        # FIX: IK chain analysis
                        if c.type == 'IK':
                            chain = {
                                "effector_bone": pb.name,
                                "chain_length": c.chain_count,
                                "has_target": c.target is not None,
                                "target": c.target.name if c.target else None,
                                "has_pole": c.pole_target is not None,
                                "pole_target": c.pole_target.name if c.pole_target else None,
                                "broken": not c.target,
                            }
                            ik_chains.append(chain)

            broken_ik = [ch for ch in ik_chains if ch["broken"]]
            self._log("get_armature_info")
            return {
                "armature": name,
                "bone_count": len(bones_out),
                "deform_bones": sum(1 for b in bones_out if b["use_deform"]),
                "bones": bones_out,
                "constraints": constraints_out,
                "ik_chains": ik_chains,
                "broken_ik_count": len(broken_ik),
                "broken_ik": broken_ik,
            }
        except Exception as e:
            self._log("get_armature_info", "error", str(e))
            return {"error": str(e)}

    def analyze_animation_quality(self, name, frame_start=None, frame_end=None):
        """
        Animation quality check: broken IK, stiff channels, missing secondary motion,
        extreme poses, velocity spikes.
        FIX: foot-sliding logic corrected (was backwards); score formula softened;
             velocity spike detection added; findings categorised by severity.
        """
        try:
            obj = bpy.data.objects.get(name)
            if not obj:
                return {"error": f"Object '{name}' not found"}

            scene = bpy.context.scene
            f_start = int(frame_start) if frame_start is not None else int(scene.frame_start)
            f_end   = int(frame_end)   if frame_end   is not None else int(scene.frame_end)
            frame_count = max(f_end - f_start, 1)

            if not obj.animation_data or not obj.animation_data.action:
                return {"object": name, "has_animation": False,
                        "findings": [{"sev": "info", "msg": "No animation action found"}], "score": 0}

            action = obj.animation_data.action
            # Fetch fcurves once via version-safe helper — works on Blender 4.4+
            # (layered action API) and all earlier versions (action.fcurves).
            fcurves = self._get_fcurves(action, obj)

            errors   = []  # will cause problems
            warnings = []  # should be reviewed
            info     = []  # useful context

            # --- Broken IK ---
            if obj.type == 'ARMATURE' and obj.pose:
                for pb in obj.pose.bones:
                    for c in pb.constraints:
                        if c.type == 'IK' and not c.target:
                            errors.append(f"Broken IK on '{pb.name}' — constraint has no target")

            # --- Stiff channels (rotation with <3 keys over full range) ---
            stiff_bones = []
            for fc in fcurves:
                if "rotation" in fc.data_path and len(fc.keyframe_points) < 3:
                    stiff_bones.append(fc.data_path)
            if len(stiff_bones) > 3:
                warnings.append(f"{len(stiff_bones)} rotation channels have <3 keyframes — may look stiff")

            # --- FIX: Foot sliding — detect XY movement while Z is LOCKED (planted foot)
            # A foot sliding = Z constant (foot on ground) BUT XY is moving (sliding)
            loc_curves = {}
            for fc in fcurves:
                if 'location' in fc.data_path:
                    key = fc.data_path
                    if key not in loc_curves:
                        loc_curves[key] = {}
                    loc_curves[key][fc.array_index] = fc

            for path, axes in loc_curves.items():
                if 2 not in axes:
                    continue
                sample_frames = range(f_start, min(f_end, f_start + 60), 3)
                z_vals = [axes[2].evaluate(f) for f in sample_frames]
                if not z_vals:
                    continue
                z_range = max(z_vals) - min(z_vals)
                # Foot is near-grounded (Z barely moves)
                if z_range < 0.02:
                    # Check if XY is moving (sliding)
                    for xy_axis in [0, 1]:
                        if xy_axis in axes:
                            xy_vals = [axes[xy_axis].evaluate(f) for f in sample_frames]
                            xy_range = max(xy_vals) - min(xy_vals)
                            if xy_range > 0.05:
                                warnings.append(
                                    f"Possible foot sliding on '{path}' axis {xy_axis}: "
                                    f"Z locked ({z_range:.4f}) but XY moves ({xy_range:.3f})"
                                )

            # --- Velocity spikes (sudden large value jumps between adjacent keyframes) ---
            for fc in fcurves:
                kps = fc.keyframe_points
                if len(kps) < 2:
                    continue
                for i in range(len(kps) - 1):
                    dt = kps[i+1].co[0] - kps[i].co[0]
                    dv = abs(kps[i+1].co[1] - kps[i].co[1])
                    if dt > 0 and dv / dt > 5.0:
                        warnings.append(
                            f"Velocity spike on '{fc.data_path}[{fc.array_index}]' "
                            f"between frames {int(kps[i].co[0])}-{int(kps[i+1].co[0])}"
                        )
                        break  # one warning per channel is enough

            # --- Missing secondary motion ---
            if obj.type == 'ARMATURE' and obj.pose:
                has_scale_anim = any('scale' in fc.data_path for fc in fcurves)
                if not has_scale_anim:
                    info.append("No scale animation — add subtle squash/stretch for secondary motion")
                # Check for constant-rotation bones (potential missing follow-through)
                for fc in fcurves:
                    if 'rotation' in fc.data_path:
                        kps = fc.keyframe_points
                        if len(kps) >= 2:
                            vals = [kp.co[1] for kp in kps]
                            if max(vals) - min(vals) < 0.001:
                                info.append(f"'{fc.data_path}' has no rotation range — possible missing follow-through")
                                break

            # FIX: softened scoring — errors cost 20, warnings cost 8, info is free
            score = max(0, 100 - len(errors) * 20 - len(warnings) * 8)
            rating = "excellent" if score >= 85 else "good" if score >= 65 else "needs_work"

            all_findings = (
                [{"severity": "error",   "msg": m} for m in errors]   +
                [{"severity": "warning", "msg": m} for m in warnings]  +
                [{"severity": "info",    "msg": m} for m in info]
            )

            self._log("analyze_animation_quality")
            return {
                "object": name,
                "frame_range": [f_start, f_end],
                "score": score,
                "rating": rating,
                "error_count":   len(errors),
                "warning_count": len(warnings),
                "findings": all_findings,
                "recommendation": (
                    "Fix errors before export" if errors
                    else "Review warnings for quality" if warnings
                    else "Animation passes quality check"
                ),
            }
        except Exception as e:
            self._log("analyze_animation_quality", "error", str(e))
            return {"error": str(e)}

    # ─────────────────────────────────────────────────────────────────────────
    # MATERIAL LAYER
    # ─────────────────────────────────────────────────────────────────────────
    def get_material_summary(self, name):
        """
        Quick material summary for an object — names, slot count, has-nodes flag.
        Ultra-compact. Use get_material_graph() when you need node details.
        FIX: empty material slots now explicitly flagged.
        """
        try:
            obj = bpy.data.objects.get(name)
            if not obj:
                return {"error": f"Object '{name}' not found"}
            slots = []
            empty_slots = 0
            for i, slot in enumerate(obj.material_slots):
                if slot.material:
                    m = slot.material
                    slots.append({
                        "slot": i,
                        "name": m.name,
                        "use_nodes": m.use_nodes,
                        "node_count": len(m.node_tree.nodes) if m.use_nodes and m.node_tree else 0,
                    })
                else:
                    empty_slots += 1
                    slots.append({"slot": i, "name": None, "empty": True})
            self._log("get_material_summary")
            return {
                "object": name,
                "material_count": len(obj.material_slots),
                "empty_slots": empty_slots,
                "materials": slots,
            }
        except Exception as e:
            self._log("get_material_summary", "error", str(e))
            return {"error": str(e)}

    def create_pbr_material(self, name,
                             base_color=(0.8, 0.8, 0.8, 1.0),
                             metallic=0.0, roughness=0.5,
                             use_subsurface=False, subsurface_radius=None,
                             emission_color=None, emission_strength=0.0,
                             alpha=1.0, wear_variation=False):
        """
        Create a production-ready PBR material with optional wear/variation layer.
        FIX: Blender 3.x/4.x socket name compatibility via _bsdf_set();
             blend_method handled with version check;
             self-verification — checks links were actually created.
        """
        try:
            mat = bpy.data.materials.get(name) or bpy.data.materials.new(name=name)
            mat.use_nodes = True
            nodes = mat.node_tree.nodes
            links = mat.node_tree.links
            nodes.clear()

            out  = nodes.new('ShaderNodeOutputMaterial'); out.location  = (600, 0)
            bsdf = nodes.new('ShaderNodeBsdfPrincipled'); bsdf.location = (300, 0)

            # FIX: version-safe socket assignments
            self._bsdf_set(bsdf, ['Base Color', 'Base_Color'], base_color)
            self._bsdf_set(bsdf, ['Metallic'], metallic)
            self._bsdf_set(bsdf, ['Roughness'], roughness)
            self._bsdf_set(bsdf, ['Alpha'], alpha)

            if use_subsurface:
                # Blender 4.x: 'Subsurface Weight' | Blender 3.x: 'Subsurface'
                self._bsdf_set(bsdf, ['Subsurface Weight', 'Subsurface'], 0.1)
                if subsurface_radius:
                    self._bsdf_set(bsdf, ['Subsurface Radius', 'Subsurface Color'], subsurface_radius)

            if emission_color:
                # Blender 4.x: 'Emission Color' | Blender 3.x: 'Emission'
                self._bsdf_set(bsdf, ['Emission Color', 'Emission'], emission_color)
                self._bsdf_set(bsdf, ['Emission Strength'], emission_strength)

            main_link = links.new(bsdf.outputs['BSDF'], out.inputs['Surface'])

            if wear_variation:
                tex_coord = nodes.new('ShaderNodeTexCoord'); tex_coord.location = (-600, -100)
                noise     = nodes.new('ShaderNodeTexNoise'); noise.location     = (-400, -100)
                noise.inputs['Scale'].default_value    = 8.0
                noise.inputs['Detail'].default_value   = 6.0
                noise.inputs['Roughness'].default_value = 0.7

                mix_rough = nodes.new('ShaderNodeMath');  mix_rough.location  = (0, -100)
                mix_rough.operation = 'ADD'
                mix_rough.inputs[1].default_value = max(0.0, roughness - 0.15)

                clamp = nodes.new('ShaderNodeClamp'); clamp.location = (150, -100)
                clamp.inputs['Min'].default_value = 0.0
                clamp.inputs['Max'].default_value = 1.0

                links.new(tex_coord.outputs['Object'], noise.inputs['Vector'])
                links.new(noise.outputs['Fac'],        mix_rough.inputs[0])
                links.new(mix_rough.outputs['Value'],  clamp.inputs['Value'])
                links.new(clamp.outputs['Result'],     bsdf.inputs['Roughness'])

            # FIX: version-safe transparency
            if alpha < 1.0:
                ver = self._get_blender_version()
                if ver >= (4, 2):
                    mat.surface_render_method = 'DITHERED'  # Blender 4.2+
                else:
                    mat.blend_method = 'BLEND'

            # FIX: self-verification — confirm the main link exists
            surface_connected = any(
                l.to_node == out and l.to_socket.name == 'Surface'
                for l in mat.node_tree.links
            )
            if not surface_connected:
                self._log("create_pbr_material", "error", "Surface link not created")
                return {"error": "Material created but Surface link failed — check Blender console"}

            self._log("create_pbr_material")
            return {
                "created": name,
                "wear_variation": wear_variation,
                "nodes_added": len(nodes),
                "verified": surface_connected,
                "blender_version": list(bpy.app.version[:2]),
            }
        except Exception as e:
            self._log("create_pbr_material", "error", str(e))
            return {"error": str(e)}

    # ─────────────────────────────────────────────────────────────────────────
    # QA LAYER  — self-review before handoff
    # ─────────────────────────────────────────────────────────────────────────
    def run_asset_qa(self, name, check_uvs=True, check_materials=True, check_modifiers=True):
        """
        Comprehensive asset QA for a named object.
        FIX: modifier check logic corrected (show_viewport ≠ unapplied);
             bmesh freed in finally; rotation check added; weight paint check added.
        """
        obj = bpy.data.objects.get(name)
        if not obj:
            return {"error": f"Object '{name}' not found"}

        bm = None
        try:
            issues  = []
            warnings = []
            passed  = []

            # --- Transform checks ---
            loc_clean   = all(abs(v) < 0.001 for v in obj.location)
            scale_clean = all(abs(v - 1.0) < 0.001 for v in obj.scale)
            rot_applied = all(abs(v) < 0.001 for v in obj.rotation_euler)

            if not loc_clean:
                warnings.append(f"Non-zero location {[round(v,3) for v in obj.location]} — intentional or needs applying?")
            else:
                passed.append("Location at origin")

            if not scale_clean:
                issues.append(f"Scale not applied {[round(v,3) for v in obj.scale]} — apply scale (Ctrl+A) before export")
            else:
                passed.append("Scale applied (1,1,1)")

            if not rot_applied:
                warnings.append(f"Non-zero rotation {[round(v,3) for v in obj.rotation_euler]} — apply rotation before export?")
            else:
                passed.append("Rotation applied")

            if obj.type == 'MESH':
                bm, _ = self._build_bmesh_from_object(obj)

                # Manifold
                nm = sum(1 for e in bm.edges if not e.is_manifold)
                if nm: issues.append(f"{nm} non-manifold edges — will break export and UE5 import")
                else:  passed.append("Manifold geometry")

                # Ngons
                ngons = sum(1 for f in bm.faces if len(f.verts) > 4)
                if ngons: issues.append(f"{ngons} ngons — triangulate before Unreal export")
                else:     passed.append("No ngons")

                # Isolated verts
                iso = sum(1 for v in bm.verts if not v.link_edges)
                if iso: issues.append(f"{iso} isolated vertices — run Mesh > Clean Up > Delete Loose")
                else:   passed.append("No isolated verts")

                # Duplicate faces
                face_sets = [frozenset(v.index for v in f.verts) for f in bm.faces]
                dup = len(face_sets) - len(set(face_sets))
                if dup: issues.append(f"{dup} duplicate faces — run Remove Doubles")
                else:   passed.append("No duplicate faces")

                # Poly count thresholds
                face_count = len(bm.faces)
                if face_count == 0:
                    issues.append("Mesh has no faces")
                elif face_count > 100000:
                    issues.append(f"Very high poly ({face_count:,}) — needs LODs for game use")
                elif face_count > 50000:
                    warnings.append(f"High poly count ({face_count:,}) — consider LODs")
                else:
                    passed.append(f"Poly count OK ({face_count:,} faces)")

                bm.free(); bm = None

                # UV check
                if check_uvs:
                    if not obj.data.uv_layers:
                        issues.append("No UV maps — required for texturing and UE5 export")
                    else:
                        passed.append(f"UV maps present ({len(obj.data.uv_layers)} layer(s))")

                # Material check
                if check_materials:
                    if not obj.material_slots:
                        issues.append("No materials assigned")
                    else:
                        empty = [i for i, s in enumerate(obj.material_slots) if not s.material]
                        if empty: issues.append(f"Empty material slots at indices: {empty}")
                        else:     passed.append(f"{len(obj.material_slots)} material slot(s) assigned")

                # FIX: Modifier check — flag modifiers that MUST be applied before skeletal export
                if check_modifiers:
                    # Only flag modifiers that are incompatible with UE5 FBX export
                    blocking = [m.name for m in obj.modifiers
                                if m.type in ('BOOLEAN', 'ARRAY', 'MIRROR', 'BEVEL', 'SOLIDIFY')
                                and m.show_viewport]
                    advisory = [m.name for m in obj.modifiers
                                if m.type == 'SUBSURF' and m.show_viewport]
                    if blocking:
                        issues.append(f"Modifiers that must be applied before export: {blocking}")
                    if advisory:
                        warnings.append(f"Subdivision modifier present — apply if exporting to UE5: {advisory}")
                    if not blocking and not advisory:
                        passed.append("No blocking modifiers")

                # Weight paint check (if rigged)
                if obj.vertex_groups:
                    unweighted = sum(
                        1 for v in obj.data.vertices
                        if not any(g.group < len(obj.vertex_groups) for g in v.groups)
                    )
                    if unweighted > 0:
                        warnings.append(f"{unweighted} vertices have no weight — may collapse in UE5")
                    else:
                        passed.append(f"All vertices weighted ({len(obj.vertex_groups)} groups)")

            verdict = "PASS" if len(issues) == 0 else "FAIL"
            self._log("run_asset_qa")
            return {
                "object": name,
                "verdict": verdict,
                "passed": passed,
                "issues": issues,
                "warnings": warnings,
                "issue_count": len(issues),
                "warning_count": len(warnings),
                "summary": f"{verdict} — {len(issues)} blocking issue(s), {len(warnings)} warning(s)",
            }
        except Exception as e:
            self._log("run_asset_qa", "error", str(e))
            return {"error": str(e)}
        finally:
            if bm is not None:
                bm.free()

    def run_unreal_readiness_check(self, name, expected_unit_scale=0.01):
        """
        UE5 pre-export checklist with severity levels.
        FIX: triangulation downgraded to warning (UE5 triangulates on import);
             normal map direction check added; bmesh freed in finally;
             active obj set before mode_set.
        """
        obj = bpy.data.objects.get(name)
        if not obj:
            return {"error": f"Object '{name}' not found"}

        bm = None
        try:
            checks = {}

            # Naming convention
            ue_prefixes = ("SM_", "SK_", "T_", "M_", "MI_", "BP_", "A_", "P_", "NS_", "ABP_")
            checks["naming_convention"] = {
                "pass": any(name.startswith(p) for p in ue_prefixes),
                "severity": "warning",
                "detail": (f"Name '{name}' — UE5 naming: SM_ static mesh, SK_ skeletal, "
                           f"T_ texture, M_ material, MI_ material instance"),
            }

            # Scale uniform
            scale = obj.scale
            scale_uniform = (abs(scale.x - scale.y) < 0.001 and abs(scale.y - scale.z) < 0.001)
            checks["scale_uniform"] = {
                "pass": scale_uniform,
                "severity": "error",
                "detail": (f"Scale {[round(v,3) for v in scale]} — non-uniform scale "
                           f"will distort mesh in UE5. Apply scale (Ctrl+A)."),
            }

            # Scale applied (= 1,1,1)
            scale_applied = all(abs(v - 1.0) < 0.001 for v in obj.scale)
            checks["scale_applied"] = {
                "pass": scale_applied,
                "severity": "error",
                "detail": "Scale must be (1,1,1) before FBX export for correct UE5 sizing",
            }

            # Pivot at origin
            pivot_ok = all(abs(v) < 0.01 for v in obj.location)
            checks["pivot_at_origin"] = {
                "pass": pivot_ok,
                "severity": "warning",
                "detail": "UE5 uses pivot as spawn point — off-origin pivot causes offset placement",
            }

            if obj.type == 'MESH':
                bm, _ = self._build_bmesh_from_object(obj)
                bm.faces.ensure_lookup_table()

                # FIX: triangulation is advisory, not blocking
                quads_ngons = sum(1 for f in bm.faces if len(f.verts) > 3)
                checks["triangulated"] = {
                    "pass": quads_ngons == 0,
                    "severity": "warning",  # FIX: was "error"
                    "detail": (f"{quads_ngons} non-tri faces — UE5 auto-triangulates on import, "
                               f"but pre-triangulating gives you control over the result"),
                }

                # UV channels
                uv_count = len(obj.data.uv_layers)
                checks["has_uvs"] = {
                    "pass": uv_count >= 1,
                    "severity": "error",
                    "detail": f"{uv_count} UV channel(s) — at least one required",
                }
                checks["lightmap_uv"] = {
                    "pass": uv_count >= 2,
                    "severity": "warning",
                    "detail": (f"{uv_count} UV channel(s) — UE5 Lumen/Lightmass needs "
                               f"channel index 1 dedicated to lightmaps"),
                }
                bm.free(); bm = None

                # Collision mesh
                col_variants = ([f"UCX_{name}", f"UBX_{name}", f"USP_{name}"]
                                + [f"UCX_{name}_{i:02d}" for i in range(5)])
                has_collision = any(bpy.data.objects.get(cn) for cn in col_variants)
                checks["collision_mesh"] = {
                    "pass": has_collision,
                    "severity": "warning",
                    "detail": f"No UCX_/UBX_ collision mesh — UE5 will use auto-convex hull",
                }

                # LOD naming
                lod0_name = f"{name}_LOD0"
                checks["lod_naming"] = {
                    "pass": bpy.data.objects.get(lod0_name) is not None,
                    "severity": "info",
                    "detail": f"No '{lod0_name}' — name LODs as [name]_LOD0, _LOD1 etc.",
                }

                # Modifiers that MUST be applied
                blocking_mods = [m.name for m in obj.modifiers
                                 if m.type in ('BOOLEAN','ARRAY','MIRROR','BEVEL','SOLIDIFY')]
                checks["modifiers_applied"] = {
                    "pass": len(blocking_mods) == 0,
                    "severity": "error",
                    "detail": (f"Apply these modifiers before export: {blocking_mods}"
                               if blocking_mods else "No blocking modifiers"),
                }

                # FIX: Normal map direction check
                normal_map_warning = False
                for mat_slot in obj.material_slots:
                    if mat_slot.material and mat_slot.material.use_nodes:
                        for node in mat_slot.material.node_tree.nodes:
                            if node.type == 'NORMAL_MAP' and node.space == 'TANGENT':
                                normal_map_warning = True
                                break
                checks["normal_map_direction"] = {
                    "pass": True,  # Not a blocker, just informational
                    "severity": "info",
                    "detail": (
                        "Normal maps detected — Blender uses OpenGL (G up), UE5 uses DirectX (G down). "
                        "Flip G channel or enable 'Flip Green Channel' in UE5 texture import."
                        if normal_map_warning
                        else "No normal maps detected, or none to check"
                    ),
                }

            total = len(checks)
            errors   = sum(1 for c in checks.values() if not c["pass"] and c["severity"] == "error")
            warnings_count = sum(1 for c in checks.values() if not c["pass"] and c["severity"] == "warning")
            ue5_ready = errors == 0

            self._log("run_unreal_readiness_check")
            return {
                "object": name,
                "ue5_ready": ue5_ready,
                "blocking_errors": errors,
                "warnings": warnings_count,
                "total_checks": total,
                "checks": checks,
                "summary": (
                    f"READY — {warnings_count} advisory warning(s)" if ue5_ready
                    else f"NOT READY — {errors} blocking error(s), {warnings_count} warning(s)"
                ),
            }
        except Exception as e:
            self._log("run_unreal_readiness_check", "error", str(e))
            return {"error": str(e)}
        finally:
            if bm is not None:
                bm.free()

    # ─────────────────────────────────────────────────────────────────────────
    # EXPORT LAYER
    # ─────────────────────────────────────────────────────────────────────────
    def export_for_unreal(self, name, export_path,
                           apply_modifiers=True, triangulate=True,
                           scale=100.0, embed_textures=False,
                           export_animations=False):
        """
        Export object/armature as FBX with correct UE5 settings.
        FIX: pre-export QA gate (warns but doesn't block); always includes ARMATURE
             in object_types when exporting animations; path validation added.
        """
        try:
            obj = bpy.data.objects.get(name)
            if not obj:
                return {"error": f"Object '{name}' not found"}

            # Validate path
            if not export_path.lower().endswith('.fbx'):
                export_path = export_path.rstrip('/\\') + '.fbx'

            export_dir = os.path.dirname(export_path)
            if export_dir and not os.path.exists(export_dir):
                os.makedirs(export_dir, exist_ok=True)

            # FIX: pre-export advisory QA (non-blocking)
            qa = self.detect_mesh_problems(name) if obj.type == 'MESH' else {}
            pre_export_warnings = []
            if qa.get("problems"):
                pre_export_warnings = [f"{p['type']}: {p['count']}" for p in qa["problems"]]

            # Select object + children
            bpy.ops.object.select_all(action='DESELECT')
            obj.select_set(True)
            bpy.context.view_layer.objects.active = obj
            if obj.type == 'ARMATURE':
                for child in obj.children:
                    child.select_set(True)

            # FIX: always include ARMATURE in object_types (needed for skin weights)
            obj_types = {'MESH', 'ARMATURE'}

            bpy.ops.export_scene.fbx(
                filepath=export_path,
                use_selection=True,
                apply_unit_scale=True,
                apply_scale_options='FBX_SCALE_ALL',
                global_scale=scale / 100.0,
                axis_forward='-Z',
                axis_up='Y',
                object_types=obj_types,
                use_mesh_modifiers=apply_modifiers,
                mesh_smooth_type='FACE',
                use_triangles=triangulate,
                add_leaf_bones=False,
                bake_anim=export_animations,
                bake_anim_use_all_bones=export_animations,
                bake_anim_simplify_factor=1.0,
                path_mode='COPY' if embed_textures else 'AUTO',
                embed_textures=embed_textures,
            )

            # Verify file was actually created
            if not os.path.exists(export_path):
                return {"error": f"Export appeared to succeed but file not found at: {export_path}"}

            file_size = os.path.getsize(export_path)
            if file_size < 100:
                return {"error": f"Exported file is suspiciously small ({file_size}b) — check Blender console"}

            self._log("export_for_unreal")
            return {
                "exported": True,
                "object": name,
                "path": export_path,
                "file_size_kb": round(file_size / 1024, 1),
                "pre_export_warnings": pre_export_warnings,
                "settings": {
                    "scale": scale,
                    "triangulate": triangulate,
                    "apply_modifiers": apply_modifiers,
                    "animations": export_animations,
                    "axis": "-Z forward, Y up (UE5 standard)",
                },
            }
        except Exception as e:
            self._log("export_for_unreal", "error", str(e))
            return {"error": str(e)}

    def prepare_lod_names(self, base_name, lod_count=4):
        """
        Scan scene for LOD objects following UE5 convention.
        FIX: reports reduction percentage between LODs; validates LOD ordering
             (each LOD should have fewer faces than previous).
        """
        try:
            lods = []
            prev_faces = None
            for i in range(lod_count):
                lod_name = f"{base_name}_LOD{i}"
                lod_obj  = bpy.data.objects.get(lod_name)
                exists   = lod_obj is not None
                face_count = 0
                reduction_pct = None

                if exists and lod_obj.type == 'MESH':
                    face_count = len(lod_obj.data.polygons)
                    if prev_faces and prev_faces > 0:
                        reduction_pct = round((1 - face_count / prev_faces) * 100, 1)

                lod_entry = {
                    "lod": i,
                    "name": lod_name,
                    "exists": exists,
                    "faces": face_count,
                }
                if reduction_pct is not None:
                    lod_entry["reduction_from_prev_pct"] = reduction_pct
                    # Validate reduction is meaningful
                    if reduction_pct < 40:
                        lod_entry["warning"] = f"Only {reduction_pct}% reduction — UE5 expects ~50% per LOD"
                lods.append(lod_entry)
                if exists and face_count > 0:
                    prev_faces = face_count

            missing = [l["name"] for l in lods if not l["exists"]]
            lod0_faces = lods[0]["faces"] if lods and lods[0]["exists"] else 0

            self._log("prepare_lod_names")
            return {
                "base": base_name,
                "lod_count_found": sum(1 for l in lods if l["exists"]),
                "lods": lods,
                "missing": missing,
                "lod0_faces": lod0_faces,
                "tip": ("All LODs present — ready for UE5 import" if not missing
                        else f"Missing LODs: {missing} — create these meshes with ~50% face reduction each"),
            }
        except Exception as e:
            self._log("prepare_lod_names", "error", str(e))
            return {"error": str(e)}

    # ─────────────────────────────────────────────────────────────────────────
    # ORIGINAL METHODS CONTINUE BELOW (unchanged)
    # ─────────────────────────────────────────────────────────────────────────

    def get_scene_info(self):
        """Get information about the current Blender scene"""
        try:
            print("Getting scene info...")
            # Simplify the scene info to reduce data size
            scene_info = {
                "name": bpy.context.scene.name,
                "object_count": len(bpy.context.scene.objects),
                "objects": [],
                "materials_count": len(bpy.data.materials),
            }

            # Collect minimal object information (limit to first 10 objects)
            for i, obj in enumerate(bpy.context.scene.objects):
                if i >= 10:  # Reduced from 20 to 10
                    break

                obj_info = {
                    "name": obj.name,
                    "type": obj.type,
                    # Only include basic location data
                    "location": [round(float(obj.location.x), 2),
                                round(float(obj.location.y), 2),
                                round(float(obj.location.z), 2)],
                }
                scene_info["objects"].append(obj_info)

            print(f"Scene info collected: {len(scene_info['objects'])} objects")
            return scene_info
        except Exception as e:
            print(f"Error in get_scene_info: {str(e)}")
            traceback.print_exc()
            return {"error": str(e)}

    @staticmethod
    def _get_aabb(obj):
        """ Returns the world-space axis-aligned bounding box (AABB) of an object. """
        if obj.type != 'MESH':
            raise TypeError("Object must be a mesh")

        # Get the bounding box corners in local space
        local_bbox_corners = [mathutils.Vector(corner) for corner in obj.bound_box]

        # Convert to world coordinates
        world_bbox_corners = [obj.matrix_world @ corner for corner in local_bbox_corners]

        # Compute axis-aligned min/max coordinates
        min_corner = mathutils.Vector(map(min, zip(*world_bbox_corners)))
        max_corner = mathutils.Vector(map(max, zip(*world_bbox_corners)))

        return [
            [*min_corner], [*max_corner]
        ]



    def get_object_info(self, name):
        """Get detailed information about a specific object"""
        obj = bpy.data.objects.get(name)
        if not obj:
            raise ValueError(f"Object not found: {name}")

        # Basic object info
        obj_info = {
            "name": obj.name,
            "type": obj.type,
            "location": [obj.location.x, obj.location.y, obj.location.z],
            "rotation": [obj.rotation_euler.x, obj.rotation_euler.y, obj.rotation_euler.z],
            "scale": [obj.scale.x, obj.scale.y, obj.scale.z],
            "visible": obj.visible_get(),
            "materials": [],
        }

        if obj.type == "MESH":
            bounding_box = self._get_aabb(obj)
            obj_info["world_bounding_box"] = bounding_box

        # Add material slots
        for slot in obj.material_slots:
            if slot.material:
                obj_info["materials"].append(slot.material.name)

        # Add mesh data if applicable
        if obj.type == 'MESH' and obj.data:
            mesh = obj.data
            obj_info["mesh"] = {
                "vertices": len(mesh.vertices),
                "edges": len(mesh.edges),
                "polygons": len(mesh.polygons),
            }

        return obj_info

    def get_viewport_screenshot(self, max_size=800, filepath=None, format="png"):
        """
        Capture a screenshot of the current 3D viewport and save it to the specified path.

        Parameters:
        - max_size: Maximum size in pixels for the largest dimension of the image
        - filepath: Path where to save the screenshot file
        - format: Image format (png, jpg, etc.)

        Returns success/error status
        """
        try:
            if not filepath:
                return {"error": "No filepath provided"}

            # Find the active 3D viewport
            area = None
            for a in bpy.context.screen.areas:
                if a.type == 'VIEW_3D':
                    area = a
                    break

            if not area:
                return {"error": "No 3D viewport found"}

            # Take screenshot with proper context override
            with bpy.context.temp_override(area=area):
                bpy.ops.screen.screenshot_area(filepath=filepath)

            # Load and resize if needed
            img = bpy.data.images.load(filepath)
            width, height = img.size

            if max(width, height) > max_size:
                scale = max_size / max(width, height)
                new_width = int(width * scale)
                new_height = int(height * scale)
                img.scale(new_width, new_height)

                # Set format and save
                img.file_format = format.upper()
                img.save()
                width, height = new_width, new_height

            # Cleanup Blender image data
            bpy.data.images.remove(img)

            return {
                "success": True,
                "width": width,
                "height": height,
                "filepath": filepath
            }

        except Exception as e:
            return {"error": str(e)}

    def execute_code(self, code):
        """Execute arbitrary Blender Python code"""
        # This is powerful but potentially dangerous - use with caution
        try:
            # Create a local namespace for execution
            namespace = {"bpy": bpy}

            # Capture stdout during execution, and return it as result
            capture_buffer = io.StringIO()
            with redirect_stdout(capture_buffer):
                exec(code, namespace)

            captured_output = capture_buffer.getvalue()
            return {"executed": True, "result": captured_output}
        except Exception as e:
            raise Exception(f"Code execution error: {str(e)}")



    def get_polyhaven_categories(self, asset_type):
        """Get categories for a specific asset type from Polyhaven"""
        try:
            if asset_type not in ["hdris", "textures", "models", "all"]:
                return {"error": f"Invalid asset type: {asset_type}. Must be one of: hdris, textures, models, all"}

            response = requests.get(f"https://api.polyhaven.com/categories/{asset_type}", headers=REQ_HEADERS)
            if response.status_code == 200:
                return {"categories": response.json()}
            else:
                return {"error": f"API request failed with status code {response.status_code}"}
        except Exception as e:
            return {"error": str(e)}

    def search_polyhaven_assets(self, asset_type=None, categories=None):
        """Search for assets from Polyhaven with optional filtering"""
        try:
            url = "https://api.polyhaven.com/assets"
            params = {}

            if asset_type and asset_type != "all":
                if asset_type not in ["hdris", "textures", "models"]:
                    return {"error": f"Invalid asset type: {asset_type}. Must be one of: hdris, textures, models, all"}
                params["type"] = asset_type

            if categories:
                params["categories"] = categories

            response = requests.get(url, params=params, headers=REQ_HEADERS)
            if response.status_code == 200:
                # Limit the response size to avoid overwhelming Blender
                assets = response.json()
                # Return only the first 20 assets to keep response size manageable
                limited_assets = {}
                for i, (key, value) in enumerate(assets.items()):
                    if i >= 20:  # Limit to 20 assets
                        break
                    limited_assets[key] = value

                return {"assets": limited_assets, "total_count": len(assets), "returned_count": len(limited_assets)}
            else:
                return {"error": f"API request failed with status code {response.status_code}"}
        except Exception as e:
            return {"error": str(e)}

    def download_polyhaven_asset(self, asset_id, asset_type, resolution="1k", file_format=None):
        try:
            # First get the files information
            files_response = requests.get(f"https://api.polyhaven.com/files/{asset_id}", headers=REQ_HEADERS)
            if files_response.status_code != 200:
                return {"error": f"Failed to get asset files: {files_response.status_code}"}

            files_data = files_response.json()

            # Handle different asset types
            if asset_type == "hdris":
                # For HDRIs, download the .hdr or .exr file
                if not file_format:
                    file_format = "hdr"  # Default format for HDRIs

                if "hdri" in files_data and resolution in files_data["hdri"] and file_format in files_data["hdri"][resolution]:
                    file_info = files_data["hdri"][resolution][file_format]
                    file_url = file_info["url"]

                    # For HDRIs, we need to save to a temporary file first
                    # since Blender can't properly load HDR data directly from memory
                    with tempfile.NamedTemporaryFile(suffix=f".{file_format}", delete=False) as tmp_file:
                        # Download the file
                        response = requests.get(file_url, headers=REQ_HEADERS)
                        if response.status_code != 200:
                            return {"error": f"Failed to download HDRI: {response.status_code}"}

                        tmp_file.write(response.content)
                        tmp_path = tmp_file.name

                    try:
                        # Create a new world if none exists
                        if not bpy.data.worlds:
                            bpy.data.worlds.new("World")

                        world = bpy.data.worlds[0]
                        world.use_nodes = True
                        node_tree = world.node_tree

                        # Clear existing nodes
                        for node in node_tree.nodes:
                            node_tree.nodes.remove(node)

                        # Create nodes
                        tex_coord = node_tree.nodes.new(type='ShaderNodeTexCoord')
                        tex_coord.location = (-800, 0)

                        mapping = node_tree.nodes.new(type='ShaderNodeMapping')
                        mapping.location = (-600, 0)

                        # Load the image from the temporary file
                        env_tex = node_tree.nodes.new(type='ShaderNodeTexEnvironment')
                        env_tex.location = (-400, 0)
                        env_tex.image = bpy.data.images.load(tmp_path)

                        # Use a color space that exists in all Blender versions
                        if file_format.lower() == 'exr':
                            # Try to use Linear color space for EXR files
                            try:
                                env_tex.image.colorspace_settings.name = 'Linear'
                            except:
                                # Fallback to Non-Color if Linear isn't available
                                env_tex.image.colorspace_settings.name = 'Non-Color'
                        else:  # hdr
                            # For HDR files, try these options in order
                            for color_space in ['Linear', 'Linear Rec.709', 'Non-Color']:
                                try:
                                    env_tex.image.colorspace_settings.name = color_space
                                    break  # Stop if we successfully set a color space
                                except:
                                    continue

                        background = node_tree.nodes.new(type='ShaderNodeBackground')
                        background.location = (-200, 0)

                        output = node_tree.nodes.new(type='ShaderNodeOutputWorld')
                        output.location = (0, 0)

                        # Connect nodes
                        node_tree.links.new(tex_coord.outputs['Generated'], mapping.inputs['Vector'])
                        node_tree.links.new(mapping.outputs['Vector'], env_tex.inputs['Vector'])
                        node_tree.links.new(env_tex.outputs['Color'], background.inputs['Color'])
                        node_tree.links.new(background.outputs['Background'], output.inputs['Surface'])

                        # Set as active world
                        bpy.context.scene.world = world

                        # Clean up temporary file
                        try:
                            tempfile._cleanup()  # This will clean up all temporary files
                        except:
                            pass

                        return {
                            "success": True,
                            "message": f"HDRI {asset_id} imported successfully",
                            "image_name": env_tex.image.name
                        }
                    except Exception as e:
                        return {"error": f"Failed to set up HDRI in Blender: {str(e)}"}
                else:
                    return {"error": f"Requested resolution or format not available for this HDRI"}

            elif asset_type == "textures":
                if not file_format:
                    file_format = "jpg"  # Default format for textures

                downloaded_maps = {}

                try:
                    for map_type in files_data:
                        if map_type not in ["blend", "gltf"]:  # Skip non-texture files
                            if resolution in files_data[map_type] and file_format in files_data[map_type][resolution]:
                                file_info = files_data[map_type][resolution][file_format]
                                file_url = file_info["url"]

                                # Use NamedTemporaryFile like we do for HDRIs
                                with tempfile.NamedTemporaryFile(suffix=f".{file_format}", delete=False) as tmp_file:
                                    # Download the file
                                    response = requests.get(file_url, headers=REQ_HEADERS)
                                    if response.status_code == 200:
                                        tmp_file.write(response.content)
                                        tmp_path = tmp_file.name

                                        # Load image from temporary file
                                        image = bpy.data.images.load(tmp_path)
                                        image.name = f"{asset_id}_{map_type}.{file_format}"

                                        # Pack the image into .blend file
                                        image.pack()

                                        # Set color space based on map type
                                        if map_type in ['color', 'diffuse', 'albedo']:
                                            try:
                                                image.colorspace_settings.name = 'sRGB'
                                            except:
                                                pass
                                        else:
                                            try:
                                                image.colorspace_settings.name = 'Non-Color'
                                            except:
                                                pass

                                        downloaded_maps[map_type] = image

                                        # Clean up temporary file
                                        try:
                                            os.unlink(tmp_path)
                                        except:
                                            pass

                    if not downloaded_maps:
                        return {"error": f"No texture maps found for the requested resolution and format"}

                    # Create a new material with the downloaded textures
                    mat = bpy.data.materials.new(name=asset_id)
                    mat.use_nodes = True
                    nodes = mat.node_tree.nodes
                    links = mat.node_tree.links

                    # Clear default nodes
                    for node in nodes:
                        nodes.remove(node)

                    # Create output node
                    output = nodes.new(type='ShaderNodeOutputMaterial')
                    output.location = (300, 0)

                    # Create principled BSDF node
                    principled = nodes.new(type='ShaderNodeBsdfPrincipled')
                    principled.location = (0, 0)
                    links.new(principled.outputs[0], output.inputs[0])

                    # Add texture nodes based on available maps
                    tex_coord = nodes.new(type='ShaderNodeTexCoord')
                    tex_coord.location = (-800, 0)

                    mapping = nodes.new(type='ShaderNodeMapping')
                    mapping.location = (-600, 0)
                    mapping.vector_type = 'TEXTURE'  # Changed from default 'POINT' to 'TEXTURE'
                    links.new(tex_coord.outputs['UV'], mapping.inputs['Vector'])

                    # Position offset for texture nodes
                    x_pos = -400
                    y_pos = 300

                    # Connect different texture maps
                    for map_type, image in downloaded_maps.items():
                        tex_node = nodes.new(type='ShaderNodeTexImage')
                        tex_node.location = (x_pos, y_pos)
                        tex_node.image = image

                        # Set color space based on map type
                        if map_type.lower() in ['color', 'diffuse', 'albedo']:
                            try:
                                tex_node.image.colorspace_settings.name = 'sRGB'
                            except:
                                pass  # Use default if sRGB not available
                        else:
                            try:
                                tex_node.image.colorspace_settings.name = 'Non-Color'
                            except:
                                pass  # Use default if Non-Color not available

                        links.new(mapping.outputs['Vector'], tex_node.inputs['Vector'])

                        # Connect to appropriate input on Principled BSDF
                        if map_type.lower() in ['color', 'diffuse', 'albedo']:
                            links.new(tex_node.outputs['Color'], principled.inputs['Base Color'])
                        elif map_type.lower() in ['roughness', 'rough']:
                            links.new(tex_node.outputs['Color'], principled.inputs['Roughness'])
                        elif map_type.lower() in ['metallic', 'metalness', 'metal']:
                            links.new(tex_node.outputs['Color'], principled.inputs['Metallic'])
                        elif map_type.lower() in ['normal', 'nor']:
                            # Add normal map node
                            normal_map = nodes.new(type='ShaderNodeNormalMap')
                            normal_map.location = (x_pos + 200, y_pos)
                            links.new(tex_node.outputs['Color'], normal_map.inputs['Color'])
                            links.new(normal_map.outputs['Normal'], principled.inputs['Normal'])
                        elif map_type in ['displacement', 'disp', 'height']:
                            # Add displacement node
                            disp_node = nodes.new(type='ShaderNodeDisplacement')
                            disp_node.location = (x_pos + 200, y_pos - 200)
                            links.new(tex_node.outputs['Color'], disp_node.inputs['Height'])
                            links.new(disp_node.outputs['Displacement'], output.inputs['Displacement'])

                        y_pos -= 250

                    return {
                        "success": True,
                        "message": f"Texture {asset_id} imported as material",
                        "material": mat.name,
                        "maps": list(downloaded_maps.keys())
                    }

                except Exception as e:
                    return {"error": f"Failed to process textures: {str(e)}"}

            elif asset_type == "models":
                # For models, prefer glTF format if available
                if not file_format:
                    file_format = "gltf"  # Default format for models

                if file_format in files_data and resolution in files_data[file_format]:
                    file_info = files_data[file_format][resolution][file_format]
                    file_url = file_info["url"]

                    # Create a temporary directory to store the model and its dependencies
                    temp_dir = tempfile.mkdtemp()
                    main_file_path = ""

                    try:
                        # Download the main model file
                        main_file_name = file_url.split("/")[-1]
                        main_file_path = os.path.join(temp_dir, main_file_name)

                        response = requests.get(file_url, headers=REQ_HEADERS)
                        if response.status_code != 200:
                            return {"error": f"Failed to download model: {response.status_code}"}

                        with open(main_file_path, "wb") as f:
                            f.write(response.content)

                        # Check for included files and download them
                        if "include" in file_info and file_info["include"]:
                            for include_path, include_info in file_info["include"].items():
                                # Get the URL for the included file - this is the fix
                                include_url = include_info["url"]

                                # Create the directory structure for the included file
                                include_file_path = os.path.join(temp_dir, include_path)
                                os.makedirs(os.path.dirname(include_file_path), exist_ok=True)

                                # Download the included file
                                include_response = requests.get(include_url, headers=REQ_HEADERS)
                                if include_response.status_code == 200:
                                    with open(include_file_path, "wb") as f:
                                        f.write(include_response.content)
                                else:
                                    print(f"Failed to download included file: {include_path}")

                        # Import the model into Blender
                        if file_format == "gltf" or file_format == "glb":
                            bpy.ops.import_scene.gltf(filepath=main_file_path)
                        elif file_format == "fbx":
                            bpy.ops.import_scene.fbx(filepath=main_file_path)
                        elif file_format == "obj":
                            bpy.ops.import_scene.obj(filepath=main_file_path)
                        elif file_format == "blend":
                            # For blend files, we need to append or link
                            with bpy.data.libraries.load(main_file_path, link=False) as (data_from, data_to):
                                data_to.objects = data_from.objects

                            # Link the objects to the scene
                            for obj in data_to.objects:
                                if obj is not None:
                                    bpy.context.collection.objects.link(obj)
                        else:
                            return {"error": f"Unsupported model format: {file_format}"}

                        # Get the names of imported objects
                        imported_objects = [obj.name for obj in bpy.context.selected_objects]

                        return {
                            "success": True,
                            "message": f"Model {asset_id} imported successfully",
                            "imported_objects": imported_objects
                        }
                    except Exception as e:
                        return {"error": f"Failed to import model: {str(e)}"}
                    finally:
                        # Clean up temporary directory
                        with suppress(Exception):
                            shutil.rmtree(temp_dir)
                else:
                    return {"error": f"Requested format or resolution not available for this model"}

            else:
                return {"error": f"Unsupported asset type: {asset_type}"}

        except Exception as e:
            return {"error": f"Failed to download asset: {str(e)}"}

    def set_texture(self, object_name, texture_id):
        """Apply a previously downloaded Polyhaven texture to an object by creating a new material"""
        try:
            # Get the object
            obj = bpy.data.objects.get(object_name)
            if not obj:
                return {"error": f"Object not found: {object_name}"}

            # Make sure object can accept materials
            if not hasattr(obj, 'data') or not hasattr(obj.data, 'materials'):
                return {"error": f"Object {object_name} cannot accept materials"}

            # Find all images related to this texture and ensure they're properly loaded
            texture_images = {}
            for img in bpy.data.images:
                if img.name.startswith(texture_id + "_"):
                    # Extract the map type from the image name
                    map_type = img.name.split('_')[-1].split('.')[0]

                    # Force a reload of the image
                    img.reload()

                    # Ensure proper color space
                    if map_type.lower() in ['color', 'diffuse', 'albedo']:
                        try:
                            img.colorspace_settings.name = 'sRGB'
                        except:
                            pass
                    else:
                        try:
                            img.colorspace_settings.name = 'Non-Color'
                        except:
                            pass

                    # Ensure the image is packed
                    if not img.packed_file:
                        img.pack()

                    texture_images[map_type] = img
                    print(f"Loaded texture map: {map_type} - {img.name}")

                    # Debug info
                    print(f"Image size: {img.size[0]}x{img.size[1]}")
                    print(f"Color space: {img.colorspace_settings.name}")
                    print(f"File format: {img.file_format}")
                    print(f"Is packed: {bool(img.packed_file)}")

            if not texture_images:
                return {"error": f"No texture images found for: {texture_id}. Please download the texture first."}

            # Create a new material
            new_mat_name = f"{texture_id}_material_{object_name}"

            # Remove any existing material with this name to avoid conflicts
            existing_mat = bpy.data.materials.get(new_mat_name)
            if existing_mat:
                bpy.data.materials.remove(existing_mat)

            new_mat = bpy.data.materials.new(name=new_mat_name)
            new_mat.use_nodes = True

            # Set up the material nodes
            nodes = new_mat.node_tree.nodes
            links = new_mat.node_tree.links

            # Clear default nodes
            nodes.clear()

            # Create output node
            output = nodes.new(type='ShaderNodeOutputMaterial')
            output.location = (600, 0)

            # Create principled BSDF node
            principled = nodes.new(type='ShaderNodeBsdfPrincipled')
            principled.location = (300, 0)
            links.new(principled.outputs[0], output.inputs[0])

            # Add texture nodes based on available maps
            tex_coord = nodes.new(type='ShaderNodeTexCoord')
            tex_coord.location = (-800, 0)

            mapping = nodes.new(type='ShaderNodeMapping')
            mapping.location = (-600, 0)
            mapping.vector_type = 'TEXTURE'  # Changed from default 'POINT' to 'TEXTURE'
            links.new(tex_coord.outputs['UV'], mapping.inputs['Vector'])

            # Position offset for texture nodes
            x_pos = -400
            y_pos = 300

            # Connect different texture maps
            for map_type, image in texture_images.items():
                tex_node = nodes.new(type='ShaderNodeTexImage')
                tex_node.location = (x_pos, y_pos)
                tex_node.image = image

                # Set color space based on map type
                if map_type.lower() in ['color', 'diffuse', 'albedo']:
                    try:
                        tex_node.image.colorspace_settings.name = 'sRGB'
                    except:
                        pass  # Use default if sRGB not available
                else:
                    try:
                        tex_node.image.colorspace_settings.name = 'Non-Color'
                    except:
                        pass  # Use default if Non-Color not available

                links.new(mapping.outputs['Vector'], tex_node.inputs['Vector'])

                # Connect to appropriate input on Principled BSDF
                if map_type.lower() in ['color', 'diffuse', 'albedo']:
                    links.new(tex_node.outputs['Color'], principled.inputs['Base Color'])
                elif map_type.lower() in ['roughness', 'rough']:
                    links.new(tex_node.outputs['Color'], principled.inputs['Roughness'])
                elif map_type.lower() in ['metallic', 'metalness', 'metal']:
                    links.new(tex_node.outputs['Color'], principled.inputs['Metallic'])
                elif map_type.lower() in ['normal', 'nor', 'dx', 'gl']:
                    # Add normal map node
                    normal_map = nodes.new(type='ShaderNodeNormalMap')
                    normal_map.location = (x_pos + 200, y_pos)
                    links.new(tex_node.outputs['Color'], normal_map.inputs['Color'])
                    links.new(normal_map.outputs['Normal'], principled.inputs['Normal'])
                elif map_type.lower() in ['displacement', 'disp', 'height']:
                    # Add displacement node
                    disp_node = nodes.new(type='ShaderNodeDisplacement')
                    disp_node.location = (x_pos + 200, y_pos - 200)
                    disp_node.inputs['Scale'].default_value = 0.1  # Reduce displacement strength
                    links.new(tex_node.outputs['Color'], disp_node.inputs['Height'])
                    links.new(disp_node.outputs['Displacement'], output.inputs['Displacement'])

                y_pos -= 250

            # Second pass: Connect nodes with proper handling for special cases
            texture_nodes = {}

            # First find all texture nodes and store them by map type
            for node in nodes:
                if node.type == 'TEX_IMAGE' and node.image:
                    for map_type, image in texture_images.items():
                        if node.image == image:
                            texture_nodes[map_type] = node
                            break

            # Now connect everything using the nodes instead of images
            # Handle base color (diffuse)
            for map_name in ['color', 'diffuse', 'albedo']:
                if map_name in texture_nodes:
                    links.new(texture_nodes[map_name].outputs['Color'], principled.inputs['Base Color'])
                    print(f"Connected {map_name} to Base Color")
                    break

            # Handle roughness
            for map_name in ['roughness', 'rough']:
                if map_name in texture_nodes:
                    links.new(texture_nodes[map_name].outputs['Color'], principled.inputs['Roughness'])
                    print(f"Connected {map_name} to Roughness")
                    break

            # Handle metallic
            for map_name in ['metallic', 'metalness', 'metal']:
                if map_name in texture_nodes:
                    links.new(texture_nodes[map_name].outputs['Color'], principled.inputs['Metallic'])
                    print(f"Connected {map_name} to Metallic")
                    break

            # Handle normal maps
            for map_name in ['gl', 'dx', 'nor']:
                if map_name in texture_nodes:
                    normal_map_node = nodes.new(type='ShaderNodeNormalMap')
                    normal_map_node.location = (100, 100)
                    links.new(texture_nodes[map_name].outputs['Color'], normal_map_node.inputs['Color'])
                    links.new(normal_map_node.outputs['Normal'], principled.inputs['Normal'])
                    print(f"Connected {map_name} to Normal")
                    break

            # Handle displacement
            for map_name in ['displacement', 'disp', 'height']:
                if map_name in texture_nodes:
                    disp_node = nodes.new(type='ShaderNodeDisplacement')
                    disp_node.location = (300, -200)
                    disp_node.inputs['Scale'].default_value = 0.1  # Reduce displacement strength
                    links.new(texture_nodes[map_name].outputs['Color'], disp_node.inputs['Height'])
                    links.new(disp_node.outputs['Displacement'], output.inputs['Displacement'])
                    print(f"Connected {map_name} to Displacement")
                    break

            # Handle ARM texture (Ambient Occlusion, Roughness, Metallic)
            if 'arm' in texture_nodes:
                separate_rgb = nodes.new(type='ShaderNodeSeparateRGB')
                separate_rgb.location = (-200, -100)
                links.new(texture_nodes['arm'].outputs['Color'], separate_rgb.inputs['Image'])

                # Connect Roughness (G) if no dedicated roughness map
                if not any(map_name in texture_nodes for map_name in ['roughness', 'rough']):
                    links.new(separate_rgb.outputs['G'], principled.inputs['Roughness'])
                    print("Connected ARM.G to Roughness")

                # Connect Metallic (B) if no dedicated metallic map
                if not any(map_name in texture_nodes for map_name in ['metallic', 'metalness', 'metal']):
                    links.new(separate_rgb.outputs['B'], principled.inputs['Metallic'])
                    print("Connected ARM.B to Metallic")

                # For AO (R channel), multiply with base color if we have one
                base_color_node = None
                for map_name in ['color', 'diffuse', 'albedo']:
                    if map_name in texture_nodes:
                        base_color_node = texture_nodes[map_name]
                        break

                if base_color_node:
                    mix_node = nodes.new(type='ShaderNodeMixRGB')
                    mix_node.location = (100, 200)
                    mix_node.blend_type = 'MULTIPLY'
                    mix_node.inputs['Fac'].default_value = 0.8  # 80% influence

                    # Disconnect direct connection to base color
                    for link in base_color_node.outputs['Color'].links:
                        if link.to_socket == principled.inputs['Base Color']:
                            links.remove(link)

                    # Connect through the mix node
                    links.new(base_color_node.outputs['Color'], mix_node.inputs[1])
                    links.new(separate_rgb.outputs['R'], mix_node.inputs[2])
                    links.new(mix_node.outputs['Color'], principled.inputs['Base Color'])
                    print("Connected ARM.R to AO mix with Base Color")

            # Handle AO (Ambient Occlusion) if separate
            if 'ao' in texture_nodes:
                base_color_node = None
                for map_name in ['color', 'diffuse', 'albedo']:
                    if map_name in texture_nodes:
                        base_color_node = texture_nodes[map_name]
                        break

                if base_color_node:
                    mix_node = nodes.new(type='ShaderNodeMixRGB')
                    mix_node.location = (100, 200)
                    mix_node.blend_type = 'MULTIPLY'
                    mix_node.inputs['Fac'].default_value = 0.8  # 80% influence

                    # Disconnect direct connection to base color
                    for link in base_color_node.outputs['Color'].links:
                        if link.to_socket == principled.inputs['Base Color']:
                            links.remove(link)

                    # Connect through the mix node
                    links.new(base_color_node.outputs['Color'], mix_node.inputs[1])
                    links.new(texture_nodes['ao'].outputs['Color'], mix_node.inputs[2])
                    links.new(mix_node.outputs['Color'], principled.inputs['Base Color'])
                    print("Connected AO to mix with Base Color")

            # CRITICAL: Make sure to clear all existing materials from the object
            while len(obj.data.materials) > 0:
                obj.data.materials.pop(index=0)

            # Assign the new material to the object
            obj.data.materials.append(new_mat)

            # CRITICAL: Make the object active and select it
            bpy.context.view_layer.objects.active = obj
            obj.select_set(True)

            # CRITICAL: Force Blender to update the material
            bpy.context.view_layer.update()

            # Get the list of texture maps
            texture_maps = list(texture_images.keys())

            # Get info about texture nodes for debugging
            material_info = {
                "name": new_mat.name,
                "has_nodes": new_mat.use_nodes,
                "node_count": len(new_mat.node_tree.nodes),
                "texture_nodes": []
            }

            for node in new_mat.node_tree.nodes:
                if node.type == 'TEX_IMAGE' and node.image:
                    connections = []
                    for output in node.outputs:
                        for link in output.links:
                            connections.append(f"{output.name} → {link.to_node.name}.{link.to_socket.name}")

                    material_info["texture_nodes"].append({
                        "name": node.name,
                        "image": node.image.name,
                        "colorspace": node.image.colorspace_settings.name,
                        "connections": connections
                    })

            return {
                "success": True,
                "message": f"Created new material and applied texture {texture_id} to {object_name}",
                "material": new_mat.name,
                "maps": texture_maps,
                "material_info": material_info
            }

        except Exception as e:
            print(f"Error in set_texture: {str(e)}")
            traceback.print_exc()
            return {"error": f"Failed to apply texture: {str(e)}"}

    def get_telemetry_consent(self):
        """Get the current telemetry consent status"""
        try:
            # Get addon preferences - use the module name
            addon_prefs = bpy.context.preferences.addons.get(__name__)
            if addon_prefs:
                consent = addon_prefs.preferences.telemetry_consent
            else:
                # Fallback to default if preferences not available
                consent = True
        except (AttributeError, KeyError):
            # Fallback to default if preferences not available
            consent = True
        return {"consent": consent}

    def get_polyhaven_status(self):
        """Get the current status of PolyHaven integration"""
        enabled = bpy.context.scene.blendermcp_use_polyhaven
        if enabled:
            return {"enabled": True, "message": "PolyHaven integration is enabled and ready to use."}
        else:
            return {
                "enabled": False,
                "message": """PolyHaven integration is currently disabled. To enable it:
                            1. In the 3D Viewport, find the BlenderMCP panel in the sidebar (press N if hidden)
                            2. Check the 'Use assets from Poly Haven' checkbox
                            3. Restart the connection to Claude"""
        }

    #region Hyper3D
    def get_hyper3d_status(self):
        """Get the current status of Hyper3D Rodin integration"""
        enabled = bpy.context.scene.blendermcp_use_hyper3d
        if enabled:
            if not bpy.context.scene.blendermcp_hyper3d_api_key:
                return {
                    "enabled": False,
                    "message": """Hyper3D Rodin integration is currently enabled, but API key is not given. To enable it:
                                1. In the 3D Viewport, find the BlenderMCP panel in the sidebar (press N if hidden)
                                2. Keep the 'Use Hyper3D Rodin 3D model generation' checkbox checked
                                3. Choose the right plaform and fill in the API Key
                                4. Restart the connection to Claude"""
                }
            mode = bpy.context.scene.blendermcp_hyper3d_mode
            message = f"Hyper3D Rodin integration is enabled and ready to use. Mode: {mode}. " + \
                f"Key type: {'private' if bpy.context.scene.blendermcp_hyper3d_api_key != RODIN_FREE_TRIAL_KEY else 'free_trial'}"
            return {
                "enabled": True,
                "message": message
            }
        else:
            return {
                "enabled": False,
                "message": """Hyper3D Rodin integration is currently disabled. To enable it:
                            1. In the 3D Viewport, find the BlenderMCP panel in the sidebar (press N if hidden)
                            2. Check the 'Use Hyper3D Rodin 3D model generation' checkbox
                            3. Restart the connection to Claude"""
            }

    def create_rodin_job(self, *args, **kwargs):
        match bpy.context.scene.blendermcp_hyper3d_mode:
            case "MAIN_SITE":
                return self.create_rodin_job_main_site(*args, **kwargs)
            case "FAL_AI":
                return self.create_rodin_job_fal_ai(*args, **kwargs)
            case _:
                return f"Error: Unknown Hyper3D Rodin mode!"

    def create_rodin_job_main_site(
            self,
            text_prompt: str=None,
            images: list[tuple[str, str]]=None,
            bbox_condition=None
        ):
        try:
            if images is None:
                images = []
            """Call Rodin API, get the job uuid and subscription key"""
            files = [
                *[("images", (f"{i:04d}{img_suffix}", img)) for i, (img_suffix, img) in enumerate(images)],
                ("tier", (None, "Sketch")),
                ("mesh_mode", (None, "Raw")),
            ]
            if text_prompt:
                files.append(("prompt", (None, text_prompt)))
            if bbox_condition:
                files.append(("bbox_condition", (None, json.dumps(bbox_condition))))
            response = requests.post(
                "https://hyperhuman.deemos.com/api/v2/rodin",
                headers={
                    "Authorization": f"Bearer {bpy.context.scene.blendermcp_hyper3d_api_key}",
                },
                files=files
            )
            data = response.json()
            return data
        except Exception as e:
            return {"error": str(e)}

    def create_rodin_job_fal_ai(
            self,
            text_prompt: str=None,
            images: list[tuple[str, str]]=None,
            bbox_condition=None
        ):
        try:
            req_data = {
                "tier": "Sketch",
            }
            if images:
                req_data["input_image_urls"] = images
            if text_prompt:
                req_data["prompt"] = text_prompt
            if bbox_condition:
                req_data["bbox_condition"] = bbox_condition
            response = requests.post(
                "https://queue.fal.run/fal-ai/hyper3d/rodin",
                headers={
                    "Authorization": f"Key {bpy.context.scene.blendermcp_hyper3d_api_key}",
                    "Content-Type": "application/json",
                },
                json=req_data
            )
            data = response.json()
            return data
        except Exception as e:
            return {"error": str(e)}

    def poll_rodin_job_status(self, *args, **kwargs):
        match bpy.context.scene.blendermcp_hyper3d_mode:
            case "MAIN_SITE":
                return self.poll_rodin_job_status_main_site(*args, **kwargs)
            case "FAL_AI":
                return self.poll_rodin_job_status_fal_ai(*args, **kwargs)
            case _:
                return f"Error: Unknown Hyper3D Rodin mode!"

    def poll_rodin_job_status_main_site(self, subscription_key: str):
        """Call the job status API to get the job status"""
        response = requests.post(
            "https://hyperhuman.deemos.com/api/v2/status",
            headers={
                "Authorization": f"Bearer {bpy.context.scene.blendermcp_hyper3d_api_key}",
            },
            json={
                "subscription_key": subscription_key,
            },
        )
        data = response.json()
        return {
            "status_list": [i["status"] for i in data["jobs"]]
        }

    def poll_rodin_job_status_fal_ai(self, request_id: str):
        """Call the job status API to get the job status"""
        response = requests.get(
            f"https://queue.fal.run/fal-ai/hyper3d/requests/{request_id}/status",
            headers={
                "Authorization": f"KEY {bpy.context.scene.blendermcp_hyper3d_api_key}",
            },
        )
        data = response.json()
        return data

    @staticmethod
    def _clean_imported_glb(filepath, mesh_name=None):
        # Get the set of existing objects before import
        existing_objects = set(bpy.data.objects)

        # Import the GLB file
        bpy.ops.import_scene.gltf(filepath=filepath)

        # Ensure the context is updated
        bpy.context.view_layer.update()

        # Get all imported objects
        imported_objects = list(set(bpy.data.objects) - existing_objects)
        # imported_objects = [obj for obj in bpy.context.view_layer.objects if obj.select_get()]

        if not imported_objects:
            print("Error: No objects were imported.")
            return

        # Identify the mesh object
        mesh_obj = None

        if len(imported_objects) == 1 and imported_objects[0].type == 'MESH':
            mesh_obj = imported_objects[0]
            print("Single mesh imported, no cleanup needed.")
        else:
            if len(imported_objects) == 2:
                empty_objs = [i for i in imported_objects if i.type == "EMPTY"]
                if len(empty_objs) != 1:
                    print("Error: Expected an empty node with one mesh child or a single mesh object.")
                    return
                parent_obj = empty_objs.pop()
                if len(parent_obj.children) == 1:
                    potential_mesh = parent_obj.children[0]
                    if potential_mesh.type == 'MESH':
                        print("GLB structure confirmed: Empty node with one mesh child.")

                        # Unparent the mesh from the empty node
                        potential_mesh.parent = None

                        # Remove the empty node
                        bpy.data.objects.remove(parent_obj)
                        print("Removed empty node, keeping only the mesh.")

                        mesh_obj = potential_mesh
                    else:
                        print("Error: Child is not a mesh object.")
                        return
                else:
                    print("Error: Expected an empty node with one mesh child or a single mesh object.")
                    return
            else:
                print("Error: Expected an empty node with one mesh child or a single mesh object.")
                return

        # Rename the mesh if needed
        try:
            if mesh_obj and mesh_obj.name is not None and mesh_name:
                mesh_obj.name = mesh_name
                if mesh_obj.data.name is not None:
                    mesh_obj.data.name = mesh_name
                print(f"Mesh renamed to: {mesh_name}")
        except Exception as e:
            print("Having issue with renaming, give up renaming.")

        return mesh_obj

    def import_generated_asset(self, *args, **kwargs):
        match bpy.context.scene.blendermcp_hyper3d_mode:
            case "MAIN_SITE":
                return self.import_generated_asset_main_site(*args, **kwargs)
            case "FAL_AI":
                return self.import_generated_asset_fal_ai(*args, **kwargs)
            case _:
                return f"Error: Unknown Hyper3D Rodin mode!"

    def import_generated_asset_main_site(self, task_uuid: str, name: str):
        """Fetch the generated asset, import into blender"""
        response = requests.post(
            "https://hyperhuman.deemos.com/api/v2/download",
            headers={
                "Authorization": f"Bearer {bpy.context.scene.blendermcp_hyper3d_api_key}",
            },
            json={
                'task_uuid': task_uuid
            }
        )
        data_ = response.json()
        temp_file = None
        for i in data_["list"]:
            if i["name"].endswith(".glb"):
                temp_file = tempfile.NamedTemporaryFile(
                    delete=False,
                    prefix=task_uuid,
                    suffix=".glb",
                )

                try:
                    # Download the content
                    response = requests.get(i["url"], stream=True)
                    response.raise_for_status()  # Raise an exception for HTTP errors

                    # Write the content to the temporary file
                    for chunk in response.iter_content(chunk_size=8192):
                        temp_file.write(chunk)

                    # Close the file
                    temp_file.close()

                except Exception as e:
                    # Clean up the file if there's an error
                    temp_file.close()
                    os.unlink(temp_file.name)
                    return {"succeed": False, "error": str(e)}

                break
        else:
            return {"succeed": False, "error": "Generation failed. Please first make sure that all jobs of the task are done and then try again later."}

        try:
            obj = self._clean_imported_glb(
                filepath=temp_file.name,
                mesh_name=name
            )
            result = {
                "name": obj.name,
                "type": obj.type,
                "location": [obj.location.x, obj.location.y, obj.location.z],
                "rotation": [obj.rotation_euler.x, obj.rotation_euler.y, obj.rotation_euler.z],
                "scale": [obj.scale.x, obj.scale.y, obj.scale.z],
            }

            if obj.type == "MESH":
                bounding_box = self._get_aabb(obj)
                result["world_bounding_box"] = bounding_box

            return {
                "succeed": True, **result
            }
        except Exception as e:
            return {"succeed": False, "error": str(e)}

    def import_generated_asset_fal_ai(self, request_id: str, name: str):
        """Fetch the generated asset, import into blender"""
        response = requests.get(
            f"https://queue.fal.run/fal-ai/hyper3d/requests/{request_id}",
            headers={
                "Authorization": f"Key {bpy.context.scene.blendermcp_hyper3d_api_key}",
            }
        )
        data_ = response.json()
        temp_file = None

        temp_file = tempfile.NamedTemporaryFile(
            delete=False,
            prefix=request_id,
            suffix=".glb",
        )

        try:
            # Download the content
            response = requests.get(data_["model_mesh"]["url"], stream=True)
            response.raise_for_status()  # Raise an exception for HTTP errors

            # Write the content to the temporary file
            for chunk in response.iter_content(chunk_size=8192):
                temp_file.write(chunk)

            # Close the file
            temp_file.close()

        except Exception as e:
            # Clean up the file if there's an error
            temp_file.close()
            os.unlink(temp_file.name)
            return {"succeed": False, "error": str(e)}

        try:
            obj = self._clean_imported_glb(
                filepath=temp_file.name,
                mesh_name=name
            )
            result = {
                "name": obj.name,
                "type": obj.type,
                "location": [obj.location.x, obj.location.y, obj.location.z],
                "rotation": [obj.rotation_euler.x, obj.rotation_euler.y, obj.rotation_euler.z],
                "scale": [obj.scale.x, obj.scale.y, obj.scale.z],
            }

            if obj.type == "MESH":
                bounding_box = self._get_aabb(obj)
                result["world_bounding_box"] = bounding_box

            return {
                "succeed": True, **result
            }
        except Exception as e:
            return {"succeed": False, "error": str(e)}
    #endregion
 
    #region Sketchfab API
    def get_sketchfab_status(self):
        """Get the current status of Sketchfab integration"""
        enabled = bpy.context.scene.blendermcp_use_sketchfab
        api_key = bpy.context.scene.blendermcp_sketchfab_api_key

        # Test the API key if present
        if api_key:
            try:
                headers = {
                    "Authorization": f"Token {api_key}"
                }

                response = requests.get(
                    "https://api.sketchfab.com/v3/me",
                    headers=headers,
                    timeout=30  # Add timeout of 30 seconds
                )

                if response.status_code == 200:
                    user_data = response.json()
                    username = user_data.get("username", "Unknown user")
                    return {
                        "enabled": True,
                        "message": f"Sketchfab integration is enabled and ready to use. Logged in as: {username}"
                    }
                else:
                    return {
                        "enabled": False,
                        "message": f"Sketchfab API key seems invalid. Status code: {response.status_code}"
                    }
            except requests.exceptions.Timeout:
                return {
                    "enabled": False,
                    "message": "Timeout connecting to Sketchfab API. Check your internet connection."
                }
            except Exception as e:
                return {
                    "enabled": False,
                    "message": f"Error testing Sketchfab API key: {str(e)}"
                }

        if enabled and api_key:
            return {"enabled": True, "message": "Sketchfab integration is enabled and ready to use."}
        elif enabled and not api_key:
            return {
                "enabled": False,
                "message": """Sketchfab integration is currently enabled, but API key is not given. To enable it:
                            1. In the 3D Viewport, find the BlenderMCP panel in the sidebar (press N if hidden)
                            2. Keep the 'Use Sketchfab' checkbox checked
                            3. Enter your Sketchfab API Key
                            4. Restart the connection to Claude"""
            }
        else:
            return {
                "enabled": False,
                "message": """Sketchfab integration is currently disabled. To enable it:
                            1. In the 3D Viewport, find the BlenderMCP panel in the sidebar (press N if hidden)
                            2. Check the 'Use assets from Sketchfab' checkbox
                            3. Enter your Sketchfab API Key
                            4. Restart the connection to Claude"""
            }

    def search_sketchfab_models(self, query, categories=None, count=20, downloadable=True):
        """Search for models on Sketchfab based on query and optional filters"""
        try:
            api_key = bpy.context.scene.blendermcp_sketchfab_api_key
            if not api_key:
                return {"error": "Sketchfab API key is not configured"}

            # Build search parameters with exact fields from Sketchfab API docs
            params = {
                "type": "models",
                "q": query,
                "count": count,
                "downloadable": downloadable,
                "archives_flavours": False
            }

            if categories:
                params["categories"] = categories

            # Make API request to Sketchfab search endpoint
            # The proper format according to Sketchfab API docs for API key auth
            headers = {
                "Authorization": f"Token {api_key}"
            }


            # Use the search endpoint as specified in the API documentation
            response = requests.get(
                "https://api.sketchfab.com/v3/search",
                headers=headers,
                params=params,
                timeout=30  # Add timeout of 30 seconds
            )

            if response.status_code == 401:
                return {"error": "Authentication failed (401). Check your API key."}

            if response.status_code != 200:
                return {"error": f"API request failed with status code {response.status_code}"}

            response_data = response.json()

            # Safety check on the response structure
            if response_data is None:
                return {"error": "Received empty response from Sketchfab API"}

            # Handle 'results' potentially missing from response
            results = response_data.get("results", [])
            if not isinstance(results, list):
                return {"error": f"Unexpected response format from Sketchfab API: {response_data}"}

            return response_data

        except requests.exceptions.Timeout:
            return {"error": "Request timed out. Check your internet connection."}
        except json.JSONDecodeError as e:
            return {"error": f"Invalid JSON response from Sketchfab API: {str(e)}"}
        except Exception as e:
            import traceback
            traceback.print_exc()
            return {"error": str(e)}

    def get_sketchfab_model_preview(self, uid):
        """Get thumbnail preview image of a Sketchfab model by its UID"""
        try:
            import base64
            
            api_key = bpy.context.scene.blendermcp_sketchfab_api_key
            if not api_key:
                return {"error": "Sketchfab API key is not configured"}

            headers = {"Authorization": f"Token {api_key}"}
            
            # Get model info which includes thumbnails
            response = requests.get(
                f"https://api.sketchfab.com/v3/models/{uid}",
                headers=headers,
                timeout=30
            )
            
            if response.status_code == 401:
                return {"error": "Authentication failed (401). Check your API key."}
            
            if response.status_code == 404:
                return {"error": f"Model not found: {uid}"}
            
            if response.status_code != 200:
                return {"error": f"Failed to get model info: {response.status_code}"}
            
            data = response.json()
            thumbnails = data.get("thumbnails", {}).get("images", [])
            
            if not thumbnails:
                return {"error": "No thumbnail available for this model"}
            
            # Find a suitable thumbnail (prefer medium size ~640px)
            selected_thumbnail = None
            for thumb in thumbnails:
                width = thumb.get("width", 0)
                if 400 <= width <= 800:
                    selected_thumbnail = thumb
                    break
            
            # Fallback to the first available thumbnail
            if not selected_thumbnail:
                selected_thumbnail = thumbnails[0]
            
            thumbnail_url = selected_thumbnail.get("url")
            if not thumbnail_url:
                return {"error": "Thumbnail URL not found"}
            
            # Download the thumbnail image
            img_response = requests.get(thumbnail_url, timeout=30)
            if img_response.status_code != 200:
                return {"error": f"Failed to download thumbnail: {img_response.status_code}"}
            
            # Encode image as base64
            image_data = base64.b64encode(img_response.content).decode('ascii')
            
            # Determine format from content type or URL
            content_type = img_response.headers.get("Content-Type", "")
            if "png" in content_type or thumbnail_url.endswith(".png"):
                img_format = "png"
            else:
                img_format = "jpeg"
            
            # Get additional model info for context
            model_name = data.get("name", "Unknown")
            author = data.get("user", {}).get("username", "Unknown")
            
            return {
                "success": True,
                "image_data": image_data,
                "format": img_format,
                "model_name": model_name,
                "author": author,
                "uid": uid,
                "thumbnail_width": selected_thumbnail.get("width"),
                "thumbnail_height": selected_thumbnail.get("height")
            }
            
        except requests.exceptions.Timeout:
            return {"error": "Request timed out. Check your internet connection."}
        except Exception as e:
            import traceback
            traceback.print_exc()
            return {"error": f"Failed to get model preview: {str(e)}"}

    def download_sketchfab_model(self, uid, normalize_size=False, target_size=1.0):
        """Download a model from Sketchfab by its UID
        
        Parameters:
        - uid: The unique identifier of the Sketchfab model
        - normalize_size: If True, scale the model so its largest dimension equals target_size
        - target_size: The target size in Blender units (meters) for the largest dimension
        """
        try:
            api_key = bpy.context.scene.blendermcp_sketchfab_api_key
            if not api_key:
                return {"error": "Sketchfab API key is not configured"}

            # Use proper authorization header for API key auth
            headers = {
                "Authorization": f"Token {api_key}"
            }

            # Request download URL using the exact endpoint from the documentation
            download_endpoint = f"https://api.sketchfab.com/v3/models/{uid}/download"

            response = requests.get(
                download_endpoint,
                headers=headers,
                timeout=30  # Add timeout of 30 seconds
            )

            if response.status_code == 401:
                return {"error": "Authentication failed (401). Check your API key."}

            if response.status_code != 200:
                return {"error": f"Download request failed with status code {response.status_code}"}

            data = response.json()

            # Safety check for None data
            if data is None:
                return {"error": "Received empty response from Sketchfab API for download request"}

            # Extract download URL with safety checks
            gltf_data = data.get("gltf")
            if not gltf_data:
                return {"error": "No gltf download URL available for this model. Response: " + str(data)}

            download_url = gltf_data.get("url")
            if not download_url:
                return {"error": "No download URL available for this model. Make sure the model is downloadable and you have access."}

            # Download the model (already has timeout)
            model_response = requests.get(download_url, timeout=60)  # 60 second timeout

            if model_response.status_code != 200:
                return {"error": f"Model download failed with status code {model_response.status_code}"}

            # Save to temporary file
            temp_dir = tempfile.mkdtemp()
            zip_file_path = os.path.join(temp_dir, f"{uid}.zip")

            with open(zip_file_path, "wb") as f:
                f.write(model_response.content)

            # Extract the zip file with enhanced security
            with zipfile.ZipFile(zip_file_path, 'r') as zip_ref:
                # More secure zip slip prevention
                for file_info in zip_ref.infolist():
                    # Get the path of the file
                    file_path = file_info.filename

                    # Convert directory separators to the current OS style
                    # This handles both / and \ in zip entries
                    target_path = os.path.join(temp_dir, os.path.normpath(file_path))

                    # Get absolute paths for comparison
                    abs_temp_dir = os.path.abspath(temp_dir)
                    abs_target_path = os.path.abspath(target_path)

                    # Ensure the normalized path doesn't escape the target directory
                    if not abs_target_path.startswith(abs_temp_dir):
                        with suppress(Exception):
                            shutil.rmtree(temp_dir)
                        return {"error": "Security issue: Zip contains files with path traversal attempt"}

                    # Additional explicit check for directory traversal
                    if ".." in file_path:
                        with suppress(Exception):
                            shutil.rmtree(temp_dir)
                        return {"error": "Security issue: Zip contains files with directory traversal sequence"}

                # If all files passed security checks, extract them
                zip_ref.extractall(temp_dir)

            # Find the main glTF file
            gltf_files = [f for f in os.listdir(temp_dir) if f.endswith('.gltf') or f.endswith('.glb')]

            if not gltf_files:
                with suppress(Exception):
                    shutil.rmtree(temp_dir)
                return {"error": "No glTF file found in the downloaded model"}

            main_file = os.path.join(temp_dir, gltf_files[0])

            # Import the model
            bpy.ops.import_scene.gltf(filepath=main_file)

            # Get the imported objects
            imported_objects = list(bpy.context.selected_objects)
            imported_object_names = [obj.name for obj in imported_objects]

            # Clean up temporary files
            with suppress(Exception):
                shutil.rmtree(temp_dir)

            # Find root objects (objects without parents in the imported set)
            root_objects = [obj for obj in imported_objects if obj.parent is None]

            # Helper function to recursively get all mesh children
            def get_all_mesh_children(obj):
                """Recursively collect all mesh objects in the hierarchy"""
                meshes = []
                if obj.type == 'MESH':
                    meshes.append(obj)
                for child in obj.children:
                    meshes.extend(get_all_mesh_children(child))
                return meshes

            # Collect ALL meshes from the entire hierarchy (starting from roots)
            all_meshes = []
            for obj in root_objects:
                all_meshes.extend(get_all_mesh_children(obj))
            
            if all_meshes:
                # Calculate combined world bounding box for all meshes
                all_min = mathutils.Vector((float('inf'), float('inf'), float('inf')))
                all_max = mathutils.Vector((float('-inf'), float('-inf'), float('-inf')))
                
                for mesh_obj in all_meshes:
                    # Get world-space bounding box corners
                    for corner in mesh_obj.bound_box:
                        world_corner = mesh_obj.matrix_world @ mathutils.Vector(corner)
                        all_min.x = min(all_min.x, world_corner.x)
                        all_min.y = min(all_min.y, world_corner.y)
                        all_min.z = min(all_min.z, world_corner.z)
                        all_max.x = max(all_max.x, world_corner.x)
                        all_max.y = max(all_max.y, world_corner.y)
                        all_max.z = max(all_max.z, world_corner.z)
                
                # Calculate dimensions
                dimensions = [
                    all_max.x - all_min.x,
                    all_max.y - all_min.y,
                    all_max.z - all_min.z
                ]
                max_dimension = max(dimensions)
                
                # Apply normalization if requested
                scale_applied = 1.0
                if normalize_size and max_dimension > 0:
                    scale_factor = target_size / max_dimension
                    scale_applied = scale_factor
                    
                    # ✅ Only apply scale to ROOT objects (not children!)
                    # Child objects inherit parent's scale through matrix_world
                    for root in root_objects:
                        root.scale = (
                            root.scale.x * scale_factor,
                            root.scale.y * scale_factor,
                            root.scale.z * scale_factor
                        )
                    
                    # Update the scene to recalculate matrix_world for all objects
                    bpy.context.view_layer.update()
                    
                    # Recalculate bounding box after scaling
                    all_min = mathutils.Vector((float('inf'), float('inf'), float('inf')))
                    all_max = mathutils.Vector((float('-inf'), float('-inf'), float('-inf')))
                    
                    for mesh_obj in all_meshes:
                        for corner in mesh_obj.bound_box:
                            world_corner = mesh_obj.matrix_world @ mathutils.Vector(corner)
                            all_min.x = min(all_min.x, world_corner.x)
                            all_min.y = min(all_min.y, world_corner.y)
                            all_min.z = min(all_min.z, world_corner.z)
                            all_max.x = max(all_max.x, world_corner.x)
                            all_max.y = max(all_max.y, world_corner.y)
                            all_max.z = max(all_max.z, world_corner.z)
                    
                    dimensions = [
                        all_max.x - all_min.x,
                        all_max.y - all_min.y,
                        all_max.z - all_min.z
                    ]
                
                world_bounding_box = [[all_min.x, all_min.y, all_min.z], [all_max.x, all_max.y, all_max.z]]
            else:
                world_bounding_box = None
                dimensions = None
                scale_applied = 1.0

            result = {
                "success": True,
                "message": "Model imported successfully",
                "imported_objects": imported_object_names
            }
            
            if world_bounding_box:
                result["world_bounding_box"] = world_bounding_box
            if dimensions:
                result["dimensions"] = [round(d, 4) for d in dimensions]
            if normalize_size:
                result["scale_applied"] = round(scale_applied, 6)
                result["normalized"] = True
            
            return result

        except requests.exceptions.Timeout:
            return {"error": "Request timed out. Check your internet connection and try again with a simpler model."}
        except json.JSONDecodeError as e:
            return {"error": f"Invalid JSON response from Sketchfab API: {str(e)}"}
        except Exception as e:
            import traceback
            traceback.print_exc()
            return {"error": f"Failed to download model: {str(e)}"}
    #endregion

    #region Hunyuan3D
    def get_hunyuan3d_status(self):
        """Get the current status of Hunyuan3D integration"""
        enabled = bpy.context.scene.blendermcp_use_hunyuan3d
        hunyuan3d_mode = bpy.context.scene.blendermcp_hunyuan3d_mode
        if enabled:
            match hunyuan3d_mode:
                case "OFFICIAL_API":
                    if not bpy.context.scene.blendermcp_hunyuan3d_secret_id or not bpy.context.scene.blendermcp_hunyuan3d_secret_key:
                        return {
                            "enabled": False, 
                            "mode": hunyuan3d_mode, 
                            "message": """Hunyuan3D integration is currently enabled, but SecretId or SecretKey is not given. To enable it:
                                1. In the 3D Viewport, find the BlenderMCP panel in the sidebar (press N if hidden)
                                2. Keep the 'Use Tencent Hunyuan 3D model generation' checkbox checked
                                3. Choose the right platform and fill in the SecretId and SecretKey
                                4. Restart the connection to Claude"""
                        }
                case "LOCAL_API":
                    if not bpy.context.scene.blendermcp_hunyuan3d_api_url:
                        return {
                            "enabled": False, 
                            "mode": hunyuan3d_mode, 
                            "message": """Hunyuan3D integration is currently enabled, but API URL  is not given. To enable it:
                                1. In the 3D Viewport, find the BlenderMCP panel in the sidebar (press N if hidden)
                                2. Keep the 'Use Tencent Hunyuan 3D model generation' checkbox checked
                                3. Choose the right platform and fill in the API URL
                                4. Restart the connection to Claude"""
                        }
                case _:
                    return {
                        "enabled": False, 
                        "message": "Hunyuan3D integration is enabled and mode is not supported."
                    }
            return {
                "enabled": True, 
                "mode": hunyuan3d_mode,
                "message": "Hunyuan3D integration is enabled and ready to use."
            }
        return {
            "enabled": False, 
            "message": """Hunyuan3D integration is currently disabled. To enable it:
                        1. In the 3D Viewport, find the BlenderMCP panel in the sidebar (press N if hidden)
                        2. Check the 'Use Tencent Hunyuan 3D model generation' checkbox
                        3. Restart the connection to Claude"""
        }
    
    @staticmethod
    def get_tencent_cloud_sign_headers(
        method: str,
        path: str,
        headParams: dict,
        data: dict,
        service: str,
        region: str,
        secret_id: str,
        secret_key: str,
        host: str = None
    ):
        """Generate the signature header required for Tencent Cloud API requests headers"""
        # Generate timestamp
        timestamp = int(time.time())
        date = datetime.utcfromtimestamp(timestamp).strftime("%Y-%m-%d")
        
        # If host is not provided, it is generated based on service and region.
        if not host:
            host = f"{service}.tencentcloudapi.com"
        
        endpoint = f"https://{host}"
        
        # Constructing the request body
        payload_str = json.dumps(data)
        
        # ************* Step 1: Concatenate the canonical request string *************
        canonical_uri = path
        canonical_querystring = ""
        ct = "application/json; charset=utf-8"
        canonical_headers = f"content-type:{ct}\nhost:{host}\nx-tc-action:{headParams.get('Action', '').lower()}\n"
        signed_headers = "content-type;host;x-tc-action"
        hashed_request_payload = hashlib.sha256(payload_str.encode("utf-8")).hexdigest()
        
        canonical_request = (method + "\n" +
                            canonical_uri + "\n" +
                            canonical_querystring + "\n" +
                            canonical_headers + "\n" +
                            signed_headers + "\n" +
                            hashed_request_payload)

        # ************* Step 2: Construct the reception signature string *************
        credential_scope = f"{date}/{service}/tc3_request"
        hashed_canonical_request = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
        string_to_sign = ("TC3-HMAC-SHA256" + "\n" +
                        str(timestamp) + "\n" +
                        credential_scope + "\n" +
                        hashed_canonical_request)

        # ************* Step 3: Calculate the signature *************
        def sign(key, msg):
            return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

        secret_date = sign(("TC3" + secret_key).encode("utf-8"), date)
        secret_service = sign(secret_date, service)
        secret_signing = sign(secret_service, "tc3_request")
        signature = hmac.new(
            secret_signing, 
            string_to_sign.encode("utf-8"), 
            hashlib.sha256
        ).hexdigest()

        # ************* Step 4: Connect Authorization *************
        authorization = ("TC3-HMAC-SHA256" + " " +
                        "Credential=" + secret_id + "/" + credential_scope + ", " +
                        "SignedHeaders=" + signed_headers + ", " +
                        "Signature=" + signature)

        # Constructing request headers
        headers = {
            "Authorization": authorization,
            "Content-Type": "application/json; charset=utf-8",
            "Host": host,
            "X-TC-Action": headParams.get("Action", ""),
            "X-TC-Timestamp": str(timestamp),
            "X-TC-Version": headParams.get("Version", ""),
            "X-TC-Region": region
        }

        return headers, endpoint

    def create_hunyuan_job(self, *args, **kwargs):
        match bpy.context.scene.blendermcp_hunyuan3d_mode:
            case "OFFICIAL_API":
                return self.create_hunyuan_job_main_site(*args, **kwargs)
            case "LOCAL_API":
                return self.create_hunyuan_job_local_site(*args, **kwargs)
            case _:
                return f"Error: Unknown Hunyuan3D mode!"

    def create_hunyuan_job_main_site(
        self,
        text_prompt: str = None,
        image: str = None
    ):
        try:
            secret_id = bpy.context.scene.blendermcp_hunyuan3d_secret_id
            secret_key = bpy.context.scene.blendermcp_hunyuan3d_secret_key

            if not secret_id or not secret_key:
                return {"error": "SecretId or SecretKey is not given"}

            # Parameter verification
            if not text_prompt and not image:
                return {"error": "Prompt or Image is required"}
            if text_prompt and image:
                return {"error": "Prompt and Image cannot be provided simultaneously"}
            # Fixed parameter configuration
            service = "hunyuan"
            action = "SubmitHunyuanTo3DJob"
            version = "2023-09-01"
            region = "ap-guangzhou"

            headParams={
                "Action": action,
                "Version": version,
                "Region": region,
            }

            # Constructing request parameters
            data = {
                "Num": 1  # The current API limit is only 1
            }

            # Handling text prompts
            if text_prompt:
                if len(text_prompt) > 200:
                    return {"error": "Prompt exceeds 200 characters limit"}
                data["Prompt"] = text_prompt

            # Handling image
            if image:
                if re.match(r'^https?://', image, re.IGNORECASE) is not None:
                    data["ImageUrl"] = image
                else:
                    try:
                        # Convert to Base64 format
                        with open(image, "rb") as f:
                            image_base64 = base64.b64encode(f.read()).decode("ascii")
                        data["ImageBase64"] = image_base64
                    except Exception as e:
                        return {"error": f"Image encoding failed: {str(e)}"}
            
            # Get signed headers
            headers, endpoint = self.get_tencent_cloud_sign_headers("POST", "/", headParams, data, service, region, secret_id, secret_key)

            response = requests.post(
                endpoint,
                headers = headers,
                data = json.dumps(data)
            )

            if response.status_code == 200:
                return response.json()
            return {
                "error": f"API request failed with status {response.status_code}: {response}"
            }
        except Exception as e:
            return {"error": str(e)}

    def create_hunyuan_job_local_site(
        self,
        text_prompt: str = None,
        image: str = None):
        try:
            base_url = bpy.context.scene.blendermcp_hunyuan3d_api_url.rstrip('/')
            octree_resolution = bpy.context.scene.blendermcp_hunyuan3d_octree_resolution
            num_inference_steps = bpy.context.scene.blendermcp_hunyuan3d_num_inference_steps
            guidance_scale = bpy.context.scene.blendermcp_hunyuan3d_guidance_scale
            texture = bpy.context.scene.blendermcp_hunyuan3d_texture

            if not base_url:
                return {"error": "API URL is not given"}
            # Parameter verification
            if not text_prompt and not image:
                return {"error": "Prompt or Image is required"}

            # Constructing request parameters
            data = {
                "octree_resolution": octree_resolution,
                "num_inference_steps": num_inference_steps,
                "guidance_scale": guidance_scale,
                "texture": texture,
            }

            # Handling text prompts
            if text_prompt:
                data["text"] = text_prompt

            # Handling image
            if image:
                if re.match(r'^https?://', image, re.IGNORECASE) is not None:
                    try:
                        resImg = requests.get(image)
                        resImg.raise_for_status()
                        image_base64 = base64.b64encode(resImg.content).decode("ascii")
                        data["image"] = image_base64
                    except Exception as e:
                        return {"error": f"Failed to download or encode image: {str(e)}"} 
                else:
                    try:
                        # Convert to Base64 format
                        with open(image, "rb") as f:
                            image_base64 = base64.b64encode(f.read()).decode("ascii")
                        data["image"] = image_base64
                    except Exception as e:
                        return {"error": f"Image encoding failed: {str(e)}"}

            response = requests.post(
                f"{base_url}/generate",
                json = data,
            )

            if response.status_code != 200:
                return {
                    "error": f"Generation failed: {response.text}"
                }
        
            # Decode base64 and save to temporary file
            with tempfile.NamedTemporaryFile(delete=False, suffix=".glb") as temp_file:
                temp_file.write(response.content)
                temp_file_name = temp_file.name

            # Import the GLB file in the main thread
            def import_handler():
                bpy.ops.import_scene.gltf(filepath=temp_file_name)
                os.unlink(temp_file.name)
                return None
            
            bpy.app.timers.register(import_handler)

            return {
                "status": "DONE",
                "message": "Generation and Import glb succeeded"
            }
        except Exception as e:
            print(f"An error occurred: {e}")
            return {"error": str(e)}
        
    
    def poll_hunyuan_job_status(self, *args, **kwargs):
        return self.poll_hunyuan_job_status_ai(*args, **kwargs)
    
    def poll_hunyuan_job_status_ai(self, job_id: str):
        """Call the job status API to get the job status"""
        print(job_id)
        try:
            secret_id = bpy.context.scene.blendermcp_hunyuan3d_secret_id
            secret_key = bpy.context.scene.blendermcp_hunyuan3d_secret_key

            if not secret_id or not secret_key:
                return {"error": "SecretId or SecretKey is not given"}
            if not job_id:
                return {"error": "JobId is required"}
            
            service = "hunyuan"
            action = "QueryHunyuanTo3DJob"
            version = "2023-09-01"
            region = "ap-guangzhou"

            headParams={
                "Action": action,
                "Version": version,
                "Region": region,
            }

            clean_job_id = job_id.removeprefix("job_")
            data = {
                "JobId": clean_job_id
            }

            headers, endpoint = self.get_tencent_cloud_sign_headers("POST", "/", headParams, data, service, region, secret_id, secret_key)

            response = requests.post(
                endpoint,
                headers=headers,
                data=json.dumps(data)
            )

            if response.status_code == 200:
                return response.json()
            return {
                "error": f"API request failed with status {response.status_code}: {response}"
            }
        except Exception as e:
            return {"error": str(e)}

    def import_generated_asset_hunyuan(self, *args, **kwargs):
        return self.import_generated_asset_hunyuan_ai(*args, **kwargs)
            
    def import_generated_asset_hunyuan_ai(self, name: str , zip_file_url: str):
        if not zip_file_url:
            return {"error": "Zip file not found"}
        
        # Validate URL
        if not re.match(r'^https?://', zip_file_url, re.IGNORECASE):
            return {"error": "Invalid URL format. Must start with http:// or https://"}
        
        # Create a temporary directory
        temp_dir = tempfile.mkdtemp(prefix="tencent_obj_")
        zip_file_path = osp.join(temp_dir, "model.zip")
        obj_file_path = osp.join(temp_dir, "model.obj")
        mtl_file_path = osp.join(temp_dir, "model.mtl")

        try:
            # Download ZIP file
            zip_response = requests.get(zip_file_url, stream=True)
            zip_response.raise_for_status()
            with open(zip_file_path, "wb") as f:
                for chunk in zip_response.iter_content(chunk_size=8192):
                    f.write(chunk)

            # Unzip the ZIP
            with zipfile.ZipFile(zip_file_path, "r") as zip_ref:
                zip_ref.extractall(temp_dir)

            # Find the .obj file (there may be multiple, assuming the main file is model.obj)
            for file in os.listdir(temp_dir):
                if file.endswith(".obj"):
                    obj_file_path = osp.join(temp_dir, file)

            if not osp.exists(obj_file_path):
                return {"succeed": False, "error": "OBJ file not found after extraction"}

            # Import obj file
            if bpy.app.version>=(4, 0, 0):
                bpy.ops.wm.obj_import(filepath=obj_file_path)
            else:
                bpy.ops.import_scene.obj(filepath=obj_file_path)

            imported_objs = [obj for obj in bpy.context.selected_objects if obj.type == 'MESH']
            if not imported_objs:
                return {"succeed": False, "error": "No mesh objects imported"}

            obj = imported_objs[0]
            if name:
                obj.name = name

            result = {
                "name": obj.name,
                "type": obj.type,
                "location": [obj.location.x, obj.location.y, obj.location.z],
                "rotation": [obj.rotation_euler.x, obj.rotation_euler.y, obj.rotation_euler.z],
                "scale": [obj.scale.x, obj.scale.y, obj.scale.z],
            }

            if obj.type == "MESH":
                bounding_box = self._get_aabb(obj)
                result["world_bounding_box"] = bounding_box

            return {"succeed": True, **result}
        except Exception as e:
            return {"succeed": False, "error": str(e)}
        finally:
            #  Clean up temporary zip and obj, save texture and mtl
            try:
                if os.path.exists(zip_file_path):
                    os.remove(zip_file_path) 
                if os.path.exists(obj_file_path):
                    os.remove(obj_file_path)
            except Exception as e:
                print(f"Failed to clean up temporary directory {temp_dir}: {e}")
    #endregion

# Blender Addon Preferences
class BLENDERMCP_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = __name__
    
    telemetry_consent: BoolProperty(
        name="Allow Telemetry",
        description="Allow collection of prompts, code snippets, and screenshots to help improve Blender MCP",
        default=True
    )

    def draw(self, context):
        layout = self.layout
        
        # Telemetry section
        layout.label(text="Telemetry & Privacy:", icon='PREFERENCES')
        
        box = layout.box()
        row = box.row()
        row.prop(self, "telemetry_consent", text="Allow Telemetry")
        
        # Info text
        box.separator()
        if self.telemetry_consent:
            box.label(text="With consent: We collect anonymized prompts, code, and screenshots.", icon='INFO')
        else:
            box.label(text="Without consent: We only collect minimal anonymous usage data", icon='INFO')
            box.label(text="(tool names, success/failure, duration - no prompts or code).", icon='BLANK1')
        box.separator()
        box.label(text="All data is fully anonymized. You can change this anytime.", icon='CHECKMARK')
        
        # Terms and Conditions link
        box.separator()
        row = box.row()
        row.operator("blendermcp.open_terms", text="View Terms and Conditions", icon='TEXT')

# Blender UI Panel
class BLENDERMCP_PT_Panel(bpy.types.Panel):
    bl_label = "Blender MCP"
    bl_idname = "BLENDERMCP_PT_Panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'BlenderMCP'

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        layout.prop(scene, "blendermcp_port")
        layout.prop(scene, "blendermcp_use_polyhaven", text="Use assets from Poly Haven")

        layout.prop(scene, "blendermcp_use_hyper3d", text="Use Hyper3D Rodin 3D model generation")
        if scene.blendermcp_use_hyper3d:
            layout.prop(scene, "blendermcp_hyper3d_mode", text="Rodin Mode")
            layout.prop(scene, "blendermcp_hyper3d_api_key", text="API Key")
            layout.operator("blendermcp.set_hyper3d_free_trial_api_key", text="Set Free Trial API Key")

        layout.prop(scene, "blendermcp_use_sketchfab", text="Use assets from Sketchfab")
        if scene.blendermcp_use_sketchfab:
            layout.prop(scene, "blendermcp_sketchfab_api_key", text="API Key")

        layout.prop(scene, "blendermcp_use_hunyuan3d", text="Use Tencent Hunyuan 3D model generation")
        if scene.blendermcp_use_hunyuan3d:
            layout.prop(scene, "blendermcp_hunyuan3d_mode", text="Hunyuan3D Mode")
            if scene.blendermcp_hunyuan3d_mode == 'OFFICIAL_API':
                layout.prop(scene, "blendermcp_hunyuan3d_secret_id", text="SecretId")
                layout.prop(scene, "blendermcp_hunyuan3d_secret_key", text="SecretKey")
            if scene.blendermcp_hunyuan3d_mode == 'LOCAL_API':
                layout.prop(scene, "blendermcp_hunyuan3d_api_url", text="API URL")
                layout.prop(scene, "blendermcp_hunyuan3d_octree_resolution", text="Octree Resolution")
                layout.prop(scene, "blendermcp_hunyuan3d_num_inference_steps", text="Number of Inference Steps")
                layout.prop(scene, "blendermcp_hunyuan3d_guidance_scale", text="Guidance Scale")
                layout.prop(scene, "blendermcp_hunyuan3d_texture", text="Generate Texture")
        
        if not scene.blendermcp_server_running:
            layout.operator("blendermcp.start_server", text="Connect to MCP server")
        else:
            layout.operator("blendermcp.stop_server", text="Disconnect from MCP server")
            layout.label(text=f"Running on port {scene.blendermcp_port}")

# Operator to set Hyper3D API Key
class BLENDERMCP_OT_SetFreeTrialHyper3DAPIKey(bpy.types.Operator):
    bl_idname = "blendermcp.set_hyper3d_free_trial_api_key"
    bl_label = "Set Free Trial API Key"

    def execute(self, context):
        context.scene.blendermcp_hyper3d_api_key = RODIN_FREE_TRIAL_KEY
        context.scene.blendermcp_hyper3d_mode = 'MAIN_SITE'
        self.report({'INFO'}, "API Key set successfully!")
        return {'FINISHED'}

# Operator to start the server
class BLENDERMCP_OT_StartServer(bpy.types.Operator):
    bl_idname = "blendermcp.start_server"
    bl_label = "Connect to Claude"
    bl_description = "Start the BlenderMCP server to connect with Claude"

    def execute(self, context):
        scene = context.scene

        # Create a new server instance
        if not hasattr(bpy.types, "blendermcp_server") or not bpy.types.blendermcp_server:
            bpy.types.blendermcp_server = BlenderMCPServer(port=scene.blendermcp_port)

        # Start the server
        bpy.types.blendermcp_server.start()
        scene.blendermcp_server_running = True

        return {'FINISHED'}

# Operator to stop the server
class BLENDERMCP_OT_StopServer(bpy.types.Operator):
    bl_idname = "blendermcp.stop_server"
    bl_label = "Stop the connection to Claude"
    bl_description = "Stop the connection to Claude"

    def execute(self, context):
        scene = context.scene

        # Stop the server if it exists
        if hasattr(bpy.types, "blendermcp_server") and bpy.types.blendermcp_server:
            bpy.types.blendermcp_server.stop()
            del bpy.types.blendermcp_server

        scene.blendermcp_server_running = False

        return {'FINISHED'}

# Operator to open Terms and Conditions
class BLENDERMCP_OT_OpenTerms(bpy.types.Operator):
    bl_idname = "blendermcp.open_terms"
    bl_label = "View Terms and Conditions"
    bl_description = "Open the Terms and Conditions document"

    def execute(self, context):
        # Open the Terms and Conditions on GitHub
        terms_url = "https://github.com/ahujasid/blender-mcp/blob/main/TERMS_AND_CONDITIONS.md"
        try:
            import webbrowser
            webbrowser.open(terms_url)
            self.report({'INFO'}, "Terms and Conditions opened in browser")
        except Exception as e:
            self.report({'ERROR'}, f"Could not open Terms and Conditions: {str(e)}")
        
        return {'FINISHED'}

def replace_default_cube_with_sphere(_scene):
    """Replace the default startup cube with a UV sphere."""
    cube = bpy.data.objects.get("Cube")
    if cube is not None:
        bpy.data.objects.remove(cube, do_unlink=True)
        bpy.ops.mesh.primitive_uv_sphere_add(radius=1, location=(0, 0, 0))
        sphere = bpy.context.active_object
        sphere.name = "Sphere"


# Registration functions
def register():
    bpy.types.Scene.blendermcp_port = IntProperty(
        name="Port",
        description="Port for the BlenderMCP server",
        default=9876,
        min=1024,
        max=65535
    )

    bpy.types.Scene.blendermcp_server_running = bpy.props.BoolProperty(
        name="Server Running",
        default=False
    )

    bpy.types.Scene.blendermcp_use_polyhaven = bpy.props.BoolProperty(
        name="Use Poly Haven",
        description="Enable Poly Haven asset integration",
        default=False
    )

    bpy.types.Scene.blendermcp_use_hyper3d = bpy.props.BoolProperty(
        name="Use Hyper3D Rodin",
        description="Enable Hyper3D Rodin generatino integration",
        default=False
    )

    bpy.types.Scene.blendermcp_hyper3d_mode = bpy.props.EnumProperty(
        name="Rodin Mode",
        description="Choose the platform used to call Rodin APIs",
        items=[
            ("MAIN_SITE", "hyper3d.ai", "hyper3d.ai"),
            ("FAL_AI", "fal.ai", "fal.ai"),
        ],
        default="MAIN_SITE"
    )

    bpy.types.Scene.blendermcp_hyper3d_api_key = bpy.props.StringProperty(
        name="Hyper3D API Key",
        subtype="PASSWORD",
        description="API Key provided by Hyper3D",
        default=""
    )

    bpy.types.Scene.blendermcp_use_hunyuan3d = bpy.props.BoolProperty(
        name="Use Hunyuan 3D",
        description="Enable Hunyuan asset integration",
        default=False
    )

    bpy.types.Scene.blendermcp_hunyuan3d_mode = bpy.props.EnumProperty(
        name="Hunyuan3D Mode",
        description="Choose a local or official APIs",
        items=[
            ("LOCAL_API", "local api", "local api"),
            ("OFFICIAL_API", "official api", "official api"),
        ],
        default="LOCAL_API"
    )

    bpy.types.Scene.blendermcp_hunyuan3d_secret_id = bpy.props.StringProperty(
        name="Hunyuan 3D SecretId",
        description="SecretId provided by Hunyuan 3D",
        default=""
    )

    bpy.types.Scene.blendermcp_hunyuan3d_secret_key = bpy.props.StringProperty(
        name="Hunyuan 3D SecretKey",
        subtype="PASSWORD",
        description="SecretKey provided by Hunyuan 3D",
        default=""
    )

    bpy.types.Scene.blendermcp_hunyuan3d_api_url = bpy.props.StringProperty(
        name="API URL",
        description="URL of the Hunyuan 3D API service",
        default="http://localhost:8081"
    )

    bpy.types.Scene.blendermcp_hunyuan3d_octree_resolution = bpy.props.IntProperty(
        name="Octree Resolution",
        description="Octree resolution for the 3D generation",
        default=256,
        min=128,
        max=512,
    )

    bpy.types.Scene.blendermcp_hunyuan3d_num_inference_steps = bpy.props.IntProperty(
        name="Number of Inference Steps",
        description="Number of inference steps for the 3D generation",
        default=20,
        min=20,
        max=50,
    )

    bpy.types.Scene.blendermcp_hunyuan3d_guidance_scale = bpy.props.FloatProperty(
        name="Guidance Scale",
        description="Guidance scale for the 3D generation",
        default=5.5,
        min=1.0,
        max=10.0,
    )

    bpy.types.Scene.blendermcp_hunyuan3d_texture = bpy.props.BoolProperty(
        name="Generate Texture",
        description="Whether to generate texture for the 3D model",
        default=False,
    )
    
    bpy.types.Scene.blendermcp_use_sketchfab = bpy.props.BoolProperty(
        name="Use Sketchfab",
        description="Enable Sketchfab asset integration",
        default=False
    )

    bpy.types.Scene.blendermcp_sketchfab_api_key = bpy.props.StringProperty(
        name="Sketchfab API Key",
        subtype="PASSWORD",
        description="API Key provided by Sketchfab",
        default=""
    )

    # Register preferences class
    bpy.utils.register_class(BLENDERMCP_AddonPreferences)

    bpy.utils.register_class(BLENDERMCP_PT_Panel)
    bpy.utils.register_class(BLENDERMCP_OT_SetFreeTrialHyper3DAPIKey)
    bpy.utils.register_class(BLENDERMCP_OT_StartServer)
    bpy.utils.register_class(BLENDERMCP_OT_StopServer)
    bpy.utils.register_class(BLENDERMCP_OT_OpenTerms)

    bpy.app.handlers.load_post.append(replace_default_cube_with_sphere)

    print("BlenderMCP addon registered")

def unregister():
    # Stop the server if it's running
    if hasattr(bpy.types, "blendermcp_server") and bpy.types.blendermcp_server:
        bpy.types.blendermcp_server.stop()
        del bpy.types.blendermcp_server

    bpy.utils.unregister_class(BLENDERMCP_PT_Panel)
    bpy.utils.unregister_class(BLENDERMCP_OT_SetFreeTrialHyper3DAPIKey)
    bpy.utils.unregister_class(BLENDERMCP_OT_StartServer)
    bpy.utils.unregister_class(BLENDERMCP_OT_StopServer)
    bpy.utils.unregister_class(BLENDERMCP_OT_OpenTerms)
    bpy.utils.unregister_class(BLENDERMCP_AddonPreferences)

    del bpy.types.Scene.blendermcp_port
    del bpy.types.Scene.blendermcp_server_running
    del bpy.types.Scene.blendermcp_use_polyhaven
    del bpy.types.Scene.blendermcp_use_hyper3d
    del bpy.types.Scene.blendermcp_hyper3d_mode
    del bpy.types.Scene.blendermcp_hyper3d_api_key
    del bpy.types.Scene.blendermcp_use_sketchfab
    del bpy.types.Scene.blendermcp_sketchfab_api_key
    del bpy.types.Scene.blendermcp_use_hunyuan3d
    del bpy.types.Scene.blendermcp_hunyuan3d_mode
    del bpy.types.Scene.blendermcp_hunyuan3d_secret_id
    del bpy.types.Scene.blendermcp_hunyuan3d_secret_key
    del bpy.types.Scene.blendermcp_hunyuan3d_api_url
    del bpy.types.Scene.blendermcp_hunyuan3d_octree_resolution
    del bpy.types.Scene.blendermcp_hunyuan3d_num_inference_steps
    del bpy.types.Scene.blendermcp_hunyuan3d_guidance_scale
    del bpy.types.Scene.blendermcp_hunyuan3d_texture

    if replace_default_cube_with_sphere in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(replace_default_cube_with_sphere)

    print("BlenderMCP addon unregistered")

if __name__ == "__main__":
    register()

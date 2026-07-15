# Regression guard for the animation-panel bug class fixed on 2026-07-15:
#   1. action.fcurves doesn't exist on Blender 4.4+'s layered action API —
#      get_action_fcurves() must handle both APIs.
#   2. Actions live on the driving armature, not whatever mesh happens to be
#      active — _resolve_armature() must find it via modifier/parent, not
#      assume the active object IS the armature.
#   3. _action_compatible_with_armature() must match actions by real bone
#      fcurve data, not by object-name substring (the original bug: an
#      armature named "Armature" never matched actions named "frontflip").
#
# Run headless: /Applications/Blender.app/Contents/MacOS/Blender --background \
#   --python tests/test_addon_animation_compat.py
#
# Exits non-zero on any failure so it can gate CI/a pre-commit hook later.

import sys
import os
import importlib.util

import bpy

ADDON_PATH = os.path.join(os.path.dirname(__file__), "..", "addon.py")

failures = []


def check(name, condition):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}")
    if not condition:
        failures.append(name)


def load_addon_module():
    spec = importlib.util.spec_from_file_location("blendermcp_addon_under_test", ADDON_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_rigged_test_scene():
    """Minimal armature + mesh + action, named to deliberately NOT share a
    substring — this is exactly the shape that broke the old name-matching
    filter (armature 'TestRig', action 'unrelated_clip_name')."""
    bpy.ops.wm.read_factory_settings(use_empty=True)

    bpy.ops.object.armature_add(enter_editmode=False, location=(0, 0, 0))
    arm_obj = bpy.context.active_object
    arm_obj.name = "TestRig"
    bpy.ops.object.mode_set(mode='EDIT')
    bone = arm_obj.data.edit_bones[0]
    bone.name = "root_bone"
    bpy.ops.object.mode_set(mode='OBJECT')

    bpy.ops.mesh.primitive_cube_add(location=(0, 0, 0))
    mesh_obj = bpy.context.active_object
    mesh_obj.name = "TestMesh"
    mod = mesh_obj.modifiers.new(name="Armature", type='ARMATURE')
    mod.object = arm_obj

    if not arm_obj.animation_data:
        arm_obj.animation_data_create()
    action = bpy.data.actions.new(name="unrelated_clip_name")
    arm_obj.animation_data.action = action

    bpy.context.view_layer.objects.active = arm_obj
    bpy.ops.object.mode_set(mode='POSE')
    pbone = arm_obj.pose.bones["root_bone"]
    pbone.location = (0, 0, 0)
    pbone.keyframe_insert(data_path="location", frame=1)
    pbone.location = (0, 0, 1)
    pbone.keyframe_insert(data_path="location", frame=48)
    bpy.ops.object.mode_set(mode='OBJECT')

    return arm_obj, mesh_obj, action


def main():
    module = load_addon_module()
    arm_obj, mesh_obj, action = build_rigged_test_scene()

    # ── get_action_fcurves: must return real fcurves on this Blender version ──
    fcurves = module.get_action_fcurves(action, arm_obj)
    check(
        f"get_action_fcurves returns non-empty list on Blender {bpy.app.version} "
        f"(got {len(fcurves)})",
        len(fcurves) > 0,
    )
    check(
        "fcurves target pose.bones data paths",
        all(fc.data_path.startswith('pose.bones["') for fc in fcurves),
    )

    # ── _resolve_armature: mesh with Armature modifier resolves to the rig ──
    resolved_from_mesh = module._resolve_armature(mesh_obj)
    check(
        "_resolve_armature(mesh) finds the armature via modifier, not the mesh itself",
        resolved_from_mesh is arm_obj,
    )
    resolved_from_arm = module._resolve_armature(arm_obj)
    check(
        "_resolve_armature(armature) returns the armature itself",
        resolved_from_arm is arm_obj,
    )
    check(
        "_resolve_armature(None) returns None, doesn't crash",
        module._resolve_armature(None) is None,
    )

    # ── _action_compatible_with_armature: real bone match, NOT name substring ──
    # This is the exact regression: action name "unrelated_clip_name" shares
    # no substring with armature name "TestRig" — the old filter returned
    # False here and hid every action whose name didn't embed the rig's name.
    check(
        "action with no name overlap is still recognized as compatible "
        "(the actual bug: name-substring matching hid all 11 of the robot's actions)",
        module._action_compatible_with_armature(action, arm_obj),
    )

    unrelated_action = bpy.data.actions.new(name="TestRig_but_empty")
    check(
        "an action with zero matching bone fcurves is correctly rejected",
        not module._action_compatible_with_armature(unrelated_action, arm_obj),
    )

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("All checks passed.")
    sys.exit(0)


if __name__ == "__main__":
    main()

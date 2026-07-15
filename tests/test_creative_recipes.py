# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp[cli]", "pillow"]
# ///
"""
Exercises record_creative_recipe()/query_creative_recipe() against the real
server.py — pure Python, no Blender needed. Run: uv run tests/test_creative_recipes.py
"""
import sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import server

failures = []


def check(name, condition):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}")
    if not condition:
        failures.append(name)


record = server.record_creative_recipe
query = server.query_creative_recipe

# Real style recipe — generalized canonical_name, IP names ONLY as trigger phrases.
# This is the exact constraint from the spec: "never hardcode game names."
style_result = json.loads(record(
    recipe_type="style",
    canonical_name="grimdark_souls_fantasy",
    trigger_phrases=["elden ring", "dark souls", "souls-like", "bloodborne", "sekiro"],
    parameters={
        "genre": "dark_fantasy",
        "silhouette": "heavy, large readable forms",
        "color_palette": "cold, muted saturation",
        "materials": ["forged metal", "ancient stone", "weathered leather"],
        "form_language": "asymmetry, layered construction",
        "wear_level": "high edge wear, story-driven damage",
    },
    notes="Derived from Elden Ring/Dark Souls/Bloodborne references — the recipe "
          "generalizes across the whole souls-like genre, not any one title.",
))
check("record_creative_recipe creates a new style recipe",
      style_result["recorded"] and style_result["action"] == "new_entry")

# Real aging/intent recipe
aging_result = json.loads(record(
    recipe_type="aging",
    canonical_name="three_centuries_outdoor_wet_decay",
    trigger_phrases=["ancient", "300 years old", "long abandoned", "centuries old"],
    parameters={
        "age_years": 300,
        "environment": "outdoor",
        "weather_exposure": "wet",
        "damage_severity": "medium",
        "surface_wear": "heavy",
        "narrative": "long-abandoned object",
    },
))
check("record_creative_recipe creates a new aging recipe",
      aging_result["recorded"] and aging_result["action"] == "new_entry")

# Confirm merge behavior: same canonical_name+recipe_type, NEW trigger phrase
confirm_result = json.loads(record(
    recipe_type="style",
    canonical_name="grimdark_souls_fantasy",
    trigger_phrases=["lies of p", "lords of the fallen"],  # new phrases, same recipe
    parameters={
        "genre": "dark_fantasy",
        "silhouette": "heavy, large readable forms",
        "color_palette": "cold, muted saturation",
        "materials": ["forged metal", "ancient stone", "weathered leather"],
        "form_language": "asymmetry, layered construction",
        "wear_level": "high edge wear, story-driven damage",
    },
))
check("recording again with new trigger phrases confirms existing entry, doesn't duplicate",
      confirm_result["action"] == "confirmed_existing" and confirm_result["id"] == style_result["id"])

# Verify merged trigger phrases via query
q1 = json.loads(query(trigger_phrase="lies of p"))
check("newly merged trigger phrase 'lies of p' now finds the SAME canonical recipe",
      q1["total_matches"] == 1 and q1["matches"][0]["canonical_name"] == "grimdark_souls_fantasy")

# The core design constraint: querying by an IP name finds a GENERALIZED recipe,
# never one whose canonical_name is itself the IP.
q2 = json.loads(query(trigger_phrase="elden ring"))
check("querying 'elden ring' returns a recipe whose canonical_name is generalized, not the game's name",
      q2["total_matches"] == 1
      and q2["matches"][0]["canonical_name"] == "grimdark_souls_fantasy"
      and "elden" not in q2["matches"][0]["canonical_name"].lower())

# Substring match works both directions (trigger_phrase in stored phrase, or vice versa)
q3 = json.invalid = None
q3 = json.loads(query(trigger_phrase="souls"))
check("partial substring match ('souls' matches 'souls-like'/'dark souls') finds the recipe",
      q3["total_matches"] == 1)

# Aging recipe is a SEPARATE entry, not mixed in with style
q4 = json.loads(query(trigger_phrase="ancient"))
check("aging trigger phrase returns only the aging recipe, not the style one",
      q4["total_matches"] == 1 and q4["matches"][0]["recipe_type"] == "aging")

# Zero-filter query returns summary only, never full parameter dumps
q5 = json.loads(query())
check("zero-filter query returns name/type summary only, no 'parameters' key leaked",
      "recipes" in q5 and all("parameters" not in r for r in q5["recipes"])
      and "matches" not in q5)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All checks passed.")
